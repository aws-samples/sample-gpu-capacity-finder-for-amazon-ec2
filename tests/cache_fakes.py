"""Shared pieces of the in-memory cache-table fakes: the conditional writes the code sends (refresh
locks, the Spot score governor item, new registry items), evaluated like DynamoDB, atomically."""
import threading

from botocore.exceptions import ClientError

LOCK = "attribute_not_exists(cacheKey) OR lockedUntil <= :now"
VERSION = "attribute_not_exists(cacheKey) OR version = :v"
NEW = "attribute_not_exists(cacheKey)"
# The Spot score registry and governor items (spot_configs), the score history items
# (spot_cache.HISTORY_PREFIX), and the Region group and first-seen items (region_slots): tests that
# count data reads and writes leave them out.
REGISTRY_PREFIXES = ("spscfg#", "#sps-budget", "spshist#", "#sps-slots#", "#regions-first-seen")

_lock = threading.Lock()


def is_registry(key):
    return str(key).startswith(REGISTRY_PREFIXES)


def failed():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")


def condition_holds(current, expression, values):
    """True when a PutItem with the condition may write over `current` (None: no item)."""
    if expression is None:
        return True
    values = values or {}
    if expression == LOCK:
        return current is None or current["lockedUntil"] <= values[":now"]
    if expression == VERSION:
        return current is None or int(current.get("version", 0)) == int(values[":v"])
    if expression == NEW:
        return current is None
    raise AssertionError(f"unexpected condition: {expression}")


def conditional_put(items, item, expression=None, values=None):
    """Write `item` into the dict `items` when the condition holds, else raise like DynamoDB."""
    with _lock:
        if not condition_holds(items.get(item["cacheKey"]), expression, values):
            raise failed()
        items[item["cacheKey"]] = dict(item)


def seeded(items):
    """Mark the governor item as seeded, so a test's first request reads no seed sources."""
    budget = items.setdefault("#sps-budget", {"cacheKey": "#sps-budget", "version": 1})
    budget["seeded"] = True


def register(items, cfg, at, result="accepted", blocked_until=None):
    """Put a registry entry for `cfg` (EC2 accepted it at `at`) and index it in the governor item."""
    import json

    entry = {"cacheKey": "spscfg#" + cfg.fp, "fp": cfg.fp, "kind": cfg.kind, "instanceType": cfg.itype,
             "endpoint": cfg.endpoint, "request": cfg.request, "scoresKey": cfg.scores_key, "source": "live",
             "firstAcceptedAt": at, "lastUsedAt": at, "lastResult": result, "expires": at + 8 * 86400}
    if blocked_until is not None:
        entry["blockedUntil"] = blocked_until
    items[entry["cacheKey"]] = entry
    budget = items.setdefault("#sps-budget", {"cacheKey": "#sps-budget", "version": 1})
    index = json.loads(budget.get("index", "{}"))
    index[cfg.fp] = {"t": cfg.itype, "k": cfg.kind}
    budget["index"] = json.dumps(index)
    budget["version"] = int(budget.get("version", 0)) + 1


def by_chunk(fn):
    """A stand-in for api._region_spot_scores(cfg) from one taking (itype, chunk)."""
    return lambda cfg: fn(cfg.itype, list(cfg.regions))
