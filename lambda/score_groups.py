"""Spot placement score groups: which instance types share one score, and in which Regions each
group is asked. Shared by the score updater (the only writer), the API and the notifier (readers).

AWS restricts how many different Spot placement score request configurations an account can make
in a rolling 24 hours; repeating one used in that window is free and returns a fresh score. One
request that lists several instance types returns ONE score per Region for the whole flexible set.
So the configurations are few and stay the same from day to day:

* A score group is one family and one GPU count per instance ("g6e/4", label "g6e · 4 GPUs"):
  the sizes of a family with the same number of GPUs. The GPU count comes from
  DescribeInstanceTypes (GpuInfo.Gpus[0].Count) and is stored once per type.
* Only the families of the `spotScoreFamilies` setting are scored (families.scored).
* A group's InstanceTypes are the sorted union of its sizes seen in any enabled Region. The list
  only grows, so a Region whose lookup failed never changes it.
* A group's Regions are the Regions that sell at least one of its sizes, kept in chunks of at most
  CHUNK_SIZE (the RegionNames limit of one request), one configuration per chunk. The list is
  append-only: a new Region goes into the last chunk while it has room, then a new chunk opens, in
  the order first-seen date, then name. A Region that stops selling stays; a Region whose lookup
  failed changes nothing. The one exception: a Region in excludeRegions is removed from its chunk
  at the next daily update.
* Every request goes to one endpoint (ENDPOINT), so the endpoint never changes a configuration.

The state is one cache-table item without a TTL (STATE_KEY). Membership changes only in the daily
update (and the bootstrap run) of the score updater; page loads, the Refresh button and the alert
check only read it."""
import json
import logging
from datetime import datetime, timedelta, timezone

from botocore.exceptions import BotoCoreError, ClientError

import families
import spot_configs
from type_order import family_key

logger = logging.getLogger()

STATE_KEY = "#sps-groups"
# The Region whose EC2 endpoint receives every Spot placement score request.
ENDPOINT = "us-east-1"
# The RegionNames limit of GetSpotPlacementScores.
CHUNK_SIZE = 10
SAME_VERSION = "attribute_not_exists(cacheKey) OR version = :v"
# The daily update runs at this UTC time; the warm run every WARM_MINUTES (minutes 0, 15, 30, 45).
DAILY_HOUR_UTC, DAILY_MINUTE_UTC = 0, 30
WARM_MINUTES = 15


def _code(err):
    return err.response.get("Error", {}).get("Code", "") if isinstance(err, ClientError) else type(err).__name__


def _day(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d")


def _iso(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ keys
def group_key(itype, gpus):
    """The score group of an instance type ("g6e.12xlarge" with 4 GPUs -> "g6e/4"), or None when
    its GPU count is unknown (or 0)."""
    try:
        count = int((gpus or {}).get(itype) or 0)
    except (TypeError, ValueError):
        return None
    return f"{families.family_of(itype)}/{count}" if count > 0 else None


def parse_key(key):
    """(family, gpu count) of a group key, or (key, None) for a key without a count."""
    family, _, count = str(key).rpartition("/")
    try:
        return family, int(count)
    except ValueError:
        return str(key), None


def label(key):
    """The group for people: "g6e · 4 GPUs", "p5 · 8 GPUs", "g6 · 1 GPU"."""
    family, count = parse_key(key)
    if count is None:
        return family
    return f"{family} · {count} GPU{'' if count == 1 else 's'}"


def sort_keys(keys):
    """Group keys in natural family order, then by GPU count."""
    return sorted(keys, key=lambda k: (family_key(parse_key(k)[0]), parse_key(k)[1] or 0, k))


# ------------------------------------------------------------ schedule
def last_daily_slot(now):
    """Epoch of the most recent daily update time (UTC) at or before `now`."""
    day = datetime.fromtimestamp(int(now), timezone.utc).replace(hour=DAILY_HOUR_UTC, minute=DAILY_MINUTE_UTC,
                                                                  second=0, microsecond=0)
    if day.timestamp() > now:
        day -= timedelta(days=1)
    return int(day.timestamp())


def next_daily(now):
    """Epoch of the next daily update time after `now`."""
    return last_daily_slot(now) + 86400


def next_warm(now):
    """Epoch of the next warm run (every WARM_MINUTES, at minutes 0, 15, 30 and 45) after `now`."""
    step = WARM_MINUTES * 60
    return (int(now) // step + 1) * step


def daily_due(state, now):
    """True when the daily update of the latest slot has not run yet."""
    return int((state or {}).get("dailyAt") or 0) < last_daily_slot(now)


def signature(patterns):
    """The spotScoreFamilies setting as stored with the groups: a change means a new bootstrap."""
    return sorted(dict.fromkeys(p for p in patterns if p))


def needs_bootstrap(state, patterns):
    """True when the groups must be built now instead of at the next daily update: no build stored
    yet (first deploy: no item, version 0), or a deploy that changed spotScoreFamilies. A stored
    build with zero groups (no scored family sold in an enabled Region) is a build: it is not
    repeated before the next daily update. False for an unreadable state (None): nothing is built
    from a failed read."""
    if state is None:
        return False
    return not state.get("version") or state.get("families") != signature(patterns)


# --------------------------------------------------------------- state
def _load(raw, default):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _clean_groups(raw):
    out = {}
    for key, g in (raw or {}).items():
        if not isinstance(g, dict):
            continue
        types = [t for t in g.get("types") or [] if isinstance(t, str)]
        chunks = [[r for r in c if isinstance(r, str)] for c in g.get("chunks") or [] if isinstance(c, list)]
        chunks = [c for c in chunks if c]
        added = {r: d for r, d in (g.get("added") or {}).items() if isinstance(r, str) and isinstance(d, str)}
        if types and chunks:
            out[str(key)] = {"types": types, "chunks": chunks, "added": added}
    return out


def parse_state(item):
    """The stored state as plain values: {version, families, groups, gpus, offered, dailyAt,
    updatedAt}; an empty state (version 0) for a missing item."""
    item = item or {}
    gpus = {}
    for t, n in _load(item.get("gpus"), {}).items():
        try:
            gpus[str(t)] = int(n)
        except (TypeError, ValueError):
            continue
    offered = {str(r): [t for t in ts if isinstance(t, str)]
               for r, ts in _load(item.get("offered"), {}).items() if isinstance(ts, list)}
    return {
        "version": int(item.get("version", 0) or 0),
        "families": _load(item.get("families"), []),
        "groups": _clean_groups(_load(item.get("groups"), {})),
        "gpus": gpus,
        "offered": offered,
        "dailyAt": int(item.get("dailyAt", 0) or 0),
        "updatedAt": int(item.get("updatedAt", 0) or 0),
    }


def read_state(table, consistent=False):
    """The stored state, or None when the item cannot be read."""
    try:
        kwargs = {"ConsistentRead": True} if consistent else {}
        item = table.get_item(Key={"cacheKey": STATE_KEY}, **kwargs).get("Item")
    except (ClientError, BotoCoreError) as err:
        logger.warning("Spot score groups read failed (%s)", _code(err))
        return None
    return parse_state(item)


def write_state(table, state, now):
    """One conditional write of the next version; False when another writer came in between or
    the write failed (logged). Only the score updater writes."""
    item = {
        "cacheKey": STATE_KEY, "version": state["version"] + 1,
        "families": json.dumps(state["families"]),
        "groups": json.dumps(state["groups"], sort_keys=True),
        "gpus": json.dumps(state["gpus"], sort_keys=True),
        "offered": json.dumps(state["offered"], sort_keys=True),
        "dailyAt": state["dailyAt"], "updatedAt": now,
    }
    try:
        table.put_item(Item=item, ConditionExpression=SAME_VERSION, ExpressionAttributeValues={":v": state["version"]})
        return True
    except (ClientError, BotoCoreError) as err:
        logger.warning("Spot score groups write failed (%s)", _code(err))
        return False


# ---------------------------------------------------------- pure rules
def collect(offered, gpus, patterns):
    """{key: {"types": set, "regions": set}} from `offered` ({region: [types]}, only Regions whose
    lookup answered): the scored types with a known GPU count, by group."""
    found = {}
    for region, types in (offered or {}).items():
        for itype in types:
            if not families.scored(itype, patterns):
                continue
            key = group_key(itype, gpus)
            if key is None:
                continue
            entry = found.setdefault(key, {"types": set(), "regions": set()})
            entry["types"].add(itype)
            entry["regions"].add(region)
    return found


def place(chunks, names):
    """`chunks` with `names` (in order) appended: a name already placed stays where it is; a new one
    fills the last chunk up to CHUNK_SIZE, then opens a new chunk. Never re-sorts."""
    out = [list(c) for c in chunks if c]
    placed = {r for c in out for r in c}
    for name in names:
        if name in placed:
            continue
        if not out or len(out[-1]) >= CHUNK_SIZE:
            out.append([])
        out[-1].append(name)
        placed.add(name)
    return out


def order_new(names, first_seen, today_epoch):
    """New Regions in placement order: first-seen date (UTC day), then name. A Region without a
    first-seen time sorts as seen today."""
    return sorted(dict.fromkeys(names), key=lambda r: (_day(first_seen.get(r, today_epoch)), r))


def merge(groups, found, patterns, excluded, first_seen, now):
    """The stored groups after one daily update. `found` comes from collect() over the Regions that
    answered. Sizes that no longer match `patterns` (after a spotScoreFamilies change) are dropped,
    and a group left without a size is dropped; every other group keeps its types and chunks, with
    the newly seen sizes added to its types, the excluded Regions removed from its chunks, and the
    newly selling Regions appended (order_new, place). A group left without a chunk is dropped."""
    excluded = set(excluded or ())
    out = {}
    keys = set(groups) | set(found)
    for key in keys:
        old = groups.get(key) or {"types": [], "chunks": [], "added": {}}
        new = found.get(key) or {"types": set(), "regions": set()}
        types = sorted(t for t in set(old["types"]) | set(new["types"]) if families.scored(t, patterns))
        if not types:
            continue
        chunks = [[r for r in c if r not in excluded] for c in old["chunks"]]
        chunks = [c for c in chunks if c]
        placed = {r for c in chunks for r in c}
        fresh = order_new([r for r in new["regions"] if r not in placed and r not in excluded], first_seen, now)
        chunks = place(chunks, fresh)
        added = {r: d for r, d in old.get("added", {}).items() if any(r in c for c in chunks)}
        added.update({r: _day(now) for r in fresh})
        if chunks:
            out[key] = {"types": types, "chunks": chunks, "added": added}
    return out


def members(group):
    """The group's Regions in stored order."""
    return [r for c in group["chunks"] for r in c]


def configs(groups):
    """[(key, Config)] of every current configuration, in group order then chunk order."""
    out = []
    for key in sort_keys(groups):
        g = groups[key]
        for chunk in g["chunks"]:
            out.append((key, spot_configs.build(key, chunk, ENDPOINT, types=g["types"])))
    return out


def config_for(groups, key, region):
    """The configuration of the chunk of group `key` that holds `region`, or None."""
    g = (groups or {}).get(key)
    if not g:
        return None
    chunk = next((c for c in g["chunks"] if region in c), None)
    return spot_configs.build(key, chunk, ENDPOINT, types=g["types"]) if chunk else None


def config_state(entry, now, limited_until=0):
    """active (granted: accepted in the last day, repeats are free), limited (AWS refused it, or
    every new grant waits for the cool-down), or planned (waits for room in the daily budget)."""
    if spot_configs.is_warm(entry, now):
        return "active"
    if spot_configs.is_blocked(entry, now) or limited_until > now:
        return "limited"
    return "planned"


def schedule_view(state, now):
    """The schedule fields every Spot score answer and /api/config carry."""
    daily = int((state or {}).get("dailyAt") or 0)
    return {
        "lastDailyUpdateAt": _iso(daily) if daily else None,
        "nextDailyUpdateAt": _iso(next_daily(now)),
        "nextWarmAt": _iso(next_warm(now)),
        "warmMinutes": WARM_MINUTES,
        "dailyUpdateUtc": f"{DAILY_HOUR_UTC:02d}:{DAILY_MINUTE_UTC:02d}",
    }
