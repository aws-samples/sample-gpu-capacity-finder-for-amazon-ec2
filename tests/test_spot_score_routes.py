"""GET /api/spot-scores and /api/spot-heatmap with the score updater's groups: page loads read the
cache or repeat granted configurations only, Refresh repeats (once per group per minute), nothing
grants or changes a group, Regions that join at the next daily update, the bootstrap trigger, and
the prices of each group. Fake EC2 and an in-memory cache table; nothing reaches AWS."""
import json
import os

os.environ.update(SPOT_SCORE_FAMILIES="g6e.*,p5.*", TABLE_NAME="t")

import api  # noqa: E402
import cache_fakes  # noqa: E402
import pytest  # noqa: E402
import score_groups as sg  # noqa: E402
import spot_cache  # noqa: E402
import spot_prices  # noqa: E402
from score_fakes import NOW, Table, World, state_item  # noqa: E402

REGIONS = ["eu-west-1", "us-east-1", "us-west-2"]
PATTERNS = ["g6e.*", "p5.*"]
GPUS = {"g6e.xlarge": 1, "g6e.12xlarge": 4, "g6e.24xlarge": 4, "p5.48xlarge": 8}
SELLS = {"us-east-1": ["g6e.xlarge", "g6e.12xlarge", "p5.48xlarge"], "us-west-2": ["g6e.12xlarge", "g6e.24xlarge"],
         "eu-west-1": ["g6e.xlarge", "g6e.12xlarge"]}
# The stored groups: eu-west-1 started selling g6e.12xlarge after the last daily update.
GROUPS = {
    "g6e/4": {"types": ["g6e.12xlarge", "g6e.24xlarge"], "chunks": [["us-east-1", "us-west-2"]],
              "added": {"us-east-1": "2033-05-17", "us-west-2": "2033-05-17"}},
    "g6e/1": {"types": ["g6e.xlarge"], "chunks": [["eu-west-1", "us-east-1"]], "added": {}},
    "p5/8": {"types": ["p5.48xlarge"], "chunks": [["us-east-1"]], "added": {}},
}


class Lambda:
    def __init__(self):
        self.invokes = []

    def invoke(self, **kwargs):
        self.invokes.append(kwargs)
        return {"StatusCode": 202}


@pytest.fixture
def env(monkeypatch):
    t = Table()
    world = World(SELLS, GPUS)
    fn = Lambda()
    clock = {"epoch": NOW, "mono": 1000.0}
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    monkeypatch.setattr(api, "client", lambda service, region: fn if service == "lambda" else world.client(region))
    monkeypatch.setattr(api, "_epoch", lambda: clock["epoch"])
    monkeypatch.setattr(api, "_now", lambda: clock["mono"])
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": list(REGIONS), "fetchedAt": None})
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", list(PATTERNS))
    monkeypatch.setattr(api, "SCORE_UPDATER_FUNCTION", "score-updater")
    monkeypatch.setattr(api, "SPOT_SCORE_CACHE_MINUTES", 15)
    cache_fakes.seeded(t.items)
    t.items[sg.STATE_KEY] = state_item(GROUPS, GPUS, PATTERNS)
    return t, world, fn, clock


def cfg(key, region):
    return sg.config_for(GROUPS, key, region)


def granted(t, *pairs, ago=600):
    for key, region in pairs:
        cache_fakes.register(t.items, cfg(key, region), NOW - ago)


def cached(t, key, region, scores, stamp="2033-05-18T03:20:00.000000Z"):
    c = cfg(key, region)
    spot_cache.write(t, key, list(c.regions), scores, stamp, NOW, 40)


def call(path, q=None):
    ev = {"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"sub": "u", "token_use": "id"}}}},
          "rawPath": path, "queryStringParameters": q}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def group(body, key):
    return next(g for g in body["groups"] if g["key"] == key)


def state_puts(t):
    return [k for k in t.puts if k == sg.STATE_KEY]


# ------------------------------------------------------------- page load
def test_a_page_load_reads_the_cache_and_calls_nothing(env):
    t, world, fn, _ = env
    granted(t, ("g6e/4", "us-east-1"), ("g6e/1", "us-east-1"), ("p5/8", "us-east-1"))
    cached(t, "g6e/4", "us-east-1", {"us-east-1": 7, "us-west-2": 4})
    cached(t, "g6e/1", "us-east-1", {"us-east-1": 9})
    cached(t, "p5/8", "us-east-1", {"us-east-1": 2})
    status, body = call("/api/spot-scores")
    assert status == 200 and world.log == [] and fn.invokes == []
    assert body["state"] == "ready" and body["endpoint"] == "us-east-1" and body["regions"] == REGIONS
    assert [g["key"] for g in body["groups"]] == ["g6e/1", "g6e/4", "p5/8"]
    g = group(body, "g6e/4")
    assert g["label"] == "g6e · 4 GPUs" and g["family"] == "g6e" and g["gpus"] == 4
    assert g["sizes"] == ["g6e.12xlarge", "g6e.24xlarge"] and g["regions"] == ["us-east-1", "us-west-2"]
    assert g["scores"] == {"us-east-1": 7, "us-west-2": 4} and g["fromCache"] is True
    assert g["fetchedAt"] == "2033-05-18T03:20:00.000000Z"
    assert g["configurations"] == [{"regions": ["us-east-1", "us-west-2"], "state": "active",
                                    "fetchedAt": "2033-05-18T03:20:00.000000Z"}]
    # g6e/1: eu-west-1 is a member but EC2 gave no score for it.
    assert group(body, "g6e/1")["regionStates"] == {"eu-west-1": "no-score", "us-east-1": "scored"}
    assert body["nextDailyUpdateAt"] == "2033-05-19T00:30:00Z" and body["nextWarmAt"] == "2033-05-18T03:45:00Z"
    assert state_puts(t) == []


def test_a_granted_configuration_without_a_current_answer_is_repeated_once(env):
    t, world, _, _ = env
    granted(t, ("g6e/4", "us-east-1"))
    status, body = call("/api/spot-scores", {"group": "g6e/4"})
    calls = world.calls("GetSpotPlacementScores")
    assert status == 200 and len(calls) == 1 and calls[0][0] == "us-east-1"
    assert calls[0][1] == {"InstanceTypes": ["g6e.12xlarge", "g6e.24xlarge"], "TargetCapacity": 1,
                           "RegionNames": ["us-east-1", "us-west-2"]}
    g = group(body, "g6e/4")
    assert g["fromCache"] is False and g["scores"] == {"us-east-1": 1 + 9 % 9, "us-west-2": 1 + 9 % 9}
    assert "spshist#g6e/4#regions" in t.items and g["history"][-1]["scores"] == g["scores"]
    # The answer is stored: the next load calls nothing.
    world.log.clear()
    assert call("/api/spot-scores", {"group": "g6e/4"})[1]["groups"][0]["fromCache"] is True and world.log == []
    # No new configuration was counted.
    assert json.loads(t.items["#sps-budget"].get("new24h", "[]")) == []


def test_a_planned_configuration_is_never_sent_from_a_page_or_a_refresh(env):
    t, world, _, _ = env
    for q in ({}, {"refresh": "true"}):
        status, body = call("/api/spot-scores", q)
        assert status == 200 and world.calls("GetSpotPlacementScores") == []
        g = group(body, "p5/8")
        assert g["configurations"][0]["state"] == "planned" and g["regionStates"] == {"us-east-1": "planned"}
    assert json.loads(t.items["#sps-budget"].get("new24h", "[]")) == [] and state_puts(t) == []


def test_a_blocked_configuration_shows_limited(env):
    t, world, _, _ = env
    cache_fakes.register(t.items, cfg("p5/8", "us-east-1"), NOW - 600, result="blocked", blocked_until=NOW + 3600)
    body = call("/api/spot-scores", {"group": "p5/8"})[1]
    assert group(body, "p5/8")["configurations"][0]["state"] == "limited" and world.log == []


# --------------------------------------------------------------- refresh
def test_refresh_repeats_granted_configurations_once_per_group_per_minute(env):
    t, world, _, clock = env
    granted(t, ("g6e/4", "us-east-1"))
    cached(t, "g6e/4", "us-east-1", {"us-east-1": 3})
    status, body = call("/api/spot-scores", {"group": "g6e/4", "refresh": "true"})
    g = group(body, "g6e/4")
    assert status == 200 and len(world.calls("GetSpotPlacementScores")) == 1
    assert g["refreshed"] is True and g["fromCache"] is False and "retryAfterSeconds" not in g
    assert t.items["#lock#sps#g6e/4"]["lockedUntil"] == NOW + 60
    # Again 20 seconds later: the lock holds, the cached answer serves.
    clock["epoch"] += 20
    clock["mono"] += 20
    g = group(call("/api/spot-scores", {"group": "g6e/4", "refresh": "true"})[1], "g6e/4")
    assert g["refreshed"] is False and g["retryAfterSeconds"] == 40
    assert len(world.calls("GetSpotPlacementScores")) == 1
    assert state_puts(t) == [] and json.loads(t.items["#sps-budget"].get("new24h", "[]")) == []


def test_refresh_must_be_a_boolean_and_groups_well_formed(env):
    assert call("/api/spot-scores", {"refresh": "yes"})[1]["code"] == "bad-parameter"
    assert call("/api/spot-scores", {"group": "g6e"})[1]["params"] == {"name": "group"}
    assert call("/api/spot-scores", {"scope": "az"})[1]["code"] == "sps-zone-removed"


def test_a_refused_repeat_is_blocked_and_reported(env):
    from score_fakes import limit_error
    t, world, _, _ = env
    granted(t, ("g6e/4", "us-east-1"))
    world.refuse_all = True
    body = call("/api/spot-scores", {"group": "g6e/4"})[1]
    g = group(body, "g6e/4")
    assert g["errors"][0]["code"] == "sps-limited" and g["configurations"][0]["state"] == "limited"
    assert body["spotScoreLimitedUntil"] is not None
    entry = t.items["spscfg#" + cfg("g6e/4", "us-east-1").fp]
    assert entry["blockedUntil"] > NOW
    assert isinstance(limit_error(), Exception)


# ---------------------------------------------- after the next daily update
def test_a_region_that_sells_a_group_size_but_is_not_a_member_waits_for_the_daily_update(env):
    t, world, _, _ = env
    # The API's offering item of eu-west-1 lists g6e.12xlarge (a g6e/4 size); it is not in g6e/4 yet.
    t.items["offerings#eu-west-1"] = {"cacheKey": "offerings#eu-west-1", "patterns": "gpu:g*,p*",
                                      "data": json.dumps({"g6e": ["g6e.12xlarge", "g6e.xlarge"]}), "zones": "{}",
                                      "fetchedAt": "t0", "expires": NOW + 3600}
    body = call("/api/spot-scores")[1]
    g = group(body, "g6e/4")
    assert g["pendingRegions"] == ["eu-west-1"] and g["regionStates"]["eu-west-1"] == "after-daily-update"
    assert "eu-west-1" not in g["regions"] and body["nextDailyUpdateAt"] == "2033-05-19T00:30:00Z"
    # Already a member of g6e/1: not pending there. No offering lookup was made.
    assert group(body, "g6e/1")["pendingRegions"] == [] and world.log == []


def offering_item(t, region, types):
    t.items[f"offerings#{region}"] = {"cacheKey": f"offerings#{region}", "patterns": "gpu:g*,p*",
                                      "data": json.dumps({"g6e": [x for x in types if x.startswith("g6e.")],
                                                          "p5": [x for x in types if x.startswith("p5.")]}),
                                      "zones": "{}", "fetchedAt": "t0", "expires": NOW + 3600}


# A subset request (?instanceType=, ?group=, with or without refresh) selects some groups; the
# offering items still list sizes of the other groups. Those must not be looked up (a KeyError,
# HTTP 500, before the fix).
SUBSET_CASES = [
    ({"instanceType": "g6e.xlarge"}, ["g6e/1"], {"g6e/1": []}),
    ({"instanceType": "g6e.24xlarge"}, ["g6e/4"], {"g6e/4": ["eu-west-1"]}),
    ({"instanceType": "g6e.xlarge", "refresh": "true"}, ["g6e/1"], {"g6e/1": []}),
    ({"instanceType": "g5.xlarge"}, [], {}),
    ({"group": "p5/8"}, ["p5/8"], {"p5/8": []}),
    ({"group": "g6e/1,p5/8"}, ["g6e/1", "p5/8"], {"g6e/1": [], "p5/8": []}),
    ({"group": "g6e/4,g6e/1", "refresh": "true"}, ["g6e/1", "g6e/4"], {"g6e/1": [], "g6e/4": ["eu-west-1"]}),
    ({"group": "g6e/2"}, [], {}),
]


@pytest.mark.parametrize("q,keys,pending", SUBSET_CASES, ids=[json.dumps(c[0]) for c in SUBSET_CASES])
def test_a_subset_request_answers_with_only_its_groups_and_their_pending_regions(env, q, keys, pending):
    t, world, _, _ = env
    offering_item(t, "eu-west-1", ["g6e.xlarge", "g6e.12xlarge"])
    offering_item(t, "us-east-1", ["g6e.xlarge", "g6e.12xlarge", "p5.48xlarge"])
    offering_item(t, "us-west-2", ["g6e.12xlarge", "g6e.24xlarge"])
    status, body = call("/api/spot-scores", q)
    assert status == 200, body
    assert [g["key"] for g in body["groups"]] == keys
    assert {g["key"]: g["pendingRegions"] for g in body["groups"]} == pending
    for g in body["groups"]:
        assert all(g["regionStates"][r] == "after-daily-update" for r in g["pendingRegions"])
    if "instanceType" in q:
        assert body["instanceType"] == q["instanceType"] and body["group"] == (keys[0] if keys else None)
        assert body["scoreState"] == ("grouped" if keys else "off")
    # Nothing is granted: no configuration is registered, so no EC2 call.
    assert world.calls("GetSpotPlacementScores") == []


def test_a_heatmap_of_one_size_answers_with_its_group_only(env):
    t, _, _, _ = env
    offering_item(t, "eu-west-1", ["g6e.xlarge", "g6e.12xlarge"])
    status, body = call("/api/spot-heatmap", {"types": "g6e.xlarge"})
    assert status == 200, body
    assert body["scoreGroups"] == {"g6e.xlarge": "g6e/1"} and body["scoreState"]["g6e.xlarge"] == "none"


def test_an_excluded_region_is_never_shown_even_while_it_is_still_in_a_chunk(env, monkeypatch):
    t, _, _, _ = env
    granted(t, ("g6e/1", "us-east-1"))
    cached(t, "g6e/1", "us-east-1", {"us-east-1": 9, "eu-west-1": 5})
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": ["us-east-1", "us-west-2"], "fetchedAt": None})
    g = group(call("/api/spot-scores")[1], "g6e/1")
    assert g["regions"] == ["us-east-1"] and g["scores"] == {"us-east-1": 9}
    assert g["configurations"][0]["regions"] == ["us-east-1"]


# ---------------------------------------------------------------- bootstrap
def test_an_empty_state_starts_the_updater_once_and_answers_building(env):
    t, world, fn, clock = env
    t.items.pop(sg.STATE_KEY)
    status, body = call("/api/spot-scores")
    assert status == 200 and body["state"] == "building" and body["groups"] == []
    assert fn.invokes == [{"FunctionName": "score-updater", "InvocationType": "Event",
                           "Payload": json.dumps({"mode": "bootstrap"}).encode()}]
    # The next page load within the lock window starts nothing more.
    clock["mono"] += 61
    call("/api/spot-scores")
    assert len(fn.invokes) == 1 and world.log == []


def test_a_stored_build_with_no_groups_is_ready_and_starts_nothing(env):
    t, world, fn, clock = env
    t.items[sg.STATE_KEY] = state_item({}, GPUS, PATTERNS)
    for _ in range(2):
        status, body = call("/api/spot-scores")
        assert status == 200 and body["state"] == "ready" and body["groups"] == []
        clock["mono"] += 61
    assert fn.invokes == [] and world.log == []


def test_a_new_family_list_is_building_and_keeps_showing_still_scored_groups(env, monkeypatch):
    t, _, fn, _ = env
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", ["g6e.*"])
    body = call("/api/spot-scores")[1]
    assert body["state"] == "building" and [g["key"] for g in body["groups"]] == ["g6e/1", "g6e/4"]
    assert len(fn.invokes) == 1


def test_an_unreadable_state_is_a_502(env):
    from botocore.exceptions import ClientError
    t, world, _, _ = env
    t.fail_get = ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
    status, body = call("/api/spot-scores")
    assert (status, body["code"]) == (502, "sps-state-unavailable") and world.log == []


# --------------------------------------------------------------------- prices
def test_each_group_shows_its_cached_from_price(env):
    t, world, _, _ = env
    spot_prices.write(t, "us-east-1", {"g6e.12xlarge": {"us-east-1a": "5.10", "us-east-1b": "4.90"},
                                       "g6e.24xlarge": {"us-east-1a": "9.00"}}, "t0", NOW)
    g = group(call("/api/spot-scores")[1], "g6e/4")
    assert g["prices"] == {"us-east-1": {"fromUsdPerHour": "4.9", "instanceType": "g6e.12xlarge", "az": "us-east-1b",
                                         "sizes": {"g6e.12xlarge": {"usdPerHour": "4.9", "az": "us-east-1b"},
                                                   "g6e.24xlarge": {"usdPerHour": "9", "az": "us-east-1a"}}}}
    assert world.log == []


# --------------------------------------------------------------------- heatmap
def test_the_heatmap_shows_each_types_group_score(env):
    t, world, _, _ = env
    granted(t, ("g6e/4", "us-east-1"))
    cached(t, "g6e/4", "us-east-1", {"us-east-1": 7})
    status, body = call("/api/spot-heatmap", {"types": "g6e.24xlarge,g6e.12xlarge,g6.xlarge,g6e.4xlarge,p5.48xlarge"})
    assert status == 200 and world.log == []
    assert body["scoreGroups"] == {"g6.xlarge": None, "g6e.4xlarge": None, "g6e.12xlarge": "g6e/4",
                                   "g6e.24xlarge": "g6e/4", "p5.48xlarge": "p5/8"}
    assert body["scoreState"] == {"g6.xlarge": "off", "g6e.4xlarge": "pending", "g6e.12xlarge": "scored",
                                  "g6e.24xlarge": "scored", "p5.48xlarge": "none"}
    assert body["scores"]["g6e.12xlarge"] == body["scores"]["g6e.24xlarge"] == {"us-east-1": 7}
    assert body["groupLabels"]["p5.48xlarge"] == "p5 · 8 GPUs" and body["scoreLevel"] == "family+gpus"


def test_the_heatmap_validates_its_types(env):
    assert call("/api/spot-heatmap", {"types": ""})[1]["code"] == "bad-parameter"
    assert call("/api/spot-heatmap", {"types": "gr6.4xlarge"})[1]["code"] == "type-not-enabled"
    many = ",".join(f"g6e.{i}xlarge" for i in range(31))
    assert call("/api/spot-heatmap", {"types": many})[1]["code"] == "list-too-long"


def test_one_type_selects_its_group(env):
    t, _, _, _ = env
    body = call("/api/spot-scores", {"instanceType": "g6e.24xlarge"})[1]
    assert body["group"] == "g6e/4" and body["scoreState"] == "grouped" and [g["key"] for g in body["groups"]] == ["g6e/4"]
    body = call("/api/spot-scores", {"instanceType": "g6.xlarge"})[1]
    assert body["scoreState"] == "off" and body["groups"] == []


# ---------------------------------------------------------------------- config
def test_config_lists_the_groups_the_schedule_and_the_budget(env):
    t, _, _, _ = env
    granted(t, ("g6e/4", "us-east-1"))
    body = call("/api/config")[1]
    assert body["spotScoreEndpoint"] == "us-east-1" and body["spotScoreState"] == "ready"
    assert body["spotScoreGrouping"] == "family+gpus" and body["spotScoreConfigsNeeded"] == 3
    assert body["spotScoreGroups"][1] == {"key": "g6e/4", "label": "g6e · 4 GPUs", "family": "g6e", "gpus": 4,
                                          "sizes": ["g6e.12xlarge", "g6e.24xlarge"], "regionCount": 2,
                                          "configurations": 1}
    assert body["spotScoreConfigsInUse"] == 1 and body["spotScoreConfigsPerDay"] == api.SPOT_SCORE_CONFIGS_PER_DAY
    assert body["warmMinutes"] == 15 and body["dailyUpdateUtc"] == "00:30"
    assert body["priceCache"]["spotMinutes"] == 30 and body["maxTestLaunchesPerDay"] == api.MAX_TEST_LAUNCHES_PER_DAY
    assert body["probeRegions"] == body["regions"] == REGIONS and body["excludeRegions"] == []


# ------------------------------------------- a Refresh whose repeat fails or is not allowed (B5)
def history(t, key, scores, stamp="2033-05-18T03:20:00.000000Z"):
    spot_cache.append_history(t, spot_cache.history_key(key, spot_cache.HISTORY_REGIONS), scores, stamp, NOW)


def test_a_refresh_whose_repeat_fails_keeps_the_stored_answer_and_reports_the_error(env):
    t, world, _, _ = env
    granted(t, ("g6e/4", "us-east-1"))
    cached(t, "g6e/4", "us-east-1", {"us-east-1": 3, "us-west-2": 5})
    history(t, "g6e/4", {"us-east-1": 3, "us-west-2": 5})
    world.refuse_all = True
    g = group(call("/api/spot-scores", {"group": "g6e/4", "refresh": "true"})[1], "g6e/4")
    assert len(world.calls("GetSpotPlacementScores")) == 1
    assert g["scores"] == {"us-east-1": 3, "us-west-2": 5} and g["fetchedAt"] == "2033-05-18T03:20:00.000000Z"
    assert g["regionStates"] == {"us-east-1": "scored", "us-west-2": "scored"}
    assert g["history"][-1]["scores"] == {"us-east-1": 3, "us-west-2": 5}
    assert g["errors"][0]["code"] == "sps-limited" and g["fromCache"] is True
    # The Refresh took the lock: not blocked, so no retryAfterSeconds.
    assert g["refreshed"] is False and "retryAfterSeconds" not in g


def test_a_refresh_of_a_group_that_may_not_be_repeated_keeps_the_stored_answer(env):
    t, world, _, _ = env
    # p5/8 has a stored answer, but its configuration is not granted (planned): nothing is sent.
    cached(t, "p5/8", "us-east-1", {"us-east-1": 2})
    history(t, "p5/8", {"us-east-1": 2})
    g = group(call("/api/spot-scores", {"group": "p5/8", "refresh": "true"})[1], "p5/8")
    assert world.calls("GetSpotPlacementScores") == []
    assert g["scores"] == {"us-east-1": 2} and g["fetchedAt"] == "2033-05-18T03:20:00.000000Z"
    assert g["history"] and g["errors"] == [{"code": "sps-limited", "message": api.SPS_MESSAGES["sps-limited"]}]
    assert g["refreshed"] is False and "retryAfterSeconds" not in g


def test_a_refresh_that_reaches_ec2_reports_no_error(env):
    t, world, _, _ = env
    granted(t, ("g6e/4", "us-east-1"))
    cached(t, "g6e/4", "us-east-1", {"us-east-1": 3})
    g = group(call("/api/spot-scores", {"group": "g6e/4", "refresh": "true"})[1], "g6e/4")
    assert g["refreshed"] is True and "errors" not in g and g["fetchedAt"] != "2033-05-18T03:20:00.000000Z"


def test_a_blocked_refresh_says_when_to_try_again_even_when_a_repeat_answered(env):
    t, world, _, clock = env
    granted(t, ("g6e/4", "us-east-1"))
    t.items["#lock#sps#g6e/4"] = {"cacheKey": "#lock#sps#g6e/4", "lockedUntil": NOW + 25, "expires": NOW + 86400}
    # No stored answer: the page-load repeat runs (free), the Refresh itself is blocked by the lock.
    g = group(call("/api/spot-scores", {"group": "g6e/4", "refresh": "true"})[1], "g6e/4")
    assert len(world.calls("GetSpotPlacementScores")) == 1
    assert g["refreshed"] is True and g["retryAfterSeconds"] == 25 and "errors" not in g
