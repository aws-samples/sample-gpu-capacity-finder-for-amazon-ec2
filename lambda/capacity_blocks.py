"""Capacity Block offering search shared by the API and the notifier.

DescribeCapacityBlockOfferings checks the optional search window against limits that move with
the clock: EndDateRange must not be later than a date about nine days ahead, and StartDateRange
must be before a date about two days ahead (both observed at the time of writing; EC2 names the
limit in the error message). Without a window, EC2 searches its full default range. So the search
sends a window only when the caller asks for one, and when EC2 rejects it and names its limit,
retries once at that limit.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

from botocore.exceptions import BotoCoreError, ClientError

import cache_reads

logger = logging.getLogger()

# The fallback when a Region's detected list (supported_types) is unknown: the instance families
# that support Capacity Blocks for ML, from "Capacity Blocks for ML" in the Amazon EC2 User Guide
# plus p4de, which EC2 reports as supported. Each entry is the part of the type name before the dot.
FAMILIES = ("p4d", "p4de", "p5", "p5e", "p5en", "p6-b200", "p6-b300", "p6e-gb200", "trn1", "trn2")
# Per-Region detection: DescribeInstanceTypes with the supported-usage-class=capacity-block filter
# lists the instance types that support Capacity Blocks in that Region (trn2 in us-east-2 but not
# in us-east-1). The answer is cached in the shared cache table under cbtypes#<Region>, asked again
# once a day, and kept for 7 days as the answer when a new call fails.
SUPPORT_KEY_PREFIX = "cbtypes#"
SUPPORT_REFRESH_SECONDS = 24 * 3600
SUPPORT_KEEP_SECONDS = 7 * 24 * 3600
SUPPORT_FILTER = [{"Name": "supported-usage-class", "Values": ["capacity-block"]}]
MAX_COUNT = 64
# Codes EC2 returns when it rejects the request itself (a client-side error, not an outage).
CLIENT_ERRORS = {"InvalidParameterValue", "UnsupportedOperation", "Unsupported"}
MESSAGE_MAX = 200
# EC2 throttling the caller: the Capacity Block routes answer HTTP 429 "throttled" for these.
THROTTLE_ERRORS = {"RequestLimitExceeded", "Throttling", "ThrottlingException"}
THROTTLED = "throttled"
# Max capacity search, split into stateless requests (see search_stage): the stage 1 counts, threads
# per round, rounds per follow-up request, and the caps on EC2 calls.
STAGE1_COUNTS = (1, 2, 4, 8, 16, 32, 64)
STAGE_WORKERS = 7
FOLLOW_UP_ROUNDS = 2
SPLIT_POINTS = 3      # counts per narrowing round: the bracket splits into quarters
FINAL_GAP = 3         # a bracket with at most this many open counts is settled in one round
MAX_CALLS = 16        # the whole search, over all its requests (for answers 0 to 64; see the tests)
MAX_REQUEST_CALLS = 14  # one request, whatever lo and hi it was given
UNSUPPORTED_TYPE = "cb-unsupported-type"
INVALID_COUNT = "cb-invalid-count"
# Notes when EC2 narrowed the search window; the page maps each code to its own text.
WINDOW_END = "cb-window-end"
WINDOW_START = "cb-window-start"

_END_LIMIT_RE = re.compile(r"End date must not be later than ([^.]+)")
_START_LIMIT_RE = re.compile(r"Start date .*? must be before ([^.]+)")
_REQUEST_ID_RE = re.compile(r"\s*\(?\bRequest\s*ID\s*[:=]?\s*[0-9A-Za-z-]+\)?\.?", re.IGNORECASE)
_THROTTLE_RE = re.compile(r"rate exceeded", re.IGNORECASE)
_ACCOUNT_RE = re.compile(r"\b\d{12}\b")
_DATE_FORMATS = ("%a %b %d %H:%M:%S %Z %Y", "%a %b %d %H:%M:%S UTC %Y")


class OfferingError(Exception):
    """EC2 rejected the search (for example an instance type without Capacity Blocks)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def is_family(itype):
    return isinstance(itype, str) and itype.split(".", 1)[0] in FAMILIES


def is_supported(itype, types):
    """True when the instance type supports Capacity Blocks: in `types` (a Region's detected list),
    or by the FAMILIES fallback when `types` is None (unknown)."""
    if types is None:
        return is_family(itype)
    return isinstance(itype, str) and itype in types


def support_key(region):
    return f"{SUPPORT_KEY_PREFIX}{region}"


def detect_types(ec2):
    """The instance types that support Capacity Blocks in the client's Region (paginated)."""
    pages = ec2.get_paginator("describe_instance_types").paginate(Filters=SUPPORT_FILTER)
    return sorted({t["InstanceType"] for page in pages for t in page["InstanceTypes"]})


def parse_support(item, now):
    """(types, fresh) from a stored cbtypes# item: types is None for a missing, expired, or
    unreadable item; fresh is False once the item is due for its daily refresh."""
    if not item or int(item.get("expires", 0)) <= now:
        return None, False
    try:
        types = json.loads(item["types"])
    except (KeyError, TypeError, ValueError):
        return None, False
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        return None, False
    return types, int(item.get("refreshAfter", 0)) > now


def supported_types(table, ec2_for, region, now, stamp):
    """The Region's Capacity Block types: the cached list while fresh, else one DescribeInstanceTypes
    call (ec2_for(region) makes the client) that is stored for a day. When the call fails, the
    stale list (at most SUPPORT_KEEP_SECONDS old) or None, so callers use the FAMILIES fallback."""
    types, fresh = parse_support(cache_reads.get_one(table, support_key(region)), now)
    if fresh:
        return types
    try:
        live = detect_types(ec2_for(region))
    except (ClientError, BotoCoreError):
        logger.warning("Capacity Block type detection failed in %s", region, exc_info=True)
        return types
    try:
        table.put_item(Item={"cacheKey": support_key(region), "types": json.dumps(live), "fetchedAt": stamp,
                             "refreshAfter": now + SUPPORT_REFRESH_SECONDS, "expires": now + SUPPORT_KEEP_SECONDS})
    except (ClientError, BotoCoreError):
        logger.warning("Capacity Block type cache write failed for %s", region, exc_info=True)
    return live


def sanitize(message):
    """EC2's message without request IDs or account IDs, on one line, at most 200 characters."""
    text = _REQUEST_ID_RE.sub("", str(message or ""))
    text = _ACCOUNT_RE.sub("<account>", text)
    return re.sub(r"\s+", " ", text).strip()[:MESSAGE_MAX]


def error_code(code, message):
    """A short machine code for a client-side EC2 error, or None for any other error."""
    if code not in CLIENT_ERRORS:
        return None
    text = str(message or "").lower()
    if "not supported for capacity blocks" in text or (code != "InvalidParameterValue" and "instance type" in text):
        return UNSUPPORTED_TYPE
    if "duration is not valid" in text:
        return "cb-invalid-duration"
    if "instance count is not valid" in text:
        return INVALID_COUNT
    if "start date is not valid" in text:
        return "cb-invalid-start"
    if "end date is not valid" in text:
        return "cb-invalid-end"
    return "cb-invalid-request" if code == "InvalidParameterValue" else "cb-unsupported"


def parse_date(text):
    """Parse a date from an EC2 message ("Fri Nov 20 07:22:48 UTC 2026" or ISO 8601), in UTC."""
    text = str(text or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _limit(pattern, message):
    found = pattern.search(str(message or ""))
    return parse_date(found.group(1)) if found else None


def _stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _call(ec2, kwargs, max_items, paginate):
    if not paginate:
        resp = ec2.describe_capacity_block_offerings(MaxResults=max_items, **kwargs)
        return list(resp.get("CapacityBlockOfferings", []))
    pages = ec2.get_paginator("describe_capacity_block_offerings").paginate(
        PaginationConfig={"MaxItems": max_items}, **kwargs
    )
    return [o for page in pages for o in page.get("CapacityBlockOfferings", [])]


def _note(code, date, message):
    """A note about the search window: a machine code, the limit date (UTC, ISO 8601), and the English text."""
    return {"code": code, "params": {"date": date}, "message": message}


def search(ec2, itype, count, hours, start=None, end=None, max_items=200, paginate=True):
    """Offerings sorted by start date, and a list of notes, one each time EC2 narrowed the window
    (empty when it did not). Each note is {"code", "params", "message"}; see _note.

    `start` and `end` are optional UTC datetimes. Raises OfferingError for a client-side EC2
    error, and lets every other ClientError through.
    """
    asked_start = start
    notes = []
    retried = set()
    while True:
        kwargs = {"InstanceType": itype, "InstanceCount": count, "CapacityDurationHours": hours}
        if start:
            kwargs["StartDateRange"] = start
        if end:
            kwargs["EndDateRange"] = end
        try:
            offerings = _call(ec2, kwargs, max_items, paginate)
            break
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            message = err.response.get("Error", {}).get("Message", "")
            limit = _limit(_END_LIMIT_RE, message) if end else None
            if limit and limit < end and "end" not in retried:
                retried.add("end")
                end = limit
                notes.append(_note(WINDOW_END, _stamp(limit),
                                   f"EC2 limited the search window: offerings must end by {_stamp(limit)}."))
                continue
            limit = _limit(_START_LIMIT_RE, message) if start else None
            if limit and limit < start and "start" not in retried:
                retried.add("start")
                # Start just inside the limit, then keep only offerings from the date asked for.
                start = limit - timedelta(minutes=1)
                notes.append(_note(WINDOW_START, _stamp(limit),
                                   f"EC2 accepts a start date before {_stamp(limit)}; "
                                   "the list shows offerings that start on or after the date you picked."))
                continue
            mapped = error_code(code, message)
            if mapped:
                raise OfferingError(mapped, sanitize(message) or code)
            raise
    if asked_start:
        offerings = [o for o in offerings if o["StartDate"] >= asked_start]
    offerings.sort(key=lambda o: o["StartDate"])
    return offerings, notes


def is_throttle(err):
    """True when a ClientError is EC2 throttling the caller (after botocore's own retries)."""
    error = err.response.get("Error", {}) if isinstance(err, ClientError) else {}
    return error.get("Code", "") in THROTTLE_ERRORS or bool(_THROTTLE_RE.search(str(error.get("Message", ""))))


def _next_points(lo, hi, found):
    """The counts for the next follow-up round over the bracket (lo, hi), given the counts this
    request has already asked (`found`).

    At most FINAL_GAP open counts: all of them, and `lo` itself when this request has not asked it
    (so the answer comes with its offerings), which settles the bracket in this round. More open
    counts: SPLIT_POINTS evenly spaced counts strictly inside, which leave a quarter of the bracket.
    """
    inside = list(range(lo + 1, hi))
    if len(inside) <= FINAL_GAP:
        return inside + ([lo] if lo and lo not in found else [])
    span = hi - lo
    return [lo + (i * span) // (SPLIT_POINTS + 1) for i in range(1, SPLIT_POINTS + 1)]


def _round(pool, available, points):
    """Asks every count of one round in parallel and waits for all of them. When a call raises, the
    error of the smallest count is raised once the whole round has finished."""
    futures = {c: pool.submit(available, c) for c in points}
    wait(futures.values())
    for c in sorted(futures):
        if futures[c].exception() is not None:
            raise futures[c].exception()
    return {c: futures[c].result() for c in points}


def search_stage(available, lo=None, hi=None, high=MAX_COUNT):
    """One stateless step of the max capacity search: the largest count from 1 to `high` for which
    `available(count)` returns a non-empty list.

    Assumption (unchanged from the earlier sequential binary search): a count with offerings means
    every smaller count has them too. Under it the final answer equals the binary search's: the
    largest such count, with the list `available` returned for it.

    The bracket (lo, hi): `lo` is the largest count known to have an offering (0 when none is known),
    `hi` the smallest count known to have none (`high` + 1 when none is known), 0 <= lo < hi.
      - Stage 1 (lo and hi None): counts 1, 2, 4, ..., 32 and `high` (64) in one parallel round.
      - Follow-up (lo and hi from the previous answer): up to FOLLOW_UP_ROUNDS rounds (_next_points).
    Each round runs on at most STAGE_WORKERS threads and waits for all its calls; this request makes
    at most MAX_REQUEST_CALLS calls and never asks a count twice. For answers 0 to 64 the search ends
    within 3 requests and MAX_CALLS calls in all (stage 1: 1 round; then up to 2 rounds per request).

    Returns (done, lo, hi, found): `found` maps each count asked in this request to its list. When
    `done`, lo is the answer and, when lo > 0, found[lo] holds its offerings.
    """
    first = lo is None
    if first:
        lo, hi = 0, high + 1
    found = {}

    def settled():
        return hi - lo == 1 and (lo == 0 or bool(found.get(lo)))

    with ThreadPoolExecutor(max_workers=STAGE_WORKERS) as pool:
        for _ in range(1 if first else FOLLOW_UP_ROUNDS):
            if settled():
                break
            points = sorted({c for c in STAGE1_COUNTS if c < high} | {high}) if first else _next_points(lo, hi, found)
            # Guard: only counts 1 to `high`, never one twice, never over the per-request cap.
            points = [c for c in dict.fromkeys(points) if 1 <= c <= high and c not in found]
            points = points[:max(0, MAX_REQUEST_CALLS - len(found))]
            if not points:
                break
            got = _round(pool, available, points)
            found.update(got)
            hits = [c for c in got if got[c] and lo < c < hi]
            if hits:
                lo = max(hits)
            elif lo and lo in got and not got[lo]:
                # `lo` had offerings in an earlier request and has none now (another buyer took
                # them): it becomes the upper end, and the search goes on below it.
                hi = lo
                lo = max((c for c in found if found[c] and c < hi), default=0)
            misses = [c for c in got if not got[c] and lo < c < hi]
            if misses:
                hi = min(misses)
    return settled(), lo, hi, found


def summary(offering):
    """The fields the page shows for one offering."""
    return {
        "start": offering.get("StartDate"),
        "end": offering.get("EndDate"),
        "zone": offering.get("AvailabilityZone"),
        "durationHours": offering.get("CapacityBlockDurationHours"),
        "instanceCount": offering.get("InstanceCount"),
        "upfrontFee": offering.get("UpfrontFee"),
        "currencyCode": offering.get("CurrencyCode"),
    }
