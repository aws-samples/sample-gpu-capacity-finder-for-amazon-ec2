"""Alert logic shared by the API and the notifier. Pure functions, no AWS calls."""
import re

import families

CAPACITY_BLOCK = "capacity-block"
SPOT_SCORE = "spot-score"
SIGNALS = (CAPACITY_BLOCK, SPOT_SCORE)
# Parameter name and allowed range per signal.
PARAMS = {CAPACITY_BLOCK: ("days", 1, 182), SPOT_SCORE: ("threshold", 1, 10)}
# Capacity Block durations EC2 accepts: 1 to 14 days, then 7-day steps up to 182.
BLOCK_DAYS = tuple(range(1, 15)) + tuple(range(21, 183, 7))
# Instances per Capacity Block alert. Alerts made before the count existed have none and mean 1.
COUNT_MAX = 64
DEFAULT_COUNT = 1

# The per-user record that holds the SNS subscription. "#" sorts before any Region name.
PROFILE_SK = "#profile"

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
PENDING = "pending"  # not checked yet; counts as not available
REJECTED = "rejected"  # EC2 refused the query itself (lastReason holds its code); never emails
# A Spot score check that got no score this run (no request EC2 accepted in the last day, the Region
# missing from the answer, or the daily limit). Never stored: the alert keeps its lastState, only
# its check time moves, and no email goes out.
UNKNOWN = "unknown"
# The state the page shows for each stored lastState.
STATES = {AVAILABLE: "available", UNAVAILABLE: "not-available", PENDING: "waiting", REJECTED: "rejected"}

TYPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}\.[a-z0-9-]{1,20}$")
SK_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d#[a-z][a-z0-9-]{0,29}\.[a-z0-9-]{1,20}#"
                   r"(capacity-block#\d{1,3}(#\d{1,2})?|spot-score#\d{1,3})$")


def valid_days(days):
    return days in BLOCK_DAYS


def valid_value(signal, value):
    """True when `value` is an accepted parameter for the signal."""
    _, low, high = PARAMS[signal]
    if not low <= value <= high:
        return False
    return signal != CAPACITY_BLOCK or valid_days(value)


def valid_count(count):
    return isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= COUNT_MAX


def count_of(item):
    """The instance count of a Capacity Block alert; DEFAULT_COUNT for items without one."""
    raw = item.get("count")
    return DEFAULT_COUNT if raw is None else int(raw)


def sort_key(region, itype, signal, value, count=DEFAULT_COUNT):
    """A Capacity Block alert for one instance keeps the key it had before counts existed, so an
    old alert and a new one for the same search are the same item."""
    base = f"{region}#{itype}#{signal}#{value}"
    if signal == CAPACITY_BLOCK and count != DEFAULT_COUNT:
        return f"{base}#{count}"
    return base


def state_view(item):
    """(state, reason) the page shows: available, not-available, waiting, or rejected with its reason."""
    last = item.get("lastState", PENDING)
    state = STATES.get(last, STATES[PENDING])
    return state, item.get("lastReason") if last == REJECTED else None


def is_subscription(item):
    return not str(item.get("sk", "")).startswith("#")


def is_valid(item, regions, score_patterns=families.DEFAULT_SPOT_SCORE_PATTERNS):
    """Skip items that no longer match the deployment settings. A Spot score alert for a type the
    deployment does not score (spotScoreFamilies, `score_patterns`) is skipped by the same check
    the API uses (families.scored); a Capacity Block alert does not depend on it (EC2 rejects a
    type without Capacity Blocks, which stores the state "rejected")."""
    itype = item.get("instanceType")
    signal = item.get("signal")
    if item.get("region") not in regions or signal not in SIGNALS:
        return False
    if not isinstance(itype, str) or not TYPE_RE.match(itype):
        return False
    if signal == SPOT_SCORE and not families.scored(itype, score_patterns):
        return False
    name = PARAMS[signal][0]
    try:
        if not valid_value(signal, int(item.get(name))):
            return False
        return signal != CAPACITY_BLOCK or valid_count(count_of(item))
    except (TypeError, ValueError):
        return False


def fetch_key(item):
    """Alerts with the same key share one EC2 query. Spot alerts that differ
    only in threshold share one score. A Spot alert names an instance type but checks its score
    group's score (family and GPU count, any size of the group), so alerts for two sizes of one
    group in one Region read one cached answer."""
    if item["signal"] == CAPACITY_BLOCK:
        return (CAPACITY_BLOCK, item["region"], item["instanceType"], int(item["days"]), count_of(item))
    return (SPOT_SCORE, item["region"], item["instanceType"])


def group(items):
    groups = {}
    for item in items:
        groups.setdefault(fetch_key(item), []).append(item)
    return groups


def is_unknown(item, result):
    """True for a Spot score alert without a score this run (`result` None)."""
    return item["signal"] == SPOT_SCORE and result is None


def is_available(item, result):
    """`result` is a list of offerings (capacity-block) or a score (spot-score)."""
    if item["signal"] == CAPACITY_BLOCK:
        return bool(result)
    return result is not None and int(result) >= int(item["threshold"])


def transitions(items, result):
    """Yield (item, new_state, notify). Notify only on not available -> available. A Spot score
    alert without a score yields UNKNOWN and never notifies: a missing score is not "not available"."""
    for item in items:
        if is_unknown(item, result):
            yield item, UNKNOWN, False
            continue
        state = AVAILABLE if is_available(item, result) else UNAVAILABLE
        yield item, state, state == AVAILABLE and item.get("lastState") != AVAILABLE


def message(item, result, site_url, group_label=None):
    """Return (subject, body) for the alert email. Subject stays under 100 characters. A Spot score
    alert reports its score group (`group_label`, "g6e · 4 GPUs"; the family when unknown)."""
    region, itype = item["region"], item["instanceType"]
    if item["signal"] == CAPACITY_BLOCK:
        days, count = int(item["days"]), count_of(item)
        first = min(result, key=lambda o: o["StartDate"])
        subject = f"Capacity Block available: {itype} in {region}"
        detail = (f"EC2 lists {len(result)} Capacity Block offering(s) for {count} x {itype}, {days} day(s), in {region}. "
                  f"Earliest start: {first['StartDate'].isoformat()} in {first.get('AvailabilityZone', 'an Availability Zone')}.")
    else:
        group = group_label or f"{families.family_of(itype)} family"
        subject = f"Spot score {int(result)}: {group} in {region}"
        detail = (f"The Spot placement score for {group} (any size of the group, your alert names {itype}) in {region} "
                  f"is {int(result)}, at or above your threshold of {int(item['threshold'])}. "
                  "The score is for the whole group; use a test launch to check one size.")
    body = (f"{detail}\n\nThis is a point-in-time signal, not a reservation. Check the page before you act:\n{site_url}\n\n"
            "You get this email because you added an alert in GPU Capacity Finder. "
            "Delete the alert on the page to stop emails for this signal.")
    return subject[:100], body
