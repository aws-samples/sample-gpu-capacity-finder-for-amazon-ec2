"""Spot placement score request configurations: one builder for every GetSpotPlacementScores
request, a registry of the configurations this deployment sent, and a governor for new ones.

AWS restricts how many different request configurations an account can make in a rolling 24 hours;
repeating a configuration used in that window stays allowed and returns a fresh score. The
restriction is not published and not adjustable, so the sample counts its own configurations
against a budget set at deploy time (spotScoreConfigsPerDay) and repeats known ones. Only the
score updater's daily update and warm runs grant new configurations (score_groups); page loads,
the Refresh button and the alert check only repeat granted ones.

* build() is the only way to make a request, and send() is the only place that calls EC2. Equal
  logical requests give byte-identical keyword arguments, so they are one configuration.
* The registry keeps one cache-table item per configuration sent (REGISTRY_PREFIX + fingerprint):
  the request, the type, when it was first accepted and last used, the last result, how long it
  stays blocked after a limit error, and how long a new or cold one waits after EC2 rejected it
  with another 4xx answer (retryAfter).
* The governor item (BUDGET_KEY) keeps the new configurations of the last 24 hours, the cool-down
  after a limit error, and an index of the registered configurations. A new configuration takes a
  slot with a conditional write before it is sent, so two copies of a function cannot both take
  the last slot. The limit is only inferred from normal accepted or refused answers, never probed.

Every item lives in the existing cache table and uses GetItem, BatchGetItem and PutItem only."""
import hashlib
import json
import logging
from collections import namedtuple
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

import cache_reads
import spot_cache

logger = logging.getLogger()

# TargetCapacity is the same in every request, forever. EC2 applies a separate, hard limit to
# requests that vary the target capacity for the same instance types, and a different value is a
# different configuration too. Never make it a parameter.
TARGET_CAPACITY = 1

DAY_SECONDS = 86400
# A configuration accepted less than this long ago is "warm": repeating it costs no budget. One
# hour short of 24, so a repeat never lands just after EC2 forgot the configuration.
WARM_SECONDS = 23 * 3600
# How long a configuration counts against the budget (a slot in new24h, and "in use" for
# budget.max_configs): 24 hours plus a margin, so a slot frees one updater run after AWS's rolling
# window has dropped the request, not a few seconds before (the stamps are the run's start time,
# taken before the request is sent).
RELEASE_MARGIN_SECONDS = 15 * 60
COUNT_SECONDS = DAY_SECONDS + RELEASE_MARGIN_SECONDS
# A configuration EC2 refused for the limit is not sent again for this long, and no new
# configuration is tried for this long either (the cool-down).
BLOCK_SECONDS = DAY_SECONDS
# A new or cold configuration that EC2 rejected with a 4xx answer other than the limit or a
# throttling (for example InvalidParameterValue or Unsupported for one size of a group) is not
# granted or sent again for this long. The slot is still given back, and the cell stays planned.
ERROR_RETRY_SECONDS = DAY_SECONDS
# After a refusal of a new configuration, the daily budget stays at most the count of new
# configurations accepted before it, for this long.
OBSERVED_CAP_SECONDS = 7 * DAY_SECONDS
# A refusal lowers the cap only when this deployment itself had this many new configurations
# accepted in the 24 hours before it; with fewer, the budget was used up elsewhere (other code or
# callers in the account): the refusal then only blocks and cools down. Never lowered below this.
OBSERVED_MIN_ACCEPTED = 3
# Registry items expire this long after their last write (the table's TTL attribute).
ENTRY_TTL_SECONDS = 8 * DAY_SECONDS
MAX_REFUSALS_KEPT = 10
# Conditional writes of the governor item: attempts before it gives up (and denies).
MAX_ATTEMPTS = 3
# Throttling answers: EC2 refused the request before it processed it.
THROTTLE_CODES = frozenset({"RequestLimitExceeded", "Throttling", "ThrottlingException"})
# Set on an exception of send() that came after EC2 answered an earlier page (the configuration
# was then processed and counts).
ANSWERED_ATTR = "gcf_pages_answered"

REGISTRY_PREFIX = "spscfg#"
BUDGET_KEY = "#sps-budget"
BUDGET_CONDITION = "attribute_not_exists(cacheKey) OR version = :v"
NEW_ITEM_CONDITION = "attribute_not_exists(cacheKey)"

REGION = "region"
ACCEPTED, BLOCKED, ERROR = "accepted", "blocked", "error"

# Who asks for a configuration that is not warm. GRANT (the score updater's daily update and warm
# runs) may take new configurations within the budget; REPEAT_ONLY (page loads, the Refresh button)
# only repeats warm ones and never creates one.
GRANT, REPEAT_ONLY = "grant", "repeat-only"

# per_day: the most new configurations in a rolling 24 hours; max_configs: the most different
# configurations in use in 24 hours. Both come from spotScoreConfigsPerDay, yet the two checks are
# not the same: a slot in new24h frees COUNT_SECONDS after the configuration was first granted, while
# a configuration repeated since stays in use for COUNT_SECONDS after its last use, so max_configs
# keeps new grants from adding to configurations still in use. daily_cap may also lower per_day
# below the setting after a refusal by AWS (the observed cap); max_configs stays at the setting.
Budget = namedtuple("Budget", "per_day max_configs")
DEFAULT_BUDGET = Budget(40, 40)

# itype: the score group key of the request ("g6e/4", score_groups), the label the registry index
# and the cache keys use.
Config = namedtuple("Config", "itype kind endpoint regions kwargs request fp scores_key")


# ----------------------------------------------------------------- builder
def request_kwargs(types, regions):
    """The canonical GetSpotPlacementScores arguments. The instance types are sorted and unique;
    the Region list keeps its stored order (the order may count for EC2). TargetCapacity is the
    constant TARGET_CAPACITY. SingleAvailabilityZone, TargetCapacityUnitType, MaxResults and
    IncludeLocalZones are never sent. NextToken pages belong to the same configuration and are
    added by the paginator."""
    names = [types] if isinstance(types, str) else list(types)
    return {"InstanceTypes": sorted(set(names)), "TargetCapacity": TARGET_CAPACITY, "RegionNames": list(regions)}


def canonical(kwargs):
    return json.dumps(kwargs, sort_keys=True, separators=(",", ":"))


def fingerprint(endpoint, kwargs):
    """The identity of a configuration: the endpoint Region and the canonical request."""
    text = json.dumps({"endpoint": endpoint, "request": kwargs}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def build(itype, regions, endpoint, types=None):
    """The Config of one Region-level request across `regions` (at most MAX_REGIONS_PER_REQUEST,
    sent to `endpoint`). `itype` labels it: a score group key ("g6e/4") whose `types` are the
    group's sizes, or one instance type when `types` is None. EC2 answers one score per Region for
    the whole flexible set."""
    regions = tuple(regions)
    if not 1 <= len(regions) <= spot_cache.MAX_REGIONS_PER_REQUEST:
        raise ValueError(f"one request takes 1 to {spot_cache.MAX_REGIONS_PER_REQUEST} Regions, not {len(regions)}")
    names = [itype] if types is None else list(types)
    if not names:
        raise ValueError("a request names at least one instance type")
    kwargs = request_kwargs(names, regions)
    return Config(itype, REGION, endpoint, regions, kwargs, canonical(kwargs),
                  fingerprint(endpoint, kwargs), spot_cache.cache_key(itype, regions))


def send(ec2, cfg):
    """The only GetSpotPlacementScores call: every answer row of the configuration (all pages).
    The arguments come from the stored canonical request, and must equal the built ones."""
    kwargs = json.loads(cfg.request)
    if kwargs != cfg.kwargs or kwargs.get("TargetCapacity") != TARGET_CAPACITY:
        raise ValueError("the request does not match its configuration")
    pages = ec2.get_paginator("get_spot_placement_scores").paginate(**kwargs)
    rows, answered = [], 0
    try:
        for page in pages:
            answered += 1
            rows += page["SpotPlacementScores"]
    except (ClientError, BotoCoreError) as err:
        if answered:
            setattr(err, ANSWERED_ATTR, answered)
        raise
    return rows


def region_scores(rows, regions):
    """{region: 1..10} from Region-level rows, for the asked Regions only, in their order. A Region
    EC2 leaves out (not in its top results, or not offered there) is missing."""
    found = {}
    for s in rows:
        region = s.get("Region")
        if region in regions and s.get("Score") is not None:
            found[region] = max(int(s["Score"]), found.get(region, 0))
    return {r: found[r] for r in regions if r in found}


def fetch(ec2, cfg):
    return region_scores(send(ec2, cfg), cfg.regions)


def is_limit_error(err):
    return spot_cache.is_limit_error(err)


def not_processed(err):
    """True when a failed send() shows EC2 did not process the request, so the configuration did
    not count: a throttling answer, or another 4xx answer (a validation, unsupported or permission
    error) on the first page. A 5xx answer, an answer without a status, a botocore error or a
    failure after an answered page may have counted: the slot is kept."""
    if not isinstance(err, ClientError) or is_limit_error(err) or getattr(err, ANSWERED_ATTR, 0):
        return False
    code = err.response.get("Error", {}).get("Code", "") or ""
    status = _int(err.response.get("ResponseMetadata", {}).get("HTTPStatusCode"))
    return code in THROTTLE_CODES or 400 <= status < 500


# ---------------------------------------------------------------- registry
def entry_key(fp):
    return REGISTRY_PREFIX + fp


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_entry(item):
    """A registry item with its numbers as int, or None when missing or not a registry item."""
    if not item or not str(item.get("cacheKey", "")).startswith(REGISTRY_PREFIX) or not item.get("fp"):
        return None
    entry = dict(item)
    for name in ("firstAcceptedAt", "lastUsedAt", "blockedUntil", "retryAfter", "expires"):
        if name in entry:
            entry[name] = _int(entry[name])
    return entry


def is_blocked(entry, now):
    return bool(entry) and _int(entry.get("blockedUntil")) > now


def waits_after_error(entry, now):
    """A new or cold configuration that EC2 rejected (rejected()) waits until its retryAfter."""
    return bool(entry) and _int(entry.get("retryAfter")) > now


def rejected(err):
    """True when EC2 answered a failed send() with a 4xx answer that is neither the limit nor a
    throttling, on the first page: the request itself is wrong for EC2 (a validation, unsupported
    or permission error), so sending it again soon gives the same answer."""
    if not not_processed(err):
        return False
    return (err.response.get("Error", {}).get("Code", "") or "") not in THROTTLE_CODES


def is_warm(entry, now):
    """Accepted by EC2 less than WARM_SECONDS ago and not blocked: repeating it is free."""
    if not entry or is_blocked(entry, now):
        return False
    used = _int(entry.get("lastUsedAt"))
    return used > 0 and 0 <= now - used < WARM_SECONDS


def in_use(entry, now):
    """Used (accepted) in the last COUNT_SECONDS, blocked or not: it counts toward
    budget.max_configs. is_warm keeps the shorter WARM_SECONDS rule for what is free to repeat."""
    used = _int((entry or {}).get("lastUsedAt"))
    return used > 0 and 0 <= now - used < COUNT_SECONDS


# The `known` value of active_count for an index entry whose read failed: counted as in use (fail
# closed) and never pruned from the index.
UNREADABLE = object()


def read_entries(table, fps, unread=None):
    """{fp: entry} for the registered fingerprints (one batched read); missing and unreadable ones
    are absent. The fingerprints whose read failed are added to `unread` (when given)."""
    failed = []
    items = cache_reads.get_items(table, [entry_key(fp) for fp in fps], unread=failed)
    if unread is not None:
        unread.extend(fp for fp in fps if entry_key(fp) in set(failed))
    found = {}
    for fp in fps:
        entry = parse_entry(items.get(entry_key(fp)))
        if entry is not None and entry["fp"] == fp:
            found[fp] = entry
    return found


def _entry_item(cfg, old, now, **fields):
    item = {
        "cacheKey": entry_key(cfg.fp), "fp": cfg.fp, "kind": cfg.kind, "instanceType": cfg.itype,
        "endpoint": cfg.endpoint, "request": cfg.request, "scoresKey": cfg.scores_key,
        "source": (old or {}).get("source", "live"), "expires": now + ENTRY_TTL_SECONDS,
    }
    for name in ("firstAcceptedAt", "lastUsedAt", "lastResult", "lastErrorCode", "blockedUntil", "retryAfter"):
        if old and old.get(name) is not None:
            item[name] = old[name]
    item.update({k: v for k, v in fields.items() if v is not None})
    for k in [k for k, v in fields.items() if v is None]:
        item.pop(k, None)
    return item


def _put(table, item, what):
    try:
        table.put_item(Item=item)
        return True
    except (ClientError, BotoCoreError) as err:
        logger.warning("spot score %s write failed (%s)", what, cache_reads._code(err))
        return False


# ---------------------------------------------------------------- governor
def _json(value, default):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return default
    return parsed if isinstance(parsed, type(default)) else default


def read_budget(table, consistent=False):
    """The governor item as {version, new24h, limitedUntil, index, refusals, seeded}; a fresh state
    when the item does not exist yet; None when it cannot be read (callers then deny new ones)."""
    try:
        kwargs = {"ConsistentRead": True} if consistent else {}
        item = table.get_item(Key={"cacheKey": BUDGET_KEY}, **kwargs).get("Item")
    except (ClientError, BotoCoreError) as err:
        logger.warning("spot score budget read failed (%s)", cache_reads._code(err))
        return None
    item = item or {}
    new24h = [e for e in _json(item.get("new24h"), []) if isinstance(e, dict) and e.get("fp")]
    index = {fp: v for fp, v in _json(item.get("index"), {}).items() if isinstance(v, dict)}
    return {
        "exists": bool(item), "version": _int(item.get("version")), "limitedUntil": _int(item.get("limitedUntil")),
        "new24h": [{**e, "at": _int(e.get("at"))} for e in new24h], "index": index,
        "refusals": [r for r in _json(item.get("refusals"), []) if isinstance(r, dict)],
        "seeded": bool(item.get("seeded")),
    }


def _write_budget(table, state):
    """The conditional write of the governor item: True when no other writer came in between."""
    item = {
        "cacheKey": BUDGET_KEY, "version": state["version"] + 1, "limitedUntil": state["limitedUntil"],
        "new24h": json.dumps(state["new24h"], sort_keys=True), "index": json.dumps(state["index"], sort_keys=True),
        "refusals": json.dumps(state["refusals"][-MAX_REFUSALS_KEPT:], sort_keys=True),
        "seeded": state["seeded"],
    }
    try:
        table.put_item(Item=item, ConditionExpression=BUDGET_CONDITION,
                       ExpressionAttributeValues={":v": state["version"]})
        return True
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.warning("spot score budget write failed (%s)", cache_reads._code(err))
            raise
        return False


def _update_budget(table, change):
    """Read, change and conditionally write the governor item, up to MAX_ATTEMPTS times. `change`
    edits the state in place and returns a result, or None for "no write needed". Returns
    (written, result); (False, None) when the item cannot be read or written."""
    for _ in range(MAX_ATTEMPTS):
        state = read_budget(table, consistent=True)
        if state is None:
            return False, None
        result = change(state)
        if result is None:
            return False, None
        try:
            if _write_budget(table, state):
                return True, result
        except (ClientError, BotoCoreError):
            return False, None
    logger.warning("spot score budget stayed contended after %d attempts", MAX_ATTEMPTS)
    return False, None


def window(state, now):
    """The new configurations of the last COUNT_SECONDS (24 hours and the release margin)."""
    return [e for e in state["new24h"] if 0 <= now - e["at"] < COUNT_SECONDS]


def daily_cap(state, budget, now):
    """The deploy-time budget, lowered for OBSERVED_CAP_SECONDS after EC2 refused a new
    configuration that came after at least OBSERVED_MIN_ACCEPTED new ones this deployment had
    accepted in the 24 hours before it: then at most that count (never below
    OBSERVED_MIN_ACCEPTED). A refusal after fewer is treated as a budget used up elsewhere and
    leaves the cap unchanged. Never raised above the setting, never learned by extra requests."""
    cap = budget.per_day
    seen = [_int(r.get("new24h")) for r in state["refusals"]
            if r.get("new") and _int(r.get("new24h")) >= OBSERVED_MIN_ACCEPTED
            and 0 <= now - _int(r.get("at")) < OBSERVED_CAP_SECONDS]
    if seen:
        cap = min(cap, max(OBSERVED_MIN_ACCEPTED, min(seen)))
    return cap


def free_slots(state, budget, now, priority=GRANT):
    """New configurations this caller may still create now: 0 during the cool-down and for
    REPEAT_ONLY."""
    if priority == REPEAT_ONLY or state["limitedUntil"] > now:
        return 0
    return max(0, daily_cap(state, budget, now) - len(window(state, now)))


def active_count(table, state, now, known=None, exclude=()):
    """Distinct configurations in use: the indexed ones used in the last COUNT_SECONDS or whose
    entry cannot be read (fail closed), and the new ones of the window (a kept slot whose request
    may have counted without an accepted answer), except `exclude` (the ones about to be counted
    again as new). `known` ({fp: entry, None when missing, or UNREADABLE}) is filled with the
    entries read here, so a retry reads only new index entries."""
    known = {} if known is None else known
    missing = [fp for fp in state["index"] if fp not in known]
    if missing:
        unread = []
        found = read_entries(table, missing, unread)
        for fp in missing:
            known[fp] = UNREADABLE if fp in unread else found.get(fp)
    used = {fp for fp in state["index"] if known.get(fp) is UNREADABLE or in_use(known.get(fp), now)}
    used |= {e["fp"] for e in window(state, now)}
    return len(used - set(exclude))


def _prune_index(state, known, now):
    """Drop index entries whose registry item was read and is gone (expired) and that are not in
    the window. An entry whose read failed (UNREADABLE) stays."""
    counted = {e["fp"] for e in window(state, now)}
    for fp in [fp for fp in state["index"] if fp in known and known[fp] is None and fp not in counted]:
        del state["index"][fp]


def grant(table, cfgs, priority, budget, now, known=None):
    """The fingerprints of `cfgs` (configurations that are not warm) that may be sent now. A
    configuration counted as new less than WARM_SECONDS ago needs no new slot; the others take one
    slot each, in order, while slots are free and fewer than budget.max_configs configurations
    are in use. The slots are taken in one conditional write before anything is sent; when the
    governor item cannot be read or written, nothing new is granted (fail closed)."""
    cfgs = list({c.fp: c for c in cfgs}.values())
    if priority == REPEAT_ONLY or not cfgs:
        return set()
    # A None of the caller may be a failed read: those are read again here, where a failure is
    # told apart from a missing item.
    entries = {fp: e for fp, e in (known or {}).items() if e is not None}

    def take(state):
        take.result = set()
        if state["limitedUntil"] > now:
            return None
        # Counted as new less than WARM_SECONDS ago: AWS still counts it in its rolling 24 hours,
        # so it goes again without a new slot. An older entry may already have left AWS's 24
        # hours, so that configuration takes a new slot (the old one still holds its slot until
        # COUNT_SECONDS, which keeps the count on the safe side).
        counted = {e["fp"] for e in state["new24h"] if 0 <= now - e["at"] < WARM_SECONDS}
        again = {c.fp for c in cfgs if c.fp in counted}
        todo = [c for c in cfgs if c.fp not in counted]
        room = free_slots(state, budget, now, priority)
        if todo and room:
            # The most configurations in use at once; one about to be chosen is not also counted
            # as one in use.
            room = min(room, budget.max_configs - active_count(table, state, now, entries, exclude={c.fp for c in todo}))
        chosen = todo[:max(0, room)]
        if not chosen:
            # Nothing to write; the earlier-counted ones still go (a repeat inside the window).
            take.result = again
            return None
        state["new24h"] = window(state, now) + [{"fp": c.fp, "at": now, "kind": c.kind, "type": c.itype} for c in chosen]
        _prune_index(state, entries, now)
        for c in chosen:
            state["index"][c.fp] = {"t": c.itype, "k": c.kind}
        return again | {c.fp for c in chosen}

    take.result = set()
    written, result = _update_budget(table, take)
    if written:
        return result
    return take.result


def release(table, fps, now):
    """Give back the slots taken at `now` for new configurations whose request EC2 did not process
    (not_processed: it did not take them as configurations)."""
    fps = set(fps)

    def drop(state):
        kept = [e for e in state["new24h"] if not (e["fp"] in fps and e["at"] == now)]
        if len(kept) == len(state["new24h"]):
            return None
        state["new24h"] = kept
        return True
    _update_budget(table, drop)


def reindex(table, cfgs):
    """Put accepted configurations (back) in the index, so they count toward budget.max_configs
    (for example one pruned after a read that looked like an expired item)."""
    def add(state):
        added = [c for c in cfgs if c.fp not in state["index"]]
        for c in added:
            state["index"][c.fp] = {"t": c.itype, "k": c.kind}
        return True if added else None
    _update_budget(table, add)


def cool_down(table, cfgs, now, new):
    """After limit refusals: no new configuration for BLOCK_SECONDS, and each refusal is kept with
    the count of the other new configurations in the window (`new`: the refused fingerprints that
    were new ones; a refused warm one does not lower the cap)."""
    refused = {c.fp for c in cfgs}

    def mark(state):
        state["limitedUntil"] = max(state["limitedUntil"], now + BLOCK_SECONDS)
        count = len([e for e in window(state, now) if e["fp"] not in refused])
        state["refusals"] = state["refusals"] + [{"at": now, "fp": c.fp, "new24h": count, "new": c.fp in new} for c in cfgs]
        for c in cfgs:
            state["index"].setdefault(c.fp, {"t": c.itype, "k": c.kind})
        return True
    written, _ = _update_budget(table, mark)
    if not written:
        logger.warning("spot score cool-down could not be stored")


def budget_view(table, budget, now):
    """The governor's numbers for /api/config (no EC2 call): spotScoreConfigsPerDay, the
    configurations in use in the last 24 hours, the new ones still allowed now, and the end of a
    cool-down after a refusal by AWS."""
    state = read_budget(table) if table is not None else None
    if state is None:
        return {"spotScoreConfigsPerDay": budget.max_configs, "spotScoreConfigsInUse": None,
                "remainingNewConfigs": 0, "spotScoreLimitedUntil": None}
    limited = state["limitedUntil"] if state["limitedUntil"] > now else None
    in_use = active_count(table, state, now)
    return {
        "spotScoreConfigsPerDay": budget.max_configs,
        "spotScoreConfigsInUse": in_use,
        "remainingNewConfigs": max(0, min(free_slots(state, budget, now), budget.max_configs - in_use)),
        "spotScoreLimitedUntil": _iso(limited) if limited else None,
    }


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(stamp):
    """Epoch seconds of a cache item's fetchedAt, or None."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return int(datetime.strptime(str(stamp), fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


# -------------------------------------------------------------------- seed
def seed(table, now, endpoint):
    """Register, once, the configurations today's items show EC2 accepted: each type's stored
    Region set (#sps-regions, sent as spot_cache.set_chunks gives it) whose scores item exists
    with a fetch time in the last WARM_SECONDS. No EC2 call. Idempotent: an existing registry
    item is never overwritten. Returns the governor state (None when unreadable)."""
    state = read_budget(table)
    if state is None or state["seeded"]:
        return state
    sets = spot_cache.read_region_sets(table)
    cfgs = []
    for itype, entry in sorted(sets.items()):
        if not isinstance(entry, dict) or not entry.get("regions"):
            continue
        try:
            cfgs += [build(itype, chunk, endpoint) for chunk in spot_cache.set_chunks(entry)]
        except (TypeError, ValueError):
            continue
    items = cache_reads.get_items(table, [c.scores_key for c in cfgs]) if cfgs else {}
    seeded = {}
    for cfg in cfgs:
        at = parse_stamp((items.get(cfg.scores_key) or {}).get("fetchedAt"))
        if at is None or not 0 <= now - at < WARM_SECONDS:
            continue
        item = _entry_item(cfg, None, now, firstAcceptedAt=at, lastUsedAt=at, lastResult=ACCEPTED)
        item["source"] = "seed"
        try:
            table.put_item(Item=item, ConditionExpression=NEW_ITEM_CONDITION)
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                logger.warning("spot score seed write failed (%s)", cache_reads._code(err))
                continue
        except BotoCoreError:
            continue
        seeded[cfg.fp] = {"t": cfg.itype, "k": cfg.kind}

    def mark(state):
        if state["seeded"]:
            return None
        state["seeded"] = True
        for fp, v in seeded.items():
            state["index"].setdefault(fp, v)
        return True
    _update_budget(table, mark)
    if seeded:
        logger.info("spot score registry seeded with %d known configurations", len(seeded))
    return read_budget(table)


# ------------------------------------------------------------------ ledger
class Ledger:
    """One request's (or one updater run's) view of the registry and the governor. Not shared
    between threads: plan before the calls, record after them."""

    def __init__(self, table, now, endpoint, budget=DEFAULT_BUDGET):
        self.table, self.now, self.endpoint, self.budget = table, now, endpoint, budget
        self.state = seed(table, now, endpoint)
        self.entries = {}
        self.granted = set()

    def load(self, fps):
        missing = [fp for fp in dict.fromkeys(fps) if fp not in self.entries]
        if missing:
            found = read_entries(self.table, missing)
            for fp in missing:
                self.entries[fp] = found.get(fp)

    def entry(self, fp):
        self.load([fp])
        return self.entries.get(fp)

    def warm(self, cfg):
        return is_warm(self.entry(cfg.fp), self.now)

    def plan(self, cfgs, priority):
        """The fingerprints of `cfgs` that may be sent now: the warm ones, plus (GRANT only) the new
        or cold ones the governor grants. Blocked ones never go before their blockedUntil, and
        rejected ones (rejected()) not before their retryAfter."""
        cfgs = list({c.fp: c for c in cfgs}.values())
        self.load([c.fp for c in cfgs])
        warm = {c.fp for c in cfgs if is_warm(self.entries.get(c.fp), self.now)}
        cold = [c for c in cfgs if c.fp not in warm and not is_blocked(self.entries.get(c.fp), self.now)
                and not waits_after_error(self.entries.get(c.fp), self.now)]
        granted = grant(self.table, cold, priority, self.budget, self.now, known=self.entries) if cold else set()
        self.granted |= granted
        if self.state is not None:
            # grant() indexed them in the governor item.
            for c in cold:
                if c.fp in granted:
                    self.state["index"].setdefault(c.fp, {"t": c.itype, "k": c.kind})
        return warm | granted

    def _outcome(self, cfg, outcome):
        """(registry item, kind) for one answer or exception; kind is accepted, refused, failed,
        or None (an outcome that says nothing about the configuration, such as a timeout)."""
        old = self.entries.get(cfg.fp)
        if not isinstance(outcome, BaseException):
            item = _entry_item(cfg, old, self.now, firstAcceptedAt=(old or {}).get("firstAcceptedAt") or self.now,
                               lastUsedAt=self.now, lastResult=ACCEPTED, lastErrorCode=None, blockedUntil=None,
                               retryAfter=None)
            return item, "accepted"
        if not isinstance(outcome, (ClientError, BotoCoreError)):
            return None, None
        code = outcome.response.get("Error", {}).get("Code", "") if isinstance(outcome, ClientError) else type(outcome).__name__
        if is_limit_error(outcome):
            if is_warm(old, self.now):
                # The registry was wrong about what EC2 counts as a known configuration.
                logger.warning("spot score ledger mismatch: EC2 refused a known configuration for %s", cfg.itype)
            return _entry_item(cfg, old, self.now, lastResult=BLOCKED, lastErrorCode=code,
                               blockedUntil=self.now + BLOCK_SECONDS), "refused"
        if rejected(outcome) and not is_warm(old, self.now):
            logger.warning("EC2 rejected the new Spot score configuration of %s (%s); not sent again for %d hours. "
                           "If a size of the group has no Spot placement scores, narrow spotScoreFamilies.",
                           cfg.itype, code, ERROR_RETRY_SECONDS // 3600)
            return _entry_item(cfg, old, self.now, lastResult=ERROR, lastErrorCode=code,
                               retryAfter=self.now + ERROR_RETRY_SECONDS), "failed"
        return _entry_item(cfg, old, self.now, lastResult=ERROR, lastErrorCode=code), "failed"

    def record_all(self, results, mapper=None):
        """Store the outcomes [(cfg, answer or exception)] of send()/fetch() calls. Accepted: the
        entry is warm from now. Refused for the limit: blocked for BLOCK_SECONDS, and the cool-down
        starts (no new configuration for BLOCK_SECONDS; no retry). Another error: the entry keeps its
        warmth, and a slot this request took for it is given back only when EC2 did not process
        the request (not_processed); a new or cold one that EC2 rejected (rejected()) is not
        granted again for ERROR_RETRY_SECONDS. An accepted configuration that is not in the index is added
        back. The registry items are written
        through `mapper` (map-like, for example a thread pool); the governor item once."""
        items, refused, freed, accepted = [], [], [], []
        self.load([cfg.fp for cfg, _ in results])
        for cfg, outcome in results:
            item, kind = self._outcome(cfg, outcome)
            if item is None:
                continue
            self.entries[cfg.fp] = parse_entry(item)
            items.append(item)
            if kind == "accepted":
                accepted.append(cfg)
            elif kind == "refused":
                refused.append(cfg)
            elif kind == "failed" and cfg.fp in self.granted and not_processed(outcome):
                self.granted.discard(cfg.fp)
                freed.append(cfg.fp)
        list((mapper or map)(lambda item: _put(self.table, item, "registry"), items))
        if refused:
            cool_down(self.table, refused, self.now, new={c.fp for c in refused if c.fp in self.granted})
            if self.state is not None:
                self.state["limitedUntil"] = max(self.state["limitedUntil"], self.now + BLOCK_SECONDS)
        if freed:
            release(self.table, freed, self.now)
        unindexed = [c for c in accepted if self.state is None or c.fp not in self.state["index"]]
        if unindexed:
            reindex(self.table, unindexed)
            if self.state is not None:
                for c in unindexed:
                    self.state["index"].setdefault(c.fp, {"t": c.itype, "k": c.kind})

    def record(self, cfg, outcome):
        self.record_all([(cfg, outcome)])

    def accepted(self, cfg):
        self.record_all([(cfg, [])])
