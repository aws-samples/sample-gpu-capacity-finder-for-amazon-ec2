"""Region-level Spot placement score cache items, shared by the score updater, the API and the
notifier: one item per configuration (sps#<group key>#<Regions hash>, see score_groups), written
by whoever sent the request, and a short score history per group (spshist#<group key>#regions).
The per-type Region sets of earlier versions (#sps-regions) are only read now, by the registry's
one-time seed. The requests themselves are built and sent by spot_configs, the only module that
calls EC2 for scores."""
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()


# AWS restricts how many different request configurations an account can make in 24 hours
# (repeating one used in that window is allowed). The error code is MaxConfigLimitExceeded.
LIMIT_RE = re.compile(r"request configurations|24-hour|unique", re.I)
# The per-type Region sets of earlier versions (read by spot_configs.seed only).
REGION_SETS_KEY = "#sps-regions"

# The RegionNames limit of GetSpotPlacementScores (EC2 API reference: RegionNames, maximum 10 items).
MAX_REGIONS_PER_REQUEST = 10


def chunks(regions):
    """The Regions, sorted and without duplicates, cut into groups of at most
    MAX_REGIONS_PER_REQUEST. The same set of Regions always gives the same chunks."""
    names = sorted(set(regions))
    return [names[i:i + MAX_REGIONS_PER_REQUEST] for i in range(0, len(names), MAX_REGIONS_PER_REQUEST)]


def regions_hash(regions):
    return hashlib.sha256(",".join(sorted(regions)).encode()).hexdigest()[:16]


def cache_key(itype, regions):
    return f"sps#{itype}#{regions_hash(regions)}"


def read(table, itype, regions, now):
    """{scores: {region: score}, fetchedAt} from the cache item, or None when missing, expired,
    or unreadable. A failed read only counts as a miss."""
    return read_item(table, itype, regions, now)[0]


def read_item(table, itype, regions, now):
    """(entry, expires): read's answer and the item's expiry (epoch seconds), or (None, None)."""
    try:
        item = table.get_item(Key={"cacheKey": cache_key(itype, regions)}).get("Item")
    except ClientError as err:
        logger.warning("spot score cache read failed for %s (%s)", itype, err.response["Error"]["Code"])
        return None, None
    return parse_item(item, regions, now)


def parse_item(item, regions, now):
    """(entry, expires) of a stored item for `regions` (one chunk), or (None, None) when the item
    is missing, expired, or unreadable. read_item and the API's batched reads share it."""
    if not item or int(item.get("expires", 0)) <= now:
        return None, None
    try:
        scores = json.loads(item["scores"])
    except (KeyError, ValueError):
        return None, None
    if not isinstance(scores, dict):
        return None, None
    entry = {"scores": {r: int(scores[r]) for r in regions if r in scores}, "fetchedAt": item.get("fetchedAt")}
    return entry, int(item["expires"])


def write(table, itype, regions, scores, stamp, now, minutes):
    """Store the scores for `minutes`. A failed write is logged and otherwise ignored."""
    try:
        table.put_item(Item={
            "cacheKey": cache_key(itype, regions),
            "scores": json.dumps(scores, sort_keys=True),
            "fetchedAt": stamp,
            "expires": now + minutes * 60,
        })
    except (ClientError, BotoCoreError) as err:
        logger.warning("spot score cache write failed for %s (%s)", itype, _code(err))


def _code(err):
    return err.response.get("Error", {}).get("Code", "") if isinstance(err, ClientError) else type(err).__name__


def is_limit_error(err):
    """True for the 24-hour limit on new request configurations (MaxConfigLimitExceeded)."""
    if not isinstance(err, ClientError):
        return False
    code = err.response.get("Error", {}).get("Code", "") or ""
    text = err.response.get("Error", {}).get("Message", "") or ""
    return "ConfigLimit" in code or bool(LIMIT_RE.search(text))


def read_region_sets(table):
    """{type: {regions, until, legacy}} from the shared item; {} when missing or unreadable."""
    try:
        item = table.get_item(Key={"cacheKey": REGION_SETS_KEY}).get("Item")
    except ClientError as err:
        logger.warning("spot score Region set read failed (%s)", err.response["Error"]["Code"])
        return {}
    try:
        sets = json.loads(item["sets"]) if item else {}
    except (KeyError, TypeError, ValueError):
        return {}
    return sets if isinstance(sets, dict) else {}


# A short history of real Spot placement score answers: one small item per score group (scope
# HISTORY_REGIONS: Region-level answers). A reading is
# {at: ISO 8601 UTC, scores: {Region: score}}, newest last. Only answers EC2 just gave are
# appended (never a cached item), at most one per HISTORY_MIN_GAP_SECONDS per item.
HISTORY_PREFIX = "spshist#"
HISTORY_REGIONS = "regions"
HISTORY_MAX_READINGS = 48
HISTORY_WINDOW_SECONDS = 86400
HISTORY_MIN_GAP_SECONDS = 300
HISTORY_TTL_SECONDS = 2 * 86400
# Readings an answer shows (the last ones).
HISTORY_SHOWN = 24


def history_key(itype, scope):
    return f"{HISTORY_PREFIX}{itype}#{scope}"


def stamp_epoch(stamp):
    """Epoch seconds of an ISO 8601 UTC stamp (with or without microseconds), or None."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return int(datetime.strptime(str(stamp), fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def parse_history(item, now):
    """The readings of a history item, oldest first: readable ones only, none older than
    HISTORY_WINDOW_SECONDS, at most HISTORY_MAX_READINGS. [] when missing or unreadable."""
    try:
        raw = json.loads((item or {}).get("readings") or "[]")
    except (TypeError, ValueError):
        return []
    readings = []
    for r in raw if isinstance(raw, list) else []:
        at = stamp_epoch(r.get("at")) if isinstance(r, dict) else None
        scores = r.get("scores") if isinstance(r, dict) else None
        if at is None or not isinstance(scores, dict) or now - at >= HISTORY_WINDOW_SECONDS:
            continue
        try:
            clean = {str(k): int(v) for k, v in scores.items()}
        except (TypeError, ValueError):
            continue
        readings.append((at, {"at": r["at"], "scores": clean}))
    readings.sort(key=lambda r: r[0])
    return [r for _, r in readings][-HISTORY_MAX_READINGS:]


def read_history(table, key, now):
    """The readings of one history item; [] when missing, or when the read fails (logged)."""
    try:
        return parse_history(table.get_item(Key={"cacheKey": key}).get("Item"), now)
    except Exception as err:  # noqa: BLE001 - the history never fails a request
        logger.warning("spot score history read failed for %s (%s)", key, type(err).__name__)
        return []


def append_history(table, key, scores, stamp, now):
    """Add one reading {at: stamp, scores} to the history item and return the readings. No write
    when `scores` is empty or the newest reading is less than HISTORY_MIN_GAP_SECONDS older than
    `stamp`. A failed read or write is logged and ignored (the readings known so far are returned)."""
    at = stamp_epoch(stamp)
    readings = read_history(table, key, now)
    if not scores or at is None:
        return readings
    if readings and at - stamp_epoch(readings[-1]["at"]) < HISTORY_MIN_GAP_SECONDS:
        return readings
    readings = [r for r in readings + [{"at": stamp, "scores": {str(k): int(v) for k, v in scores.items()}}]
                if now - stamp_epoch(r["at"]) < HISTORY_WINDOW_SECONDS][-HISTORY_MAX_READINGS:]
    try:
        table.put_item(Item={"cacheKey": key, "readings": json.dumps(readings, sort_keys=True),
                             "expires": now + HISTORY_TTL_SECONDS})
    except Exception as err:  # noqa: BLE001 - the history never fails a request
        logger.warning("spot score history write failed for %s (%s)", key, type(err).__name__)
    return readings


def shown(readings):
    return list(readings[-HISTORY_SHOWN:])


def set_chunks(entry):
    """The Region lists of a stored set, one request each. A set stored by the earlier daily-limit
    fallback ("legacy") is one request with its Regions in their stored order, as it was sent;
    any other set is cut by chunks()."""
    regions = list(entry.get("regions") or [])
    if entry.get("legacy") and 1 <= len(regions) <= MAX_REGIONS_PER_REQUEST:
        return [list(dict.fromkeys(regions))]
    return chunks(regions)
