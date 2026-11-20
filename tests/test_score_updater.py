"""The score updater (score_updater.py): bootstrap, the daily update, the 30-minute warm run, the
governor without a click reserve, refusals, and Spot prices. Fake EC2 (score_fakes.World) and an
in-memory cache table; nothing reaches AWS."""
import json

import pytest

import cache_fakes
import region_slots
import regions
import score_groups as sg
import score_updater as su
import spot_cache
import spot_configs
from score_fakes import DAY, NOW, Table, World, limit_error

PATTERNS = ["g6e.*", "p5.*"]
GPUS = {"g6e.xlarge": 1, "g6e.12xlarge": 4, "g6e.24xlarge": 4, "p5.48xlarge": 8, "g5.xlarge": 1}
SELLS = {"us-east-1": ["g6e.xlarge", "g6e.12xlarge", "p5.48xlarge", "g5.xlarge"],
         "us-west-2": ["g6e.12xlarge", "g6e.24xlarge"],
         "eu-west-1": ["g6e.xlarge"]}


@pytest.fixture
def env(monkeypatch):
    t = Table()
    world = World(SELLS, GPUS)
    clock = {"now": NOW}
    monkeypatch.setattr(su, "table", lambda: t)
    monkeypatch.setattr(su, "client", world.client)
    monkeypatch.setattr(su, "_epoch", lambda: clock["now"])
    monkeypatch.setattr(su, "SPOT_SCORE_FAMILIES", list(PATTERNS))
    monkeypatch.setattr(su, "BUDGET", spot_configs.Budget(40, 40))
    monkeypatch.setattr(su, "CACHE_MINUTES", 30)
    monkeypatch.setattr(su, "HOME_REGION", "us-west-2")
    monkeypatch.setattr(regions, "EXCLUDED", frozenset())
    cache_fakes.seeded(t.items)
    return t, world, clock


def run(clock=None, event=None):
    return su.handler(event or {}, None)


def state(t):
    return sg.read_state(t)


def sps_calls(world):
    return world.calls("GetSpotPlacementScores")


# ------------------------------------------------------------- bootstrap
def test_bootstrap_on_an_empty_state_builds_the_groups_and_scores_them_at_once(env):
    t, world, _ = env
    counts = run()
    assert counts["mode"] == "bootstrap" and counts["updated"] is True
    got = state(t)
    assert sorted(got["groups"]) == ["g6e/1", "g6e/4", "p5/8"]
    assert got["groups"]["g6e/4"] == {"types": ["g6e.12xlarge", "g6e.24xlarge"], "chunks": [["us-east-1", "us-west-2"]],
                                      "added": {"us-east-1": "2033-05-18", "us-west-2": "2033-05-18"}}
    assert got["groups"]["g6e/1"]["chunks"] == [["eu-west-1", "us-east-1"]]
    assert got["families"] == sorted(PATTERNS) and got["dailyAt"] == NOW
    # g5 is not in spotScoreFamilies: no group, no GPU count asked.
    assert "g5.xlarge" not in got["gpus"]
    # Every request went to the us-east-1 endpoint, one per chunk, all granted.
    calls = sps_calls(world)
    assert {r for r, _ in calls} == {"us-east-1"} and len(calls) == 3 == counts["granted"]
    assert {tuple(kw["InstanceTypes"]) for _, kw in calls} == {("g6e.12xlarge", "g6e.24xlarge"), ("g6e.xlarge",),
                                                               ("p5.48xlarge",)}
    # The answers are in the shared items and the group history.
    cfg = sg.config_for(got["groups"], "g6e/4", "us-west-2")
    assert spot_cache.read(t, "g6e/4", list(cfg.regions), NOW)["scores"] == {"us-east-1": 1 + 9 % 9, "us-west-2": 1 + 9 % 9}
    assert t.items[cfg.scores_key]["expires"] == NOW + 40 * 60
    assert "spshist#g6e/4#regions" in t.items


def test_a_stored_empty_build_is_not_bootstrapped_again_until_the_daily_update(env, monkeypatch):
    t, _, clock = env
    monkeypatch.setattr(su, "client", World({"us-east-1": ["g5.xlarge"], "us-west-2": ["g5.xlarge"]}, GPUS).client)
    counts = run()
    assert counts["mode"] == "bootstrap" and counts["updated"] is True
    got = state(t)
    assert got["groups"] == {} and got["version"] == 1 and got["dailyAt"] == NOW
    # The next runs are warm (nothing to score), not a bootstrap; the next daily slot is a daily update.
    clock["now"] += 1800
    assert run()["mode"] == "warm" and state(t)["version"] == 1
    clock["now"] += DAY
    assert run()["mode"] == "daily" and state(t)["version"] == 2


def test_the_offering_lookup_asks_only_for_the_scored_patterns(env):
    _, world, _ = env
    run()
    offers = world.calls("DescribeInstanceTypeOfferings")
    assert {r for r, _ in offers} == {"us-east-1", "us-west-2", "eu-west-1"}
    assert all(kw == {"LocationType": "region", "Filters": [{"Name": "instance-type", "Values": PATTERNS}]}
               for _, kw in offers)
    # DescribeRegions in the function's own Region; GPU counts once per new type, in a Region that sells it.
    assert [r for r, _ in world.calls("DescribeRegions")] == ["us-west-2"]
    asked = sorted(t for _, kw in world.calls("DescribeInstanceTypes") for t in kw["InstanceTypes"])
    assert asked == ["g6e.12xlarge", "g6e.24xlarge", "g6e.xlarge", "p5.48xlarge"]


def test_a_new_spot_score_family_list_bootstraps_and_keeps_the_existing_lists(env, monkeypatch):
    t, world, clock = env
    run()
    before = state(t)["groups"]["g6e/4"]
    monkeypatch.setattr(su, "SPOT_SCORE_FAMILIES", ["g6e.*"])
    clock["now"] += 60
    counts = run()
    assert counts["mode"] == "bootstrap"
    got = state(t)
    assert sorted(got["groups"]) == ["g6e/1", "g6e/4"] and got["groups"]["g6e/4"] == before
    assert got["families"] == ["g6e.*"]


def test_failed_lookups_change_nothing_and_the_update_is_tried_again(env):
    t, world, clock = env
    world.fail_offerings = set(SELLS)
    counts = run()
    assert counts["updated"] is False and state(t)["groups"] == {}
    # The next run tries again (still a bootstrap) and builds.
    world.fail_offerings = set()
    clock["now"] += 1800
    assert run()["mode"] == "bootstrap" and sorted(state(t)["groups"]) == ["g6e/1", "g6e/4", "p5/8"]


def test_a_region_whose_lookup_failed_keeps_its_place(env):
    t, world, clock = env
    run()
    before = state(t)["groups"]
    world.fail_offerings = {"us-west-2"}
    world.sells["us-west-2"] = []
    clock["now"] = sg.next_daily(NOW) + 5
    counts = run()
    assert counts["mode"] == "daily" and counts["updated"] is True
    assert state(t)["groups"] == before


# -------------------------------------------------------------- daily update
def test_the_daily_update_grants_within_the_budget_and_the_rest_stays_planned(env, monkeypatch):
    t, world, _ = env
    monkeypatch.setattr(su, "BUDGET", spot_configs.Budget(2, 2))
    counts = run()
    assert counts["granted"] == 2 and counts["planned"] == 1 and len(sps_calls(world)) == 2
    budget = spot_configs.read_budget(t)
    assert len(budget["new24h"]) == 2


def test_the_warm_run_repeats_granted_ones_for_free_and_grants_planned_ones_when_there_is_room(env, monkeypatch):
    t, world, clock = env
    monkeypatch.setattr(su, "BUDGET", spot_configs.Budget(2, 2))
    run()
    # 30 minutes later: the two granted ones are repeated (free), the third waits.
    clock["now"] += 1800
    world.log.clear()
    counts = run()
    assert counts["mode"] == "warm" and counts["repeated"] == 2 and counts["granted"] == 0 and counts["planned"] == 1
    assert len(spot_configs.read_budget(t)["new24h"]) == 2
    assert world.calls("DescribeInstanceTypeOfferings") == [] and world.calls("DescribeRegions") == []
    # A redeploy with a larger budget: the next warm run grants the planned one, no membership change.
    before = state(t)
    monkeypatch.setattr(su, "BUDGET", spot_configs.Budget(5, 5))
    clock["now"] += 1800
    counts = run()
    assert counts["granted"] == 1 and counts["repeated"] == 2 and counts["planned"] == 0
    assert state(t)["groups"] == before["groups"] and state(t)["version"] == before["version"]


def test_answers_are_stamped_when_they_arrive_not_when_the_run_started(env, monkeypatch):
    """A slow run must not store an older fetchedAt over a Refresh answer stored while it ran."""
    t, world, clock = env
    fetch = spot_configs.fetch

    def slow(ec2, cfg):
        clock["now"] = NOW + 120
        return fetch(ec2, cfg)
    monkeypatch.setattr(su.spot_configs, "fetch", slow)
    run()
    cfg = sg.config_for(state(t)["groups"], "g6e/4", "us-west-2")
    assert t.items[cfg.scores_key]["fetchedAt"] == su._stamp(NOW + 120)
    # The item lifetime still counts from the run's start.
    assert t.items[cfg.scores_key]["expires"] == NOW + 40 * 60


def test_a_warm_run_never_repeats_faster_than_the_refresh_interval(env):
    t, world, clock = env
    run()
    world.log.clear()
    clock["now"] += 600
    counts = run()
    assert counts["current"] == 3 and counts["repeated"] == 0 and sps_calls(world) == []


def _slow_answers(monkeypatch, clock, delay):
    """The EC2 answers arrive `delay` seconds after the run started (the run's clock moves on)."""
    fetch = spot_configs.fetch
    start = {}

    def slow(ec2, cfg):
        clock["now"] = start["at"] + delay
        return fetch(ec2, cfg)
    monkeypatch.setattr(su.spot_configs, "fetch", slow)
    return start


def _run_at(start, clock, at):
    start["at"] = clock["now"] = at
    return run()


# Scheduled starts on the hour and half hour, a few seconds late (EventBridge jitter), answers
# about half a minute later. Live: a run at 08:00:05 stored answers at 08:00:35, and the 08:30:32
# run found them 29 min 57 s old.
JITTER = [3, 5, 32, 1, 47, 0, 59, 2, 33, 4]


def test_every_scheduled_run_repeats_even_when_its_start_jitters_by_a_few_seconds(env, monkeypatch):
    t, world, clock = env
    slot = (NOW // 1800 + 1) * 1800
    start = _slow_answers(monkeypatch, clock, 30)
    assert _run_at(start, clock, slot + JITTER[0])["mode"] == "bootstrap"
    for i, jitter in enumerate(JITTER[1:], 1):
        world.log.clear()
        counts = _run_at(start, clock, slot + i * 1800 + jitter)
        assert (i, counts["repeated"], counts["current"]) == (i, 3, 0)
        assert len(sps_calls(world)) == 3


def test_a_run_ten_minutes_after_a_repeat_does_not_repeat_it(env, monkeypatch):
    t, world, clock = env
    slot = (NOW // 1800 + 1) * 1800
    start = _slow_answers(monkeypatch, clock, 30)
    _run_at(start, clock, slot + 3)
    assert _run_at(start, clock, slot + 1800 + 5)["repeated"] == 3
    world.log.clear()
    counts = _run_at(start, clock, slot + 1800 + 600 + 5)
    assert counts["repeated"] == 0 and counts["current"] == 3 and sps_calls(world) == []


def test_the_repeat_margin_absorbs_start_jitter_only(env):
    """Due at cache minutes less REPEAT_SLACK_SECONDS (25 minutes), not a second earlier: an
    off-schedule run (a manual invoke) never repeats faster than that."""
    t, world, clock = env
    assert su.REPEAT_SLACK_SECONDS == 300
    run()
    world.log.clear()
    clock["now"] = NOW + 1800 - su.REPEAT_SLACK_SECONDS - 1
    assert run()["repeated"] == 0 and sps_calls(world) == []
    clock["now"] = NOW + 1800 - su.REPEAT_SLACK_SECONDS
    assert run()["repeated"] == 3


def test_a_repeated_answer_outlives_the_next_scheduled_run(env, monkeypatch):
    """The item lives cache minutes + GRACE_MINUTES from the run's start, so the next run (30
    minutes later, jitter included) repeats it before the API sees a miss and repeats it itself."""
    t, world, clock = env
    slot = (NOW // 1800 + 1) * 1800
    start = _slow_answers(monkeypatch, clock, 30)
    _run_at(start, clock, slot + 59)
    cfg = sg.config_for(state(t)["groups"], "g6e/4", "us-west-2")
    expires = t.items[cfg.scores_key]["expires"]
    assert expires == slot + 59 + (30 + su.GRACE_MINUTES) * 60
    assert expires > slot + 1800 + 59 + 30


def test_a_longer_cache_setting_repeats_less_often(env, monkeypatch):
    t, world, clock = env
    monkeypatch.setattr(su, "CACHE_MINUTES", 60)
    run()
    world.log.clear()
    clock["now"] += 1800
    assert run()["repeated"] == 0 and sps_calls(world) == []
    clock["now"] += 1800
    assert run()["repeated"] == 3


def test_the_daily_update_adds_new_regions_and_sizes_and_removes_excluded_ones(env, monkeypatch):
    t, world, clock = env
    run()
    world.enabled.append("ap-south-1")
    world.sells["ap-south-1"] = ["g6e.12xlarge"]
    monkeypatch.setattr(regions, "EXCLUDED", frozenset({"eu-west-1"}))
    # A warm run between two daily updates changes no membership, even with a new exclusion.
    clock["now"] += 1800
    run()
    assert state(t)["groups"]["g6e/1"]["chunks"] == [["eu-west-1", "us-east-1"]]
    assert "ap-south-1" not in sg.members(state(t)["groups"]["g6e/4"])
    clock["now"] = sg.next_daily(NOW) + 1
    world.log.clear()
    counts = run()
    got = state(t)["groups"]
    assert counts["mode"] == "daily"
    assert got["g6e/4"]["chunks"] == [["us-east-1", "us-west-2", "ap-south-1"]]
    assert got["g6e/1"]["chunks"] == [["us-east-1"]]
    # The excluded Region is never called again.
    assert "eu-west-1" not in {r for r, _, _ in world.log}


def test_the_daily_update_without_a_region_list_keeps_the_groups(env):
    t, world, clock = env
    run()
    t.items.pop("regions#enabled")
    world.fail_regions = True
    clock["now"] = sg.next_daily(NOW) + 1
    counts = run()
    assert counts["mode"] == "daily" and counts["updated"] is False and state(t)["dailyAt"] == NOW


def test_the_stored_region_list_serves_when_describe_regions_fails(env):
    t, world, clock = env
    run()
    world.fail_regions = True
    clock["now"] = sg.next_daily(NOW) + 1
    assert run()["updated"] is True


def test_new_regions_are_placed_by_first_seen_date_then_name(env):
    t, world, clock = env
    t.items[region_slots.FIRST_SEEN_KEY] = {"cacheKey": region_slots.FIRST_SEEN_KEY, "version": 1,
                                            "regions": json.dumps({"us-west-2": NOW - 5 * DAY, "us-east-1": NOW})}
    run()
    assert state(t)["groups"]["g6e/4"]["chunks"] == [["us-west-2", "us-east-1"]]


# ------------------------------------------------------- old configurations
def test_configurations_of_earlier_designs_are_never_repeated(env):
    t, world, _ = env
    old_family = spot_configs.build("g6e", ["us-east-1", "us-west-2"], "us-west-2", types=["g6e.12xlarge", "g6e.xlarge"])
    old_type = spot_configs.build("p5.48xlarge", ["us-east-1", "us-west-2"], "us-east-1")
    for cfg in (old_family, old_type):
        cache_fakes.register(t.items, cfg, NOW - 600)
    run()
    sent = {json.dumps(kw, sort_keys=True) for _, kw in sps_calls(world)}
    assert json.dumps(old_family.kwargs, sort_keys=True) not in sent
    assert json.dumps(old_type.kwargs, sort_keys=True) not in sent
    # They still count as in use for their last day.
    assert spot_configs.active_count(t, spot_configs.read_budget(t), NOW) == 5


# --------------------------------------------------------------- refusals
def test_a_refusal_stops_new_grants_for_a_day_and_warm_repeats_go_on(env, monkeypatch):
    t, world, clock = env
    monkeypatch.setattr(su, "BUDGET", spot_configs.Budget(2, 2))
    run()
    granted = {json.dumps(kw, sort_keys=True) for _, kw in sps_calls(world)}
    monkeypatch.setattr(su, "BUDGET", spot_configs.Budget(5, 5))
    original = world.sps_outcome
    # AWS refuses the one configuration that was never granted.
    world.sps_outcome = lambda kw: limit_error() if json.dumps(kw, sort_keys=True) not in granted else original(kw)
    clock["now"] += 1800
    counts = run()
    assert counts["limited"] == 1 and counts["repeated"] == 2
    assert spot_configs.read_budget(t)["limitedUntil"] == clock["now"] + spot_configs.BLOCK_SECONDS
    world.log.clear()
    clock["now"] += 1800
    counts = run()
    # No new grant during the cool-down; the granted ones still repeat.
    assert counts["granted"] == 0 and counts["repeated"] == 2 and len(sps_calls(world)) == 2


def sps_error(code, status):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
                       "GetSpotPlacementScores")


def test_another_error_is_counted_and_the_run_goes_on(env):
    t, world, _ = env
    world.sps_outcome = lambda kw: sps_error("InvalidParameterValue", 400)
    counts = run()
    assert counts["failed"] == 3 and counts["granted"] == 0
    # EC2 did not process them (4xx): the slots taken for them are given back.
    assert spot_configs.read_budget(t)["new24h"] == []


@pytest.mark.parametrize("error", [sps_error("InternalError", 500), sps_error("Unavailable", 503), cache_fakes.failed()])
def test_a_server_error_keeps_the_slot_because_ec2_may_have_counted_it(env, error):
    t, world, _ = env
    world.sps_outcome = lambda kw: error
    counts = run()
    assert counts["failed"] == 3
    assert len(spot_configs.read_budget(t)["new24h"]) == 3


# ---------------------------------------------------------- lock and prices
def test_one_run_at_a_time(env):
    t, world, _ = env
    t.items[su.LOCK_KEY] = {"cacheKey": su.LOCK_KEY, "lockedUntil": NOW + 60}
    assert run() == {"skipped": "locked"} and world.log == []


def test_the_lock_is_released_after_a_run(env):
    t, _, _ = env
    run()
    assert t.items[su.LOCK_KEY]["lockedUntil"] == 0


def test_spot_prices_are_one_call_per_region_with_every_scored_type(env):
    t, world, _ = env
    counts = run()
    calls = world.calls("DescribeSpotPriceHistory")
    assert sorted(r for r, _ in calls) == ["eu-west-1", "us-east-1", "us-west-2"] and counts["prices"]["failed"] == 0
    by_region = {r: kw["InstanceTypes"] for r, kw in calls}
    assert by_region["us-east-1"] == ["g6e.12xlarge", "g6e.xlarge", "p5.48xlarge"]
    item = t.items["price-spot#us-east-1"]
    # Fresh for 30 minutes, kept 10 minutes longer so the next run's score lookups leave no gap.
    assert item["freshUntil"] == NOW + 1800 and item["expires"] == NOW + 1800 + 600
    assert json.loads(item["data"])["g6e.xlarge"] == {"us-east-1a": "1.10", "us-east-1b": "1.6"}


def test_an_unreadable_state_does_nothing(env):
    t, world, _ = env
    from botocore.exceptions import ClientError
    real = t.get_item

    def get_item(Key, **kw):
        if Key["cacheKey"] == sg.STATE_KEY:
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
        return real(Key=Key, **kw)
    t.get_item = get_item
    assert run() == {"skipped": "state-unreadable"} and world.log == []


def test_a_newly_excluded_region_gets_no_price_call_even_before_the_daily_update(env, monkeypatch):
    t, world, clock = env
    run()
    monkeypatch.setattr(regions, "EXCLUDED", frozenset({"eu-west-1"}))
    world.log.clear()
    clock["now"] += 1800
    run()
    assert "eu-west-1" not in {r for r, _ in world.calls("DescribeSpotPriceHistory")}
    assert {r for r, _ in world.calls("DescribeSpotPriceHistory")} == {"us-east-1", "us-west-2"}


# ------------------------------------------------------- review fixes (B1, B2)
def test_an_unstored_daily_update_keeps_the_old_state_and_grants_nothing_for_it(env, monkeypatch):
    t, world, _ = env
    monkeypatch.setattr(sg, "write_state", lambda *a, **k: False)
    counts = run()
    # The bootstrap built groups but could not store them: the run goes on with the stored (empty) state.
    assert counts["mode"] == "bootstrap" and counts["updated"] is False
    assert counts["configurations"] == 0 and counts["granted"] == 0 and sps_calls(world) == []
    assert json.loads(t.items["#sps-budget"].get("new24h", "[]")) == []


def test_an_unstored_daily_update_returns_the_old_state(env, monkeypatch):
    t, _, _ = env
    old = state(t)
    monkeypatch.setattr(sg, "write_state", lambda *a, **k: False)
    assert su.daily_update(old, NOW, PATTERNS) == (old, False)


def test_a_failed_gpu_count_lookup_is_asked_in_the_next_selling_region(env):
    _, world, _ = env
    world.fail_gpus_in = {"eu-west-1"}
    found = su.gpu_counts({"eu-west-1": ["g6e.xlarge"], "us-east-1": ["g6e.xlarge", "p5.48xlarge"]}, {})
    assert found == {"g6e.xlarge": 1, "p5.48xlarge": 8}
    asked = [(r, tuple(kw["InstanceTypes"])) for r, kw in world.calls("DescribeInstanceTypes")]
    assert sorted(asked) == [("eu-west-1", ("g6e.xlarge",)), ("us-east-1", ("g6e.xlarge",)), ("us-east-1", ("p5.48xlarge",))]


def test_the_gpu_count_retry_is_bounded(env):
    _, world, _ = env
    sellers = [f"xx-test-{i}" for i in range(1, 6)]
    world.fail_gpus_in = set(sellers)
    assert su.gpu_counts({r: ["g6e.xlarge"] for r in sellers}, {}) == {}
    # At most three selling Regions per daily update.
    assert su.GPU_LOOKUP_REGIONS == 3
    assert [r for r, _ in world.calls("DescribeInstanceTypes")] == sellers[:3]


def test_a_bootstrap_that_stored_nothing_does_not_score_the_old_groups(env, monkeypatch):
    t, world, clock = env
    run()
    # A deploy changed the family list: the bootstrap's state write fails, so the stored groups are
    # still those of the old list; scoring them would spend configurations on groups being replaced.
    monkeypatch.setattr(su, "SPOT_SCORE_FAMILIES", ["g6e.*"])
    monkeypatch.setattr(sg, "write_state", lambda *a, **k: False)
    world.log.clear()
    clock["now"] += 3 * 3600
    counts = run()
    assert counts["mode"] == "bootstrap" and counts["updated"] is False
    assert counts["warmSkipped"] == "bootstrap-not-stored" and counts["granted"] == counts["repeated"] == 0
    assert sps_calls(world) == []
    # Spot prices still run.
    assert world.calls("DescribeSpotPriceHistory")


def test_an_invoke_event_does_not_force_a_daily_update(env):
    """Membership changes only at the scheduled daily update and the bootstrap: a manual invoke
    with {"mode": "daily"} runs the warm run."""
    t, world, clock = env
    run()
    before = state(t)
    clock["now"] += 1800
    world.log.clear()
    counts = run(event={"mode": "daily"})
    assert counts["mode"] == "warm" and counts["updated"] is False
    assert world.calls("DescribeInstanceTypeOfferings") == [] and state(t)["version"] == before["version"]
