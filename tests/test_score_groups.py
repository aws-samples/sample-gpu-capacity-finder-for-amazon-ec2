"""Spot score groups (score_groups): the group key, the stable InstanceTypes union, the append-only
Region chunks, excluded Regions, the schedule, bootstrap, and the stored state. Pure rules and an
in-memory table only."""
import json
from datetime import datetime, timezone

import pytest

import score_groups as sg
import spot_configs
from score_fakes import DAY, NOW, Table

PATTERNS = ["g6e.*", "p5.*"]
GPUS = {"g6e.xlarge": 1, "g6e.2xlarge": 1, "g6e.12xlarge": 4, "g6e.24xlarge": 4, "g6e.48xlarge": 8,
        "p5.48xlarge": 8, "p5.4xlarge": 1, "g5.xlarge": 1}


def epoch(text):
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp())


# ------------------------------------------------------------------ keys
@pytest.mark.parametrize("itype,key", [
    ("g6e.12xlarge", "g6e/4"), ("g6e.24xlarge", "g6e/4"), ("g6e.xlarge", "g6e/1"), ("p5.48xlarge", "p5/8"),
    ("p5.4xlarge", "p5/1"),
    # Unknown or no GPU count: no group.
    ("g6e.4xlarge", None), ("m5.large", None),
])
def test_the_group_key_is_the_family_and_the_gpu_count(itype, key):
    assert sg.group_key(itype, {**GPUS, "m5.large": 0}) == key


def test_labels_and_order():
    assert sg.label("g6e/4") == "g6e · 4 GPUs" and sg.label("g6/1") == "g6 · 1 GPU"
    assert sg.label("p6-b200/8") == "p6-b200 · 8 GPUs" and sg.parse_key("p6-b200/8") == ("p6-b200", 8)
    assert sg.sort_keys(["p5/8", "g7e/2", "g6e/4", "g6e/1", "g7/1", "p5/1"]) == ["g6e/1", "g6e/4", "g7/1", "g7e/2",
                                                                                  "p5/1", "p5/8"]


def test_collect_groups_the_scored_types_with_a_known_count():
    offered = {"us-east-1": ["g6e.12xlarge", "g6e.xlarge", "g5.xlarge", "g6e.4xlarge"],
               "us-west-2": ["g6e.24xlarge", "p5.48xlarge"]}
    found = sg.collect(offered, GPUS, PATTERNS)
    assert found == {"g6e/4": {"types": {"g6e.12xlarge", "g6e.24xlarge"}, "regions": {"us-east-1", "us-west-2"}},
                     "g6e/1": {"types": {"g6e.xlarge"}, "regions": {"us-east-1"}},
                     "p5/8": {"types": {"p5.48xlarge"}, "regions": {"us-west-2"}}}


# --------------------------------------------------------------- merge
def merge(groups, offered, excluded=(), first_seen=None, now=NOW, patterns=PATTERNS):
    return sg.merge(groups, sg.collect(offered, GPUS, patterns), patterns, excluded, first_seen or {}, now)


def names(n, prefix="xx-test"):
    return [f"{prefix}-{i}" for i in range(1, n + 1)]


def test_a_new_group_is_cut_into_chunks_of_ten_in_first_seen_then_name_order():
    regions = names(12)
    first_seen = {r: NOW for r in regions}
    first_seen["xx-test-9"] = NOW - 3 * DAY  # seen earlier: first
    got = merge({}, {r: ["g6e.12xlarge"] for r in regions}, first_seen=first_seen)
    chunks = got["g6e/4"]["chunks"]
    assert [len(c) for c in chunks] == [10, 2]
    assert chunks[0][0] == "xx-test-9"
    # The rest by name (text order).
    assert chunks[0][1:] == sorted(r for r in regions if r != "xx-test-9")[:9]
    assert got["g6e/4"]["types"] == ["g6e.12xlarge"]
    assert got["g6e/4"]["added"]["xx-test-1"] == "2033-05-18"


def test_the_instance_types_union_only_grows_and_ignores_failed_lookups():
    first = merge({}, {"us-east-1": ["g6e.12xlarge"], "us-west-2": ["g6e.24xlarge"]})
    assert first["g6e/4"]["types"] == ["g6e.12xlarge", "g6e.24xlarge"]
    # us-west-2's lookup failed (absent): its size stays, nothing moves.
    again = merge(first, {"us-east-1": ["g6e.12xlarge"]})
    assert again["g6e/4"] == first["g6e/4"]
    # A Region that stops selling a size changes nothing either.
    assert merge(first, {"us-east-1": ["g6e.12xlarge"], "us-west-2": []})["g6e/4"] == first["g6e/4"]
    # A new size somewhere joins the union (sorted), so its configurations change once.
    grown = merge(first, {"eu-west-1": ["g6e.48xlarge", "g6e.12xlarge"]})
    assert grown["g6e/4"]["types"] == ["g6e.12xlarge", "g6e.24xlarge"]
    assert grown["g6e/8"]["types"] == ["g6e.48xlarge"]


def test_the_region_list_is_append_only():
    start = merge({}, {r: ["g6e.12xlarge"] for r in names(10)})
    assert start["g6e/4"]["chunks"] == [sorted(names(10))]
    # A Region that stops selling stays in its chunk; a new one opens the next chunk.
    later = merge(start, {"xx-test-1": [], "aa-new-1": ["g6e.12xlarge"]}, now=NOW + DAY)
    assert later["g6e/4"]["chunks"] == [sorted(names(10)), ["aa-new-1"]]
    assert later["g6e/4"]["added"]["aa-new-1"] == "2033-05-19"
    # Two more: the last chunk fills up; the earlier chunk never changes.
    more = merge(later, {"bb-new-1": ["g6e.24xlarge"], "ab-new-1": ["g6e.12xlarge"]}, now=NOW + 2 * DAY)
    assert more["g6e/4"]["chunks"] == [sorted(names(10)), ["aa-new-1", "ab-new-1", "bb-new-1"]]


def test_an_excluded_region_is_removed_from_its_chunk_and_never_added():
    start = merge({}, {r: ["g6e.12xlarge"] for r in ("us-east-1", "me-south-1", "eu-west-1")})
    assert start["g6e/4"]["chunks"] == [["eu-west-1", "me-south-1", "us-east-1"]]
    got = merge(start, {"af-south-1": ["g6e.12xlarge"], "me-south-1": ["g6e.12xlarge"]},
                excluded={"me-south-1", "af-south-1"})
    assert got["g6e/4"]["chunks"] == [["eu-west-1", "us-east-1"]] and "me-south-1" not in got["g6e/4"]["added"]
    # Brought back: it is a new Region at the end.
    back = merge(got, {"me-south-1": ["g6e.12xlarge"]})
    assert back["g6e/4"]["chunks"] == [["eu-west-1", "us-east-1", "me-south-1"]]


def test_a_family_no_longer_scored_is_dropped_and_a_group_without_regions_too():
    start = merge({}, {"us-east-1": ["g6e.12xlarge", "p5.48xlarge"]})
    assert set(start) == {"g6e/4", "p5/8"}
    assert set(merge(start, {}, patterns=["p5.*"])) == {"p5/8"}
    assert merge(start, {}, excluded={"us-east-1"}) == {}


def test_configs_are_one_per_chunk_to_the_us_east_1_endpoint():
    groups = merge({}, {r: ["g6e.12xlarge", "p5.48xlarge"] for r in names(11)})
    pairs = sg.configs(groups)
    assert [k for k, _ in pairs] == ["g6e/4", "g6e/4", "p5/8", "p5/8"]
    cfg = pairs[0][1]
    assert cfg.endpoint == "us-east-1" == sg.ENDPOINT and cfg.kwargs["InstanceTypes"] == ["g6e.12xlarge"]
    assert list(cfg.regions) == groups["g6e/4"]["chunks"][0] and cfg.scores_key.startswith("sps#g6e/4#")
    assert sg.config_for(groups, "g6e/4", "xx-test-9").fp == pairs[1][1].fp
    assert sg.config_for(groups, "g6e/4", "nowhere-1") is None and sg.config_for(groups, "g7/1", "xx-test-1") is None
    # The same groups always give byte-identical configurations.
    assert [c.request for _, c in sg.configs(json.loads(json.dumps(groups)))] == [c.request for _, c in pairs]


# ------------------------------------------------------------ schedule
def test_the_daily_update_is_due_once_per_day_from_00_30_utc():
    assert sg.last_daily_slot(epoch("2033-05-18T00:29")) == epoch("2033-05-17T00:30")
    assert sg.last_daily_slot(epoch("2033-05-18T00:30")) == epoch("2033-05-18T00:30")
    assert sg.next_daily(epoch("2033-05-18T03:33")) == epoch("2033-05-19T00:30")
    assert sg.daily_due({"dailyAt": epoch("2033-05-17T00:31")}, epoch("2033-05-18T00:30"))
    assert not sg.daily_due({"dailyAt": epoch("2033-05-18T00:30")}, epoch("2033-05-18T23:59"))
    # A missed 00:30 run is made up by the next run.
    assert sg.daily_due({"dailyAt": epoch("2033-05-16T00:30")}, epoch("2033-05-18T00:00"))


def test_the_warm_run_is_every_fifteen_minutes_on_the_quarter_hour():
    assert sg.next_warm(epoch("2033-05-18T03:33")) == epoch("2033-05-18T03:45")
    assert sg.next_warm(epoch("2033-05-18T03:00")) == epoch("2033-05-18T03:15")
    assert sg.next_warm(epoch("2033-05-18T03:50")) == epoch("2033-05-18T04:00")
    view = sg.schedule_view({"dailyAt": epoch("2033-05-18T00:30")}, epoch("2033-05-18T03:33"))
    assert view == {"lastDailyUpdateAt": "2033-05-18T00:30:00Z", "nextDailyUpdateAt": "2033-05-19T00:30:00Z",
                    "nextWarmAt": "2033-05-18T03:45:00Z", "warmMinutes": 15, "dailyUpdateUtc": "00:30"}


def test_bootstrap_on_no_groups_and_on_a_new_family_list():
    groups = merge({}, {"us-east-1": ["g6e.12xlarge"]})
    ready = {"version": 3, "groups": groups, "families": sg.signature(PATTERNS)}
    assert not sg.needs_bootstrap(ready, list(reversed(PATTERNS)))
    # First deploy: no item parses to version 0, whatever else it holds.
    assert sg.needs_bootstrap(sg.parse_state(None), PATTERNS)
    # A stored build with zero groups is a build: no bootstrap until the families change.
    empty = {"version": 1, "groups": {}, "families": sg.signature(PATTERNS), "dailyAt": 5}
    assert not sg.needs_bootstrap(empty, PATTERNS)
    assert sg.needs_bootstrap(empty, PATTERNS + ["g7.*"])
    assert sg.needs_bootstrap(ready, PATTERNS + ["g7.*"])
    # An unreadable state builds nothing.
    assert not sg.needs_bootstrap(None, PATTERNS)


def test_config_state_follows_the_registry():
    cfg = sg.configs(merge({}, {"us-east-1": ["g6e.12xlarge"]}))[0][1]
    warm = {"fp": cfg.fp, "lastUsedAt": NOW - 3600}
    assert sg.config_state(warm, NOW) == "active"
    assert sg.config_state(None, NOW) == "planned"
    assert sg.config_state({"fp": cfg.fp, "blockedUntil": NOW + 60}, NOW) == "limited"
    assert sg.config_state(None, NOW, limited_until=NOW + 60) == "limited"
    assert sg.config_state(warm, NOW, limited_until=NOW + 60) == "active"


# ---------------------------------------------------------------- state
def test_the_state_round_trips_with_a_version_check():
    t = Table()
    assert sg.read_state(t) == {"version": 0, "families": [], "groups": {}, "gpus": {}, "offered": {},
                                "dailyAt": 0, "updatedAt": 0}
    state = {**sg.read_state(t), "families": sg.signature(PATTERNS), "groups": merge({}, {"us-east-1": ["g6e.12xlarge"]}),
             "gpus": dict(GPUS), "offered": {"us-east-1": ["g6e.12xlarge"]}, "dailyAt": NOW}
    assert sg.write_state(t, state, NOW)
    got = sg.read_state(t)
    assert got["version"] == 1 and got["groups"] == state["groups"] and got["gpus"] == GPUS and got["dailyAt"] == NOW
    # A second writer with the old version loses.
    assert not sg.write_state(t, state, NOW)
    assert "expires" not in t.items[sg.STATE_KEY]


def test_an_unreadable_or_broken_state():
    t = Table()
    t.items[sg.STATE_KEY] = {"cacheKey": sg.STATE_KEY, "version": 3, "groups": "not json", "gpus": '{"a": "x"}'}
    got = sg.read_state(t)
    assert got["groups"] == {} and got["gpus"] == {} and got["version"] == 3
    from botocore.exceptions import ClientError
    t.fail_get = ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
    assert sg.read_state(t) is None


def test_the_budget_has_no_click_reserve():
    assert spot_configs.Budget._fields == ("per_day", "max_configs")
    assert not hasattr(spot_configs, "INTERACTIVE") and not hasattr(spot_configs, "AUTOMATIC")


def test_a_new_family_setting_drops_the_sizes_it_no_longer_matches():
    start = merge({}, {"us-east-1": ["g6e.12xlarge", "g6e.24xlarge", "p5.48xlarge"]})
    assert start["g6e/4"]["types"] == ["g6e.12xlarge", "g6e.24xlarge"]
    narrowed = ["g6e.12xlarge", "p5.*"]
    got = sg.merge(start, sg.collect({"us-east-1": ["g6e.12xlarge", "g6e.24xlarge"]}, GPUS, narrowed), narrowed,
                   (), {}, NOW)
    assert got["g6e/4"]["types"] == ["g6e.12xlarge"] and got["g6e/4"]["chunks"] == start["g6e/4"]["chunks"]
    # p5/8: its Region failed this time (not in the lookup), its size still matches: kept as it was.
    assert got["p5/8"] == start["p5/8"]
    # A setting that matches no size of a group drops the group.
    assert "g6e/4" not in sg.merge(start, {}, ["g6e.48xlarge", "p5.*"], (), {}, NOW)
