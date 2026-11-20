"""Spot placement score request configurations (spot_configs): the one request builder, the
registry, the governor (no click reserve), the seed from today's items, and a simulation of the
score updater and the page against a fake EC2 that restricts different configurations per 24
hours. Fake tables and clients only; nothing reaches AWS, and no test sends
GetSpotPlacementScores anywhere."""
import json
import os
import random
import threading

os.environ.update(SPOT_SCORE_FAMILIES="p5*,g6e.*", TABLE_NAME="t")

import api  # noqa: E402
import cache_fakes  # noqa: E402
import pytest  # noqa: E402
import regions as region_list  # noqa: E402
import score_groups as sg  # noqa: E402
import score_updater as su  # noqa: E402
import spot_cache  # noqa: E402
import spot_configs  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

NOW = 2_000_000_000
DAY = 86400
REGIONS = ["us-east-1", "us-east-2", "us-west-2", "eu-west-1"]
LIMIT = {"Error": {"Code": "MaxConfigLimitExceeded",
                   "Message": "You've exceeded your maximum allowed Spot placement configurations."}}


class Table:
    """The cache table: GetItem/PutItem by cacheKey with DynamoDB's conditional writes. Thread-safe."""

    def __init__(self):
        self.items = {}
        self.fail_reads = False

    def get_item(self, Key, ConsistentRead=False):
        if self.fail_reads:
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
        item = self.items.get(Key["cacheKey"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        cache_fakes.conditional_put(self.items, Item, ConditionExpression, ExpressionAttributeValues)


# ------------------------------------------------------------------ builder
def test_equal_logical_requests_are_byte_identical():
    a = spot_configs.build("p5.48xlarge", ["us-east-1", "us-west-2"], "us-east-1")
    b = spot_configs.build("p5.48xlarge", ("us-east-1", "us-west-2"), "us-east-1")
    assert a == b and a.request == b.request and a.fp == b.fp
    assert json.dumps(a.kwargs) == json.dumps(b.kwargs)
    assert a.request == '{"InstanceTypes":["p5.48xlarge"],"RegionNames":["us-east-1","us-west-2"],"TargetCapacity":1}'
    # The type list is sorted and unique.
    assert spot_configs.request_kwargs(["p5.48xlarge", "p4d.24xlarge", "p5.48xlarge"], ["us-east-1"])["InstanceTypes"] == \
        ["p4d.24xlarge", "p5.48xlarge"]


def test_region_order_and_endpoint_are_part_of_the_configuration():
    base = spot_configs.build("p5.48xlarge", ["us-east-1", "us-west-2"], "us-east-1")
    assert spot_configs.build("p5.48xlarge", ["us-west-2", "us-east-1"], "us-east-1").fp != base.fp
    assert spot_configs.build("p5.48xlarge", ["us-east-1", "us-west-2"], "us-west-2").fp != base.fp
    # The scores item of a Region-level request is independent of the order.
    assert base.scores_key == spot_cache.cache_key("p5.48xlarge", ["us-west-2", "us-east-1"])
    # Zone-level requests are gone.
    with pytest.raises(TypeError):
        spot_configs.build("p5.48xlarge", ["us-east-1"], "us-east-1", az=True)


def test_target_capacity_is_always_one_and_nothing_else_is_sent():
    kw = spot_configs.build("g6e/4", ["us-east-1"], "us-east-1", types=["g6e.24xlarge", "g6e.12xlarge"]).kwargs
    assert kw == {"InstanceTypes": ["g6e.12xlarge", "g6e.24xlarge"], "TargetCapacity": 1, "RegionNames": ["us-east-1"]}
    assert kw["TargetCapacity"] == spot_configs.TARGET_CAPACITY == 1


class Pages:
    def __init__(self):
        self.sent = []

    def get_paginator(self, name):
        assert name == "get_spot_placement_scores"
        outer = self

        class P:
            def paginate(self, **kw):
                outer.sent.append(kw)
                return [{"SpotPlacementScores": [{"Region": "us-east-1", "Score": 4}]}]
        return P()


def test_send_passes_exactly_the_stored_request_and_refuses_a_tampered_one():
    cfg = spot_configs.build("p5.48xlarge", ["us-east-1"], "us-east-1")
    ec2 = Pages()
    assert spot_configs.fetch(ec2, cfg) == {"us-east-1": 4}
    assert ec2.sent == [cfg.kwargs] and spot_configs.canonical(ec2.sent[0]) == cfg.request
    with pytest.raises(ValueError):
        spot_configs.send(ec2, cfg._replace(kwargs={**cfg.kwargs, "TargetCapacity": 2}))
    assert len(ec2.sent) == 1


def test_the_updater_the_api_and_the_notifier_build_through_the_one_builder():
    """Every function builds a group's configuration from the stored groups (score_groups)."""
    groups = {"g6e/4": {"types": ["g6e.12xlarge", "g6e.24xlarge"], "chunks": [["us-west-2", "us-east-1"]], "added": {}}}
    [(_, cfg)] = sg.configs(groups)
    assert sg.config_for(groups, "g6e/4", "us-east-1") == cfg
    assert cfg == spot_configs.build("g6e/4", ["us-west-2", "us-east-1"], "us-east-1",
                                     types=["g6e.24xlarge", "g6e.12xlarge"])


# ----------------------------------------------------------------- governor
CFGS = [spot_configs.build(f"g6e.{n}xlarge", ["us-east-1"], "us-east-1") for n in range(1, 31)]


def test_grants_use_the_whole_budget_without_a_reserve():
    t = Table()
    b = spot_configs.Budget(5, 40)
    assert spot_configs.grant(t, CFGS[:7], spot_configs.GRANT, b, NOW) == {c.fp for c in CFGS[:5]}
    assert spot_configs.grant(t, CFGS[5:6], spot_configs.GRANT, b, NOW + 100) == set()
    # Already counted in the window: sent again without a new slot.
    assert spot_configs.grant(t, CFGS[:2], spot_configs.GRANT, b, NOW + 200) == {c.fp for c in CFGS[:2]}
    # The window rolls: 24 hours and the release margin after the first slots they are free again,
    # not at exactly 24 hours (the run's stamp comes before the request reached AWS).
    assert spot_configs.grant(t, CFGS[5:6], spot_configs.GRANT, b, NOW + DAY) == set()
    later = NOW + spot_configs.COUNT_SECONDS
    assert spot_configs.grant(t, CFGS[5:6], spot_configs.GRANT, b, later) == {CFGS[5].fp}
    assert len(spot_configs.window(spot_configs.read_budget(t), later)) == 1


def test_a_slot_older_than_the_warm_time_does_not_count_the_configuration_again():
    """Granted 24 h 5 min ago and never accepted: AWS may have dropped it from its rolling
    24 hours, so a send now is new there and takes a new slot (or is denied without one)."""
    old = NOW - DAY - 300
    t = Table()
    assert spot_configs.grant(t, CFGS[:1], spot_configs.GRANT, spot_configs.Budget(1, 40), old) == {CFGS[0].fp}
    # The old slot still holds the only slot: no new slot, so it is denied.
    assert spot_configs.grant(t, CFGS[:1], spot_configs.GRANT, spot_configs.Budget(1, 40), NOW) == set()
    assert len(spot_configs.read_budget(t)["new24h"]) == 1
    # With a free slot it takes one; both slots count until each one's window ends.
    assert spot_configs.grant(t, CFGS[:1], spot_configs.GRANT, spot_configs.Budget(2, 40), NOW) == {CFGS[0].fp}
    state = spot_configs.read_budget(t)
    assert sorted(e["at"] for e in state["new24h"]) == [old, NOW]
    assert spot_configs.free_slots(state, spot_configs.Budget(2, 40), NOW) == 0
    # Less than the warm time after the new slot it goes again without another one.
    assert spot_configs.grant(t, CFGS[:1], spot_configs.GRANT, spot_configs.Budget(2, 40),
                              NOW + spot_configs.WARM_SECONDS - 1) == {CFGS[0].fp}
    later = NOW + spot_configs.WARM_SECONDS - 1
    assert len(spot_configs.window(spot_configs.read_budget(t), later)) == 1  # the old slot has left


def test_page_loads_and_refreshes_never_get_a_slot():
    t = Table()
    assert spot_configs.grant(t, CFGS[:1], spot_configs.REPEAT_ONLY, spot_configs.DEFAULT_BUDGET, NOW) == set()
    assert "#sps-budget" not in t.items


def test_max_configurations_in_use():
    t = Table()
    b = spot_configs.Budget(20, 3)
    for cfg in CFGS[:2]:
        cache_fakes.register(t.items, cfg, NOW - 60)  # two warm ones in use
    assert spot_configs.grant(t, CFGS[2:6], spot_configs.GRANT, b, NOW) == {CFGS[2].fp}


def test_a_configuration_chosen_again_is_not_also_counted_in_use():
    """Used 23.5 hours ago: not warm (so it asks for a slot) but in use; it is not counted twice."""
    t = Table()
    b = spot_configs.Budget(20, 2)
    cache_fakes.register(t.items, CFGS[0], NOW - 60)
    cache_fakes.register(t.items, CFGS[1], NOW - int(23.5 * 3600))
    assert spot_configs.grant(t, CFGS[1:2], spot_configs.GRANT, b, NOW) == {CFGS[1].fp}


def test_the_cool_down_denies_every_new_configuration():
    t = Table()
    led = spot_configs.Ledger(t, NOW, "us-east-1")
    assert led.plan([CFGS[0]], spot_configs.GRANT) == {CFGS[0].fp}
    led.record(CFGS[0], ClientError(LIMIT, "GetSpotPlacementScores"))
    state = spot_configs.read_budget(t)
    assert state["limitedUntil"] == NOW + DAY
    assert state["refusals"] == [{"at": NOW, "fp": CFGS[0].fp, "new24h": 0, "new": True}]
    for at in (NOW + 1, NOW + DAY - 1):
        assert spot_configs.grant(t, CFGS[1:2], spot_configs.GRANT, spot_configs.DEFAULT_BUDGET, at) == set()
    # The refused one stays blocked for the whole day.
    assert spot_configs.Ledger(t, NOW + DAY - 1, "us-east-1").plan([CFGS[0]], spot_configs.GRANT) == set()
    assert spot_configs.Ledger(t, NOW + DAY, "us-east-1").plan([CFGS[0]], spot_configs.GRANT) == {CFGS[0].fp}


def refuse_after(t, b, accepted, at=NOW):
    """`accepted` new configurations EC2 accepted, then one more refused, all at `at`."""
    led = spot_configs.Ledger(t, at, "us-east-1", b)
    cfgs = CFGS[:accepted + 1]
    assert led.plan(cfgs, spot_configs.GRANT) == {c.fp for c in cfgs}
    led.record_all([(c, {}) for c in cfgs[:-1]] + [(cfgs[-1], ClientError(LIMIT, "GetSpotPlacementScores"))])
    return spot_configs.read_budget(t)


def test_a_first_refusal_on_an_empty_ledger_leaves_the_daily_number():
    """Right after a deploy the account's budget may already be used up elsewhere: block and cool
    down, but do not learn a cap from it."""
    b = spot_configs.Budget(20, 40)
    for accepted in (0, 1, spot_configs.OBSERVED_MIN_ACCEPTED - 1):
        state = refuse_after(Table(), b, accepted)
        assert state["limitedUntil"] == NOW + DAY
        assert spot_configs.daily_cap(state, b, NOW + DAY) == 20


def test_a_refusal_after_five_accepted_lowers_the_cap_to_five_for_a_week():
    b = spot_configs.Budget(20, 40)
    state = refuse_after(Table(), b, 5)
    assert spot_configs.daily_cap(state, b, NOW + DAY) == 5
    # The lowered cap is used up by the five accepted ones until they leave the window.
    assert spot_configs.free_slots(state, b, NOW + spot_configs.COUNT_SECONDS - 1) == 0
    assert spot_configs.free_slots(state, b, NOW + spot_configs.COUNT_SECONDS) == 5
    assert spot_configs.daily_cap(state, b, NOW + 7 * DAY) == 20
    # Never below the minimum, even if a stored refusal says fewer.
    state["refusals"].append({"at": NOW, "fp": "x", "new24h": spot_configs.OBSERVED_MIN_ACCEPTED, "new": True})
    assert spot_configs.daily_cap(state, b, NOW + DAY) == spot_configs.OBSERVED_MIN_ACCEPTED >= 3


def test_after_the_cool_down_new_configurations_go_again_at_the_deploy_time_number():
    t = Table()
    b = spot_configs.Budget(6, 40)
    refuse_after(t, b, 0)
    assert spot_configs.grant(t, CFGS[5:9], spot_configs.GRANT, b, NOW + DAY - 1) == set()
    later = CFGS[10:20]
    # The refused one's slot stays counted for the release margin after the cool-down ends.
    assert spot_configs.grant(t, later, spot_configs.GRANT, b, NOW + DAY) == {c.fp for c in later[:5]}
    assert spot_configs.grant(t, later, spot_configs.GRANT, b, NOW + spot_configs.COUNT_SECONDS) == \
        {c.fp for c in later[:6]}


def test_another_error_gives_the_slot_back_and_keeps_warmth():
    t = Table()
    b = spot_configs.Budget(1, 40)
    led = spot_configs.Ledger(t, NOW, "us-east-1", b)
    assert led.plan([CFGS[0]], spot_configs.GRANT) == {CFGS[0].fp}
    led.record(CFGS[0], ClientError({"Error": {"Code": "RequestLimitExceeded", "Message": "slow down"}}, "X"))
    assert spot_configs.window(spot_configs.read_budget(t), NOW) == []
    assert spot_configs.read_budget(t)["limitedUntil"] == 0
    # A warm entry that meets throttling stays warm.
    cache_fakes.register(t.items, CFGS[1], NOW - 60)
    led = spot_configs.Ledger(t, NOW, "us-east-1", b)
    led.record(CFGS[1], ClientError({"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "X"))
    assert spot_configs.Ledger(t, NOW, "us-east-1", b).warm(CFGS[1])


def test_an_unreadable_governor_item_grants_nothing():
    t = Table()
    t.fail_reads = True
    assert spot_configs.grant(t, CFGS[:1], spot_configs.GRANT, spot_configs.DEFAULT_BUDGET, NOW) == set()


def test_two_copies_racing_for_the_last_slot_get_one_slot():
    """Both read the governor item at the same version; the conditional write lets one through,
    the other re-reads, finds no slot, and is denied."""
    class Racing(Table):
        def __init__(self):
            super().__init__()
            self.barrier = threading.Barrier(2)
            self.first = set()

        def get_item(self, Key, ConsistentRead=False):
            answer = super().get_item(Key, ConsistentRead)
            me = threading.get_ident()
            if Key["cacheKey"] == "#sps-budget" and ConsistentRead and me not in self.first:
                self.first.add(me)
                self.barrier.wait(5)
            return answer

    for _ in range(20):
        t = Racing()
        b = spot_configs.Budget(1, 40)
        got = {}

        def take(cfg):
            got[cfg.fp] = spot_configs.grant(t, [cfg], spot_configs.GRANT, b, NOW)
        threads = [threading.Thread(target=take, args=(c,)) for c in CFGS[:2]]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        assert sorted(len(v) for v in got.values()) == [0, 1]
        assert len(spot_configs.read_budget(t)["new24h"]) == 1


# --------------------------------------------------------------------- seed
def scores_item(itype, regions, fetched):
    return {"cacheKey": spot_cache.cache_key(itype, regions), "scores": "{}", "fetchedAt": fetched, "expires": NOW - 3600}


def test_seed_registers_todays_accepted_requests_once_without_ec2():
    t = Table()
    five = ["us-east-1", "us-east-2", "us-west-2", "ap-northeast-1", "ap-southeast-1"]
    twelve = [f"xx-r{i:02d}-1" for i in range(12)]
    t.items["#sps-regions"] = {"cacheKey": "#sps-regions", "sets": json.dumps({
        "p5.48xlarge": {"regions": five, "until": NOW + 3600, "legacy": True},
        "g6e.xlarge": {"regions": twelve, "until": NOW + 3600, "legacy": False},
        "g7e.2xlarge": {"regions": ["us-east-1"], "until": NOW + 3600, "legacy": False},  # never answered
        "p6-b200.48xlarge": {"regions": ["us-east-1"], "until": NOW, "legacy": False},  # answered 2 days ago
    })}
    first, second = spot_cache.chunks(twelve)
    for item in (scores_item("p5.48xlarge", five, "2033-05-18T02:33:20.000000Z"),
                 scores_item("g6e.xlarge", first, "2033-05-18T03:00:00.000000Z"),
                 scores_item("g6e.xlarge", second, "2033-05-18T03:00:01.000000Z"),
                 scores_item("p6-b200.48xlarge", ["us-east-1"], "2033-05-16T03:00:00.000000Z")):
        t.items[item["cacheKey"]] = item
    led = spot_configs.Ledger(t, NOW, "us-east-1")
    legacy = spot_configs.build("p5.48xlarge", five, "us-east-1")  # the stored order, as it was sent
    assert led.warm(legacy) and led.entry(legacy.fp)["source"] == "seed"
    assert led.entry(legacy.fp)["lastUsedAt"] == NOW - 3600
    assert json.loads(led.entry(legacy.fp)["request"])["RegionNames"] == five
    assert all(led.warm(spot_configs.build("g6e.xlarge", c, "us-east-1")) for c in (first, second))
    assert not led.warm(spot_configs.build("g7e.2xlarge", ["us-east-1"], "us-east-1"))
    assert len([k for k in t.items if k.startswith("spscfg#")]) == 3
    assert spot_configs.read_budget(t)["seeded"] is True
    # Idempotent: a later seed run (or a second copy) changes nothing, and a live entry is never overwritten.
    t.items["spscfg#" + legacy.fp]["lastUsedAt"] = NOW + 5
    spot_configs.seed(t, NOW + 10, "us-east-1")
    t.items["#sps-budget"]["seeded"] = False
    spot_configs.seed(t, NOW + 10, "us-east-1")
    assert t.items["spscfg#" + legacy.fp]["lastUsedAt"] == NOW + 5
    assert len(spot_configs.read_budget(t)["index"]) == 3


# --------------------------------------------------------------- simulation
class LimitedWorld:
    """Fake EC2 whose GetSpotPlacementScores restricts DIFFERENT configurations: one used in the
    last 24 hours is always accepted (and free); a new one is accepted while fewer than `limit`
    new ones were accepted in the last 24 hours, else refused with MaxConfigLimitExceeded."""

    def __init__(self, world, limit, clock):
        self.world, self.limit, self.clock = world, limit, clock
        self.used, self.new_times, self.attempts = {}, [], []
        self.caller = "updater"
        world.sps_outcome = self.outcome
        self._plain = type(world).sps_outcome.__get__(world)

    def outcome(self, kw):
        ident = json.dumps(kw, sort_keys=True)
        now = self.clock[0]
        new = not (ident in self.used and now - self.used[ident] < DAY)
        if new and len([t for t in self.new_times if now - t < DAY]) >= self.limit:
            self.attempts.append({"at": now, "new": True, "ok": False, "caller": self.caller})
            return ClientError(LIMIT, "GetSpotPlacementScores")
        if new:
            self.new_times.append(now)
        self.used[ident] = now
        self.attempts.append({"at": now, "new": new, "ok": True, "caller": self.caller})
        return self._plain(kw)


SIM_REGIONS = [f"xx-sim-{i}" for i in range(1, 15)]
SIM_TYPES = {"g6e.xlarge": 1, "g6e.2xlarge": 1, "g6e.12xlarge": 4, "g6e.24xlarge": 4, "g6e.48xlarge": 8,
             "p5.48xlarge": 8, "p5.4xlarge": 1}


def simulate(monkeypatch, seed, limit, per_day, days=3):
    from score_fakes import Table as FakeTable, World
    rnd = random.Random(seed)
    clock = [NOW]
    t = FakeTable()
    sells = {r: rnd.sample(sorted(SIM_TYPES), rnd.randint(1, 4)) for r in SIM_REGIONS}
    world = World(sells, SIM_TYPES)
    sps = LimitedWorld(world, limit, clock)
    monkeypatch.setattr(su, "table", lambda: t)
    monkeypatch.setattr(su, "client", world.client)
    monkeypatch.setattr(su, "_epoch", lambda: clock[0])
    monkeypatch.setattr(su, "SPOT_SCORE_FAMILIES", ["p5*", "g6e.*"])
    monkeypatch.setattr(su, "BUDGET", spot_configs.Budget(per_day, per_day))
    monkeypatch.setattr(region_list, "EXCLUDED", frozenset())
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    monkeypatch.setattr(api, "client", lambda service, region: world.client(region))
    monkeypatch.setattr(api, "_epoch", lambda: clock[0])
    monkeypatch.setattr(api, "_now", lambda: float(clock[0]))
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": list(SIM_REGIONS), "fetchedAt": None})
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", ["p5*", "g6e.*"])
    monkeypatch.setattr(api, "SCORE_UPDATER_FUNCTION", "")
    cache_fakes.seeded(t.items)
    end = NOW + days * DAY
    while clock[0] < end:
        sps.caller = "updater"
        su.handler({}, None)
        # Page loads and Refresh clicks between two runs.
        sps.caller = "api"
        for _ in range(rnd.randint(0, 3)):
            q = {"refresh": "true"} if rnd.random() < 0.3 else {}
            r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": "/api/spot-scores",
                             "queryStringParameters": q}, None)
            assert r["statusCode"] == 200, r
        # Now and then a Region starts selling another size (seen at the next daily update).
        if rnd.random() < 0.1:
            region = rnd.choice(SIM_REGIONS)
            world.sells[region] = sorted(set(world.sells[region]) | {rnd.choice(sorted(SIM_TYPES))})
        clock[0] += 1800
    return sps, t


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_simulation_stays_inside_the_budget_and_never_meets_a_larger_limit(monkeypatch, seed):
    sps, t = simulate(monkeypatch, seed, limit=60, per_day=12)
    assert all(a["ok"] for a in sps.attempts)
    news = [a["at"] for a in sps.attempts if a["new"]]
    for at in news:
        assert len([x for x in news if at <= x < at + DAY]) <= 12
    # Page loads and refreshes never sent a new configuration.
    assert not [a for a in sps.attempts if a["new"] and a["caller"] == "api"]


@pytest.mark.parametrize("seed", [4, 5])
def test_simulation_after_a_refusal_waits_a_day_and_keeps_repeating_granted_ones(monkeypatch, seed):
    sps, t = simulate(monkeypatch, seed, limit=4, per_day=20)
    refused = [a for a in sps.attempts if not a["ok"]]
    assert refused and not [a for a in refused if a["caller"] == "api"]
    first = refused[0]["at"]
    # No new attempt for a day after a refusal; known ones go on being repeated.
    assert not [a for a in sps.attempts if a["new"] and first < a["at"] < first + DAY]
    assert [a for a in sps.attempts if not a["new"] and first < a["at"] < first + DAY]


def test_the_index_forgets_expired_entries_when_it_grants():
    t = Table()
    b = spot_configs.Budget(5, 40)
    t.items["#sps-budget"] = {"cacheKey": "#sps-budget", "version": 1, "seeded": True,
                              "index": json.dumps({"gone": {"t": "x", "k": "region"}})}
    assert spot_configs.grant(t, CFGS[:1], spot_configs.GRANT, b, NOW) == {CFGS[0].fp}
    assert set(spot_configs.read_budget(t)["index"]) == {CFGS[0].fp}


# ------------------------------------------------- unreadable, kept and re-indexed entries
def server_error(code="InternalError", status=500):
    return ClientError({"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
                       "GetSpotPlacementScores")


class FailingKeys(Table):
    """GetItem fails for the keys in `failing` (BatchGetItem is not used: no meta.client)."""

    def __init__(self, failing=()):
        super().__init__()
        self.failing = set(failing)

    def get_item(self, Key, ConsistentRead=False):
        if Key["cacheKey"] in self.failing:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")
        return super().get_item(Key, ConsistentRead)


def test_an_unreadable_entry_in_use_still_counts_and_stays_in_the_index():
    t = FailingKeys()
    b = spot_configs.Budget(20, 2)
    for cfg in CFGS[:2]:
        cache_fakes.register(t.items, cfg, NOW - 60)
    t.items["#sps-budget"] = {"cacheKey": "#sps-budget", "version": 1, "seeded": True,
                              "index": json.dumps({c.fp: {"t": c.itype, "k": "region"} for c in CFGS[:2]})}
    t.failing = {spot_configs.entry_key(CFGS[0].fp)}
    # Both in use (one of them unreadable): no room for a third configuration.
    assert spot_configs.grant(t, CFGS[2:3], spot_configs.GRANT, b, NOW) == set()
    assert spot_configs.active_count(t, spot_configs.read_budget(t), NOW) == 2
    # A grant that writes (room for one more) does not prune the unreadable entry.
    assert spot_configs.grant(t, CFGS[2:3], spot_configs.GRANT, spot_configs.Budget(20, 3), NOW) == {CFGS[2].fp}
    assert CFGS[0].fp in spot_configs.read_budget(t)["index"]
    # Read again later: still counted.
    t.failing = set()
    assert spot_configs.active_count(t, spot_configs.read_budget(t), NOW) == 3


def test_read_entries_tells_a_failed_read_from_a_missing_item():
    t = FailingKeys({spot_configs.entry_key("bad")})
    unread = []
    assert spot_configs.read_entries(t, ["bad", "gone"], unread) == {}
    assert unread == ["bad"]


class FailingBatch(FailingKeys):
    """FailingKeys with a low-level client whose BatchGetItem fails: every read takes the GetItem
    fallback of the batch path (two or more keys), as with a throttled boto3 Table resource."""
    name = "cache"

    def __init__(self, failing=()):
        super().__init__(failing)

        class Client:
            @staticmethod
            def batch_get_item(RequestItems):
                raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "BatchGetItem")

        class Meta:
            client = Client()
        self.meta = Meta()


def test_a_failed_read_on_the_batch_path_is_reported_as_unread():
    t = FailingBatch({spot_configs.entry_key(CFGS[0].fp)})
    cache_fakes.register(t.items, CFGS[1], NOW - 60)
    unread = []
    found = spot_configs.read_entries(t, [CFGS[0].fp, "gone", CFGS[1].fp], unread)
    assert unread == [CFGS[0].fp] and set(found) == {CFGS[1].fp}


def test_an_unreadable_entry_on_the_batch_path_still_counts_and_stays_in_the_index():
    t = FailingBatch()
    for cfg in CFGS[:2]:
        cache_fakes.register(t.items, cfg, NOW - 60)
    t.items["#sps-budget"] = {"cacheKey": "#sps-budget", "version": 1, "seeded": True,
                              "index": json.dumps({c.fp: {"t": c.itype, "k": "region"} for c in CFGS[:2]})}
    t.failing = {spot_configs.entry_key(CFGS[0].fp)}
    assert spot_configs.active_count(t, spot_configs.read_budget(t), NOW) == 2
    assert spot_configs.grant(t, CFGS[2:3], spot_configs.GRANT, spot_configs.Budget(20, 2), NOW) == set()
    assert spot_configs.grant(t, CFGS[2:3], spot_configs.GRANT, spot_configs.Budget(20, 3), NOW) == {CFGS[2].fp}
    assert CFGS[0].fp in spot_configs.read_budget(t)["index"]


def test_an_accepted_configuration_missing_from_the_index_is_indexed_again():
    t = Table()
    cache_fakes.register(t.items, CFGS[0], NOW - 1800)
    t.items["#sps-budget"] = {"cacheKey": "#sps-budget", "version": 1, "seeded": True, "index": "{}"}
    led = spot_configs.Ledger(t, NOW, "us-east-1")
    assert led.plan([CFGS[0]], spot_configs.REPEAT_ONLY) == {CFGS[0].fp}
    led.record(CFGS[0], {})
    assert CFGS[0].fp in spot_configs.read_budget(t)["index"]
    assert spot_configs.active_count(t, spot_configs.read_budget(t), NOW) == 1
    # Already indexed: no further governor write.
    version = spot_configs.read_budget(t)["version"]
    spot_configs.Ledger(t, NOW + 60, "us-east-1").record(CFGS[0], {})
    assert spot_configs.read_budget(t)["version"] == version


@pytest.mark.parametrize("error,released", [
    (server_error("RequestLimitExceeded", 503), True),
    (server_error("Throttling", 400), True),
    (server_error("InvalidParameterValue", 400), True),
    (server_error("UnauthorizedOperation", 403), True),
    (server_error("InternalError", 500), False),
    (server_error("Unavailable", 503), False),
    (ClientError({"Error": {"Code": "Odd"}}, "GetSpotPlacementScores"), False),
])
def test_a_slot_is_given_back_only_when_ec2_did_not_process_the_request(error, released):
    t = Table()
    led = spot_configs.Ledger(t, NOW, "us-east-1", spot_configs.Budget(5, 40))
    assert led.plan([CFGS[0]], spot_configs.GRANT) == {CFGS[0].fp}
    led.record(CFGS[0], error)
    assert (spot_configs.window(spot_configs.read_budget(t), NOW) == []) is released


def test_a_kept_slot_without_an_accepted_answer_counts_toward_the_configurations_in_use():
    t = Table()
    b = spot_configs.Budget(20, 2)
    led = spot_configs.Ledger(t, NOW, "us-east-1", b)
    assert led.plan(CFGS[:2], spot_configs.GRANT) == {c.fp for c in CFGS[:2]}
    led.record_all([(c, server_error()) for c in CFGS[:2]])
    # Neither has lastUsedAt, but EC2 may hold both: no third configuration.
    assert spot_configs.grant(t, CFGS[2:3], spot_configs.GRANT, b, NOW + 60) == set()
    assert spot_configs.budget_view(t, b, NOW + 60)["spotScoreConfigsInUse"] == 2


class FailsOnPage:
    """A paginator whose second page fails with a throttling answer."""

    def __init__(self, pages_before):
        self.pages_before = pages_before

    def get_paginator(self, name):
        outer = self

        class P:
            def paginate(self, **kw):
                for _ in range(outer.pages_before):
                    yield {"SpotPlacementScores": [{"Region": "us-east-1", "Score": 4}], "NextToken": "n"}
                raise server_error("RequestLimitExceeded", 503)
        return P()


@pytest.mark.parametrize("pages_before,released", [(0, True), (1, False)])
def test_a_failure_after_an_answered_page_keeps_the_slot(pages_before, released):
    t = Table()
    led = spot_configs.Ledger(t, NOW, "us-east-1", spot_configs.Budget(5, 40))
    assert led.plan([CFGS[0]], spot_configs.GRANT) == {CFGS[0].fp}
    with pytest.raises(ClientError) as caught:
        spot_configs.send(FailsOnPage(pages_before), CFGS[0])
    assert spot_configs.not_processed(caught.value) is released
    led.record(CFGS[0], caught.value)
    assert (spot_configs.window(spot_configs.read_budget(t), NOW) == []) is released


def test_the_window_and_in_use_free_a_slot_one_margin_after_24_hours():
    state = {"new24h": [{"fp": "a", "at": NOW}]}
    assert spot_configs.window(state, NOW + DAY) == state["new24h"]
    assert spot_configs.window(state, NOW + spot_configs.COUNT_SECONDS - 1) == state["new24h"]
    assert spot_configs.window(state, NOW + spot_configs.COUNT_SECONDS) == []
    assert spot_configs.RELEASE_MARGIN_SECONDS >= 15 * 60


def test_a_failed_ledger_read_does_not_prune_the_entry_when_the_governor_writes():
    """The ledger's own read of X failed (None there); grant() reads X again, tells the failure
    from a missing item, and keeps X in the index."""
    t = FailingKeys()
    a, x, y = CFGS[0], CFGS[1], CFGS[2]
    for cfg in (a, x):
        cache_fakes.register(t.items, cfg, NOW - int(23.5 * 3600))
    t.items["#sps-budget"] = {"cacheKey": "#sps-budget", "version": 1, "seeded": True,
                              "index": json.dumps({c.fp: {"t": c.itype, "k": "region"} for c in (a, x)})}
    t.failing = {spot_configs.entry_key(x.fp)}
    led = spot_configs.Ledger(t, NOW, "us-east-1", spot_configs.Budget(20, 2))
    assert led.plan([y, x], spot_configs.GRANT) == {y.fp}
    assert x.fp in spot_configs.read_budget(t)["index"]
