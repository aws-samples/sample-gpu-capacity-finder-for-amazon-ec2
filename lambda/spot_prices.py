"""Spot prices per Region, shared by the score updater (every warm run) and the API (on a miss).

One DescribeSpotPriceHistory call per Region lists every scored instance type of that Region, and
one cache item per Region (price-spot#<Region>) keeps the latest price per type and Availability
Zone. The item is fresh for CACHE_SECONDS (freshUntil: the API looks the prices up again after
that) and lives GRACE_SECONDS longer (expires), like the Spot score items, so the score groups keep
their prices while the next warm run is still asking for scores, or after one skipped run. A Spot score group shows "from $x/h": the cheapest size in the cheapest
zone, with every size's own lowest price for the tooltip."""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from botocore.exceptions import BotoCoreError, ClientError

import score_groups

logger = logging.getLogger()

CACHE_SECONDS = 30 * 60
GRACE_SECONDS = 10 * 60
# Prices of one call: one row per type and zone with StartTime now; a generous page bound.
MAX_ITEMS = 2000
PRODUCT = "Linux/UNIX"


def cache_key(region):
    return f"price-spot#{region}"


def fetch(ec2, types):
    """{type: {zone: price text}} of the latest Linux/UNIX Spot prices of `types`, in one paginated
    DescribeSpotPriceHistory call. Raises on a failed call."""
    types = sorted(set(types))
    if not types:
        return {}
    pages = ec2.get_paginator("describe_spot_price_history").paginate(
        InstanceTypes=types, ProductDescriptions=[PRODUCT], StartTime=datetime.now(timezone.utc),
        PaginationConfig={"MaxItems": MAX_ITEMS})
    out = {}
    for page in pages:
        for p in page.get("SpotPriceHistory", []):
            # The newest row comes first for each type and zone.
            out.setdefault(p["InstanceType"], {}).setdefault(p["AvailabilityZone"], p["SpotPrice"])
    return out


def parse_item(item, now):
    """{data, fetchedAt, expires, freshUntil} of a stored item, or None when missing, expired or
    unreadable. An item without freshUntil is fresh until it expires."""
    if not item or int(item.get("expires", 0)) <= now:
        return None
    try:
        data = json.loads(item["data"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    expires = int(item["expires"])
    return {"data": data, "fetchedAt": item.get("fetchedAt"), "expires": expires,
            "freshUntil": int(item.get("freshUntil", expires))}


def entry(prices, stamp, now):
    """The {data, fetchedAt, expires, freshUntil} that write() stores."""
    return {"data": prices, "fetchedAt": stamp, "expires": now + CACHE_SECONDS + GRACE_SECONDS,
            "freshUntil": now + CACHE_SECONDS}


def is_fresh(found, now):
    """True when a parsed item is still inside CACHE_SECONDS (no new lookup needed)."""
    return found is not None and int(found.get("freshUntil", found["expires"])) > now


def write(table, region, prices, stamp, now):
    """Store the Region's prices; a failed write is logged and otherwise ignored."""
    stored = entry(prices, stamp, now)
    try:
        table.put_item(Item={"cacheKey": cache_key(region), "data": json.dumps(prices, sort_keys=True),
                             "fetchedAt": stamp, "expires": stored["expires"],
                             "freshUntil": stored["freshUntil"]})
    except (ClientError, BotoCoreError) as err:
        logger.warning("Spot price cache write failed for %s (%s)", region, type(err).__name__)


def _decimal(text):
    try:
        value = Decimal(str(text))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def _text(value):
    return format(value.normalize(), "f")


def cheapest(zones):
    """(price, zone) of the lowest price of {zone: price text}, or None."""
    found = [(_decimal(p), z) for z, p in (zones or {}).items()]
    found = [(p, z) for p, z in found if p is not None]
    return min(found) if found else None


def group_prices(prices, types):
    """{fromUsdPerHour, instanceType, az, sizes: {type: {usdPerHour, az}}} for one group in one
    Region: the cheapest size in its cheapest zone, and each size's lowest price. None when no
    size of the group has a price there."""
    sizes = {}
    best = None
    for itype in types:
        low = cheapest((prices or {}).get(itype))
        if low is None:
            continue
        sizes[itype] = {"usdPerHour": _text(low[0]), "az": low[1]}
        if best is None or (low[0], itype) < (best[0], best[2]):
            best = (low[0], low[1], itype)
    if best is None:
        return None
    return {"fromUsdPerHour": _text(best[0]), "instanceType": best[2], "az": best[1], "sizes": sizes}


def groups_view(prices, groups):
    """{group key: group_prices} for the groups ({key: {"types": [...]}}) with a price in the Region."""
    out = {}
    for key in score_groups.sort_keys(groups):
        found = group_prices(prices, groups[key]["types"])
        if found is not None:
            out[key] = found
    return out
