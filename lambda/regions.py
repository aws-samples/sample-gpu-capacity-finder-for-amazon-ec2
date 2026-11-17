"""The Regions the account has enabled, shared by the API and the notifier.

EC2 DescribeRegions without AllRegions returns only the Regions enabled for the account (Regions
that need no opt-in, and opt-in Regions the account opted in to). The list is cached in the
offering cache table under CACHE_KEY and asked again at most once per FRESH_SECONDS, by the
first caller that takes the refresh lock (LOCK_KEY, the same "#lock#<item>" lock the API's forced
refreshes use). A failed lookup never fails the caller: it falls back to the stale cached list,
else to the caller's fallback list (the function's own Region).

Every feature (supply, prices, quotas, Capacity Blocks, Spot scores, test launches and their
cleanup) uses this one list: the enabled Regions minus excludeRegions.
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

import region_slots

logger = logging.getLogger()

# Regions the administrator excluded (deploy setting excludeRegions): never listed and never called
# by any feature; the next daily update removes them from the Spot score groups (score_groups).
EXCLUDED = frozenset(r for r in os.environ.get("EXCLUDE_REGIONS", "").split(",") if r)
CACHE_KEY = "regions#enabled"
LOCK_KEY = f"#lock#{CACHE_KEY}"
# The list counts as fresh for one day.
FRESH_SECONDS = 24 * 3600
# The item outlives its freshness (DynamoDB TTL on "expires"), so a failed refresh can still serve it.
KEEP_SECONDS = 7 * 86400
LOCK_SECONDS = 60
# A caller without the lock and without any cached list waits this long for the lock holder's answer.
WAIT_SECONDS = 3
WAIT_STEP_SECONDS = 0.25
NAME_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


def is_commercial(name):
    """True for a standard commercial Region name (us-east-1, ap-southeast-7). China, GovCloud, and
    isolated Region names do not belong to this partition and are left out."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return False
    parts = name.split("-")
    return parts[0] != "cn" and "gov" not in parts and not any(p.startswith("iso") for p in parts)


def filter_names(names):
    """The commercial Region names, sorted, without duplicates."""
    return sorted({n for n in names if is_commercial(n)})


def order(enabled):
    """The enabled Regions minus the excluded ones, in alphabetical order."""
    return sorted(set(enabled) - EXCLUDED)


def describe(ec2):
    """Live DescribeRegions (AllRegions=False: only the Regions enabled for the account), filtered."""
    return filter_names(r.get("RegionName") for r in ec2.describe_regions(AllRegions=False)["Regions"])


def read_item(table, now):
    """{regions, fetchedAt, fresh} from the cache item, or None when missing or unreadable."""
    try:
        item = table.get_item(Key={"cacheKey": CACHE_KEY}).get("Item")
    except ClientError as err:
        logger.warning("Region list cache read failed (%s)", err.response["Error"]["Code"])
        return None
    if not item:
        return None
    try:
        names = filter_names(json.loads(item["regions"]))
        fresh_until = int(item.get("freshUntil", 0))
    except (KeyError, TypeError, ValueError):
        return None
    if not names:
        return None
    return {"regions": names, "fetchedAt": item.get("fetchedAt"), "fresh": fresh_until > now}


def write_item(table, names, stamp, now):
    try:
        table.put_item(Item={
            "cacheKey": CACHE_KEY,
            "regions": json.dumps(sorted(names)),
            "fetchedAt": stamp,
            "freshUntil": now + FRESH_SECONDS,
            "expires": now + KEEP_SECONDS,
        })
    except ClientError as err:
        logger.warning("Region list cache write failed (%s)", err.response["Error"]["Code"])


def claim(table, now):
    """Take the refresh lock with a conditional write; False when another caller holds it."""
    try:
        table.put_item(
            Item={"cacheKey": LOCK_KEY, "lockedUntil": now + LOCK_SECONDS, "expires": now + LOCK_SECONDS + 86400},
            ConditionExpression="attribute_not_exists(cacheKey) OR lockedUntil <= :now",
            ExpressionAttributeValues={":now": now},
        )
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("Region list lock failed (%s)", err.response["Error"]["Code"])
        return False


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _code(err):
    return err.response["Error"].get("Code", "") if isinstance(err, ClientError) else type(err).__name__


def _record_first_seen(table, names, now):
    """Record the Regions a live lookup found in the permanent first-seen item (region_slots), the
    order in which the Spot score groups place new Regions. A failure is logged and changes nothing."""
    try:
        region_slots.observe(table, names, now)
    except Exception as err:  # noqa: BLE001 - discovery never fails the caller
        logger.warning("Region first-seen record failed (%s)", _code(err))


def store_live(table, names, now):
    """Store a live answer (the score updater's daily refresh) and record its first-seen times;
    returns (regions, fetchedAt)."""
    stamp = _stamp()
    write_item(table, names, stamp, now)
    _record_first_seen(table, order(names), now)
    return order(names), stamp


def resolve(table, lookup, fallback, now, take_lock=None, sleep=None):
    """(regions, fetchedAt, source): the cached list when fresh (source "cache"); else one caller
    (the one that takes the lock) asks `lookup()` and stores the answer ("live"). A caller that does
    not get the lock, or whose lookup fails, uses the stale list ("stale"); without one, a caller
    that did not get the lock waits up to WAIT_SECONDS for the holder's item, and `fallback` (the
    function's own Region) is the last answer ("configured", fetchedAt None). Never raises."""
    take_lock = take_lock or (lambda: claim(table, now))
    sleep = sleep or time.sleep
    try:
        cached = read_item(table, now)
    except Exception:  # noqa: BLE001 - discovery never fails the caller
        cached = None
    if cached and cached["fresh"]:
        return order(cached["regions"]), cached["fetchedAt"], "cache"
    try:
        took = take_lock()
    except Exception as err:  # noqa: BLE001
        logger.warning("Region list lock failed (%s)", _code(err))
        took = False
    if took:
        try:
            names = lookup()
            if not names:
                raise ValueError("DescribeRegions returned no commercial Region")
            listed, stamp = store_live(table, names, now)
            return listed, stamp, "live"
        except (ClientError, BotoCoreError, ValueError, TypeError, KeyError) as err:
            logger.warning("Region discovery failed (%s); using the %s Region list",
                           _code(err), "cached" if cached else "fallback")
    elif not cached:
        # The first lookup ever (or after the item expired from the table) runs in another caller.
        waited = 0.0
        while waited < WAIT_SECONDS:
            sleep(WAIT_STEP_SECONDS)
            waited += WAIT_STEP_SECONDS
            try:
                found = read_item(table, now)
            except Exception:  # noqa: BLE001
                found = None
            if found:
                return order(found["regions"]), found["fetchedAt"], "cache"
    if cached:
        return order(cached["regions"]), cached["fetchedAt"], "stale"
    return order(fallback), None, "configured"
