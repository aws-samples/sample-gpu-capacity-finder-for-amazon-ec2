"""The permanent first-seen record of the account's Regions, shared by the Region discovery
(regions.py, the writer) and the Spot score groups (score_groups, which place new Regions in
first-seen order, then by name).

FIRST_SEEN_KEY: one item, without a TTL, with the time this deployment first saw each Region (in a
live DescribeRegions answer). The first write seeds every Region known then with the same time; a
Region seen later gets its own time. A Region that disappears keeps its time. The item uses GetItem
and conditional PutItem only. (The fixed Region groups of earlier versions lived here too; the
score groups of score_groups replace them.)"""
import json
import logging

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()

FIRST_SEEN_KEY = "#regions-first-seen"
SAME_VERSION = "attribute_not_exists(cacheKey) OR version = :v"
MAX_ATTEMPTS = 3


def _code(err):
    return err.response.get("Error", {}).get("Code", "") if isinstance(err, ClientError) else type(err).__name__


# ---------------------------------------------------------------- first seen
def _parse_first_seen(item):
    try:
        raw = json.loads((item or {}).get("regions") or "{}")
    except (TypeError, ValueError):
        return {}
    out = {}
    for name, at in (raw.items() if isinstance(raw, dict) else ()):
        try:
            out[str(name)] = int(at)
        except (TypeError, ValueError):
            continue
    return out


def read_first_seen(table, consistent=False):
    """(version, {region: epoch}) or None when the item cannot be read; (0, {}) when missing."""
    try:
        kwargs = {"ConsistentRead": True} if consistent else {}
        item = table.get_item(Key={"cacheKey": FIRST_SEEN_KEY}, **kwargs).get("Item")
    except (ClientError, BotoCoreError) as err:
        logger.warning("Region first-seen read failed (%s)", _code(err))
        return None
    return (int((item or {}).get("version", 0) or 0), _parse_first_seen(item))


def observe(table, names, now):
    """{region: first-seen epoch} after recording the Regions of `names` not recorded yet (with
    `now`). The first write ever seeds every Region in `names` with the same time, so the seed is
    the same whichever caller writes it, and it happens once (a conditional write). A failed read
    or write changes nothing and returns what is known (the missing ones as `now`)."""
    names = [n for n in dict.fromkeys(names) if n]
    for _ in range(MAX_ATTEMPTS):
        state = read_first_seen(table, consistent=True)
        if state is None:
            break
        version, seen = state
        missing = [n for n in names if n not in seen]
        if not missing:
            return seen
        merged = {**seen, **{n: now for n in missing}}
        item = {"cacheKey": FIRST_SEEN_KEY, "version": version + 1, "regions": json.dumps(merged, sort_keys=True)}
        if version == 0:
            item["seededAt"] = now
        try:
            # Version 0 is "no item yet": only one first seed can win.
            table.put_item(Item=item, ConditionExpression=SAME_VERSION, ExpressionAttributeValues={":v": version})
            return merged
        except ClientError as err:
            if _code(err) != "ConditionalCheckFailedException":
                logger.warning("Region first-seen write failed (%s)", _code(err))
                return merged
        except BotoCoreError as err:
            logger.warning("Region first-seen write failed (%s)", _code(err))
            return merged
    return {n: now for n in names}
