"""Scheduled Spot placement score updater: the only code that changes the Spot score groups or
grants new request configurations. EventBridge runs it every 15 minutes, at minutes 0, 15, 30 and 45
(never faster); the API also starts it once, asynchronously, when the groups must be built
(bootstrap). One run at a time (a lock item).

Each run decides what to do:

* BOOTSTRAP (no stored groups yet, or a deploy changed spotScoreFamilies) and DAILY UPDATE (the
  first run at or after 00:30 UTC each day): refresh the enabled Region list, what each Region
  sells of the scored families, and the GPU count of new sizes; add new Regions, sizes and groups
  to the stored lists (append-only, score_groups.merge); remove excluded Regions. A Region whose
  lookup failed changes nothing.
* Then every run is a WARM run: repeat every granted configuration (free) whose cached answer is
  older than the refresh interval, and grant PLANNED configurations through the governor when
  there is room (no membership change). Answers go to the shared cache items and the score
  history; the page, the Refresh button and the email alerts read them. The latest Spot prices of
  every Region (one call per Region) are stored too.

Old configurations of earlier designs are not part of any current group, so they are never sent
again. A refusal by AWS blocks new grants for 24 hours (spot_configs); warm repeats go on."""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import cache_reads
import families
import region_slots
import regions as region_list
import score_groups
import spot_cache
import spot_configs
import spot_prices

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("OFFERING_CACHE_TABLE_NAME", "")
SPOT_SCORE_FAMILIES = families.score_patterns_or_default(os.environ.get("SPOT_SCORE_FAMILIES", "").split(","))
CONFIGS_PER_DAY = int(os.environ.get("SPOT_SCORE_CONFIGS_PER_DAY", "40"))
BUDGET = spot_configs.Budget(CONFIGS_PER_DAY, CONFIGS_PER_DAY)
# Minutes an answer stays current: a warm run repeats a configuration whose answer is older than
# this less REPEAT_SLACK_SECONDS (so a run that starts a few seconds early still repeats it), and
# the cache items live GRACE_MINUTES longer, so the page never sees a gap between two runs.
CACHE_MINUTES = int(os.environ.get("SPOT_SCORE_CACHE_MINUTES", "15"))
REPEAT_SLACK_SECONDS = 300
GRACE_MINUTES = 10
# The function's own Region: DescribeRegions is asked there.
HOME_REGION = os.environ.get("AWS_REGION") or score_groups.ENDPOINT
LOCK_KEY = "#lock#score-updater"
LOCK_SECONDS = 6 * 60
WORKERS = 8
# DescribeInstanceTypes takes at most 100 names per call.
TYPES_PER_CALL = 100
EC2_CONFIG = Config(retries={"mode": "standard", "total_max_attempts": 3}, connect_timeout=3, read_timeout=15)

_clients = {}
_table = None


def client(region):
    if region not in _clients:
        _clients[region] = boto3.client("ec2", region_name=region, config=EC2_CONFIG)
    return _clients[region]


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def _epoch():
    return int(time.time())


def _stamp(now):
    """The ISO 8601 UTC time of `now` (the run's clock), as the cache items store it."""
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _code(err):
    return err.response.get("Error", {}).get("Code", "") if isinstance(err, ClientError) else type(err).__name__


def _map(fn, items):
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


# -------------------------------------------------------------------- lock
def claim(now):
    try:
        table().put_item(
            Item={"cacheKey": LOCK_KEY, "lockedUntil": now + LOCK_SECONDS, "expires": now + LOCK_SECONDS + 86400},
            ConditionExpression="attribute_not_exists(cacheKey) OR lockedUntil <= :now",
            ExpressionAttributeValues={":now": now})
        return True
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.warning("score updater lock failed (%s)", _code(err))
        return False


def release(now):
    try:
        table().put_item(Item={"cacheKey": LOCK_KEY, "lockedUntil": 0, "expires": now + 86400})
    except (ClientError, BotoCoreError) as err:
        logger.warning("score updater lock release failed (%s)", _code(err))


# ------------------------------------------------------------ daily update
def enabled_regions(now):
    """The enabled Regions minus the excluded ones: a live DescribeRegions (stored for every
    feature), else the stored list; None when neither answers."""
    try:
        names = region_list.describe(client(HOME_REGION))
        if names:
            return region_list.store_live(table(), names, now)[0]
    except (ClientError, BotoCoreError, KeyError, TypeError) as err:
        logger.warning("Region discovery failed (%s); using the stored list", _code(err))
    cached = region_list.read_item(table(), now)
    return region_list.order(cached["regions"]) if cached else None


def region_offerings(region, patterns):
    """The scored instance types the Region sells (Region-level offerings). Raises on failure."""
    pager = client(region).get_paginator("describe_instance_type_offerings")
    found = set()
    for page in pager.paginate(LocationType="region", Filters=[{"Name": "instance-type", "Values": list(patterns)}]):
        found.update(o["InstanceType"] for o in page["InstanceTypeOfferings"])
    return sorted(t for t in found if families.scored(t, patterns))


def offerings(names, patterns):
    """{region: [scored types]} for the Regions whose lookup answered; a failed one is left out."""
    def one(region):
        try:
            return region, region_offerings(region, patterns)
        except (ClientError, BotoCoreError) as err:
            logger.warning("offering lookup failed in %s (%s); its groups stay as they are", region, _code(err))
            return region, None
    return {r: types for r, types in _map(one, names) if types is not None}


# A type whose GPU count lookup fails in one selling Region is asked in the next one, at most in
# this many Regions per daily update.
GPU_LOOKUP_REGIONS = 3


def gpu_counts(offered, known):
    """{type: GPU count} for the offered types without a stored count: DescribeInstanceTypes in a
    Region that sells them (GpuInfo.Gpus[0].Count; 0 for a type without GPU information). When the
    call fails in one Region, its types are asked in the next Region that sells them (Regions in
    name order, at most GPU_LOOKUP_REGIONS); a type that fails everywhere stays unknown until the
    next daily update."""
    sellers = {}
    for region in sorted(offered):
        for itype in offered[region]:
            if itype not in known:
                sellers.setdefault(itype, []).append(region)

    def one(batch):
        region, types = batch
        try:
            out = {}
            for page in client(region).get_paginator("describe_instance_types").paginate(InstanceTypes=types):
                for t in page["InstanceTypes"]:
                    gpus = (t.get("GpuInfo") or {}).get("Gpus") or [{}]
                    out[t["InstanceType"]] = int(gpus[0].get("Count") or 0)
            return types, out
        except (ClientError, BotoCoreError, KeyError, TypeError, ValueError) as err:
            logger.warning("GPU count lookup failed in %s (%s)", region, _code(err))
            return types, None
    found, failed = {}, set(sellers)
    for attempt in range(GPU_LOOKUP_REGIONS):
        by_region = {}
        for itype in sorted(failed):
            if attempt < len(sellers[itype]):
                by_region.setdefault(sellers[itype][attempt], []).append(itype)
        if not by_region:
            break
        batches = [(r, ts[i:i + TYPES_PER_CALL]) for r, ts in sorted(by_region.items())
                   for i in range(0, len(ts), TYPES_PER_CALL)]
        failed = set()
        for types, part in _map(one, batches):
            if part is None:
                failed.update(types)
            else:
                found.update(part)
    return found


def daily_update(state, now, patterns=None):
    """The state after one daily update (or bootstrap), stored with a conditional write. When no
    Region list or no offering lookup answers, the stored groups stay as they are and the update is
    tried again at the next run."""
    patterns = list(patterns or SPOT_SCORE_FAMILIES)
    names = enabled_regions(now)
    if not names:
        logger.warning("daily update skipped: no Region list")
        return state, False
    found_offers = offerings(names, patterns)
    if not found_offers:
        logger.warning("daily update skipped: no offering lookup answered")
        return state, False
    gpus = {**state["gpus"], **gpu_counts(found_offers, state["gpus"])}
    seen = region_slots.read_first_seen(table())
    first_seen = seen[1] if seen else {}
    groups = score_groups.merge(state["groups"], score_groups.collect(found_offers, gpus, patterns), patterns,
                                region_list.EXCLUDED, first_seen, now)
    offered = {r: ts for r, ts in state["offered"].items() if r in names and r not in region_list.EXCLUDED}
    offered.update(found_offers)
    new = {**state, "families": score_groups.signature(patterns), "groups": groups, "gpus": gpus,
           "offered": offered, "dailyAt": now}
    if not score_groups.write_state(table(), new, now):
        # Not stored (another writer, or a failed write): the run goes on with the stored groups, so
        # nothing is granted for a membership that was never stored.
        logger.warning("daily update not stored; the stored groups stay as they are")
        return state, False
    new["version"] = state["version"] + 1
    logger.info("daily update: %d groups, %d configurations, %d Regions answered of %d",
                len(groups), sum(len(g["chunks"]) for g in groups.values()), len(found_offers), len(names))
    return new, True


# ---------------------------------------------------------------- warm run
def _fetched_epoch(entry):
    return spot_cache.stamp_epoch((entry or {}).get("fetchedAt")) or 0


def _warm_counts(configurations):
    return {"configurations": configurations, "repeated": 0, "granted": 0, "planned": 0, "failed": 0, "limited": 0,
            "current": 0}


def warm(state, now):
    """Repeat the granted configurations whose answer is due and grant planned ones when there is
    room; store the answers and one history reading per group. Returns the run's counts."""
    pairs = score_groups.configs(state["groups"])
    counts = _warm_counts(len(pairs))
    if not pairs:
        return counts
    items = cache_reads.get_items(table(), [c.scores_key for _, c in pairs])
    cached = {c.fp: spot_cache.parse_item(items.get(c.scores_key), list(c.regions), now)[0] for _, c in pairs}
    repeat_after = max(score_groups.WARM_MINUTES * 60, CACHE_MINUTES * 60) - REPEAT_SLACK_SECONDS
    due = [(k, c) for k, c in pairs if cached[c.fp] is None or now - _fetched_epoch(cached[c.fp]) >= repeat_after]
    counts["current"] = len(pairs) - len(due)
    # New grants go to the first chunk of every group before any second chunk, so a short budget
    # still gives every group some Regions.
    position = {}
    for k, c in pairs:
        position[c.fp] = (sum(1 for _, o in pairs if o.itype == k and o.fp in position), len(position))
    due.sort(key=lambda kc: position[kc[1].fp])
    ledger = spot_configs.Ledger(table(), now, score_groups.ENDPOINT, BUDGET)
    ledger.load([c.fp for _, c in due])
    warm_fps = {c.fp for _, c in due if ledger.warm(c)}
    allowed = ledger.plan([c for _, c in due], spot_configs.GRANT)
    counts["planned"] = sum(1 for _, c in due if c.fp not in allowed)
    send = [c for _, c in due if c.fp in allowed]
    ec2 = client(score_groups.ENDPOINT)

    def one(cfg):
        try:
            return cfg, spot_configs.fetch(ec2, cfg)
        except (ClientError, BotoCoreError) as err:
            return cfg, err
    outcomes = _map(one, send)
    ledger.record_all(outcomes, mapper=_map)
    # Stamped when the answers arrived, not when the run started: a slow run must not store an
    # older fetchedAt over the answer of a user Refresh stored while it ran.
    stamp = _stamp(max(now, _epoch()))
    fresh = {}
    for cfg, outcome in outcomes:
        if isinstance(outcome, BaseException):
            counts["limited" if spot_configs.is_limit_error(outcome) else "failed"] += 1
            logger.warning("Spot placement score request failed for %s (%s)", cfg.itype, _code(outcome))
            continue
        counts["repeated" if cfg.fp in warm_fps else "granted"] += 1
        fresh[cfg.fp] = outcome
    _map(lambda c: spot_cache.write(table(), c.itype, list(c.regions), fresh[c.fp], stamp, now,
                                    CACHE_MINUTES + GRACE_MINUTES), [c for c, _ in outcomes if c.fp in fresh])
    # One history reading per group that got a new answer: this run's answers, plus the current
    # cached answers of its other chunks.
    readings = {}
    for key, cfg in pairs:
        part = fresh.get(cfg.fp)
        if part is None and cached[cfg.fp] is not None:
            part = cached[cfg.fp]["scores"]
        if part:
            readings.setdefault(key, {}).update(part)
    changed = {cfg.itype for cfg, _ in outcomes if cfg.fp in fresh}
    _map(lambda k: spot_cache.append_history(table(), spot_cache.history_key(k, spot_cache.HISTORY_REGIONS),
                                             readings[k], stamp, now), [k for k in readings if k in changed])
    return counts


def prices(state, now):
    """The latest Spot prices of every Region that sells a scored type: one call per Region."""
    names = [r for r in sorted(state["offered"]) if r not in region_list.EXCLUDED and state["offered"][r]]
    stamp = _stamp(now)

    def one(region):
        try:
            spot_prices.write(table(), region, spot_prices.fetch(client(region), state["offered"][region]), stamp, now)
            return True
        except (ClientError, BotoCoreError) as err:
            logger.warning("Spot price lookup failed in %s (%s)", region, _code(err))
            return False
    done = _map(one, names)
    return {"regions": len(names), "failed": done.count(False)}


def handler(_event, _context):
    now = _epoch()
    if not claim(now):
        logger.info("score updater: another run holds the lock")
        return {"skipped": "locked"}
    try:
        state = score_groups.read_state(table(), consistent=True)
        if state is None:
            return {"skipped": "state-unreadable"}
        mode = "warm"
        if score_groups.needs_bootstrap(state, SPOT_SCORE_FAMILIES):
            mode = "bootstrap"
        elif score_groups.daily_due(state, now):
            # Membership changes only at the scheduled daily update and the bootstrap; the event
            # (the schedule's, or the API's {"mode": "bootstrap"} start) does not choose the mode.
            mode = "daily"
        updated = False
        if mode != "warm":
            state, updated = daily_update(state, now)
        # A bootstrap that stored nothing leaves the old groups (none, or those of the previous
        # family list): scoring them would spend the configuration budget on groups that the next
        # bootstrap replaces.
        if mode == "bootstrap" and not updated:
            scored = {**_warm_counts(0), "warmSkipped": "bootstrap-not-stored"}
        else:
            scored = warm(state, now)
        counts = {"mode": mode, "updated": updated, **scored, "prices": prices(state, now)}
    finally:
        release(now)
    logger.info("score updater: %s", json.dumps(counts, sort_keys=True))
    return counts
