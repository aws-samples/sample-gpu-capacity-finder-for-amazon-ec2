"""Enabled-Region discovery (regions.py, api._region_info, notifier.enabled_regions), the one Region
list of every feature, the Spot score request size limit, and the cold-cache fan-out over many Regions.
Fake tables and clients only; nothing reaches AWS."""
import json
import logging
import os
import threading
import time

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t", ENABLE_PROBE="true")

import api  # noqa: E402
import families  # noqa: E402
import notifier  # noqa: E402
import pytest  # noqa: E402
import regions  # noqa: E402
import cache_fakes  # noqa: E402
import spot_cache  # noqa: E402
import spot_configs  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

NOW = 2_000_000_000
CONFIGURED = ["us-west-2", "us-east-1"]
# 17 enabled Regions, as EC2 might list them (not sorted).
SEVENTEEN = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "ap-south-1", "ap-northeast-1", "ap-northeast-2",
             "ap-northeast-3", "ap-southeast-1", "ap-southeast-2", "ca-central-1", "eu-central-1", "eu-west-1",
             "eu-west-2", "eu-west-3", "eu-north-1", "sa-east-1"]


class Table:
    """The cache table: get_item/put_item by cacheKey, with the lock's conditional write. Thread-safe."""

    def __init__(self, delay=0.0):
        self.items, self.gets, self.puts = {}, [], []
        self.lock = threading.Lock()
        self.delay = delay
        self.fail_get = None

    def get_item(self, Key, **_):
        if self.delay:
            time.sleep(self.delay)
        if self.fail_get:
            raise self.fail_get
        with self.lock:
            # The Region set item and the Spot score registry items are counted apart.
            if Key["cacheKey"] != "#sps-regions" and not cache_fakes.is_registry(Key["cacheKey"]):
                self.gets.append(Key["cacheKey"])
            item = self.items.get(Key["cacheKey"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            cache_fakes.conditional_put(self.items, Item, ConditionExpression, ExpressionAttributeValues)
            if Item["cacheKey"] != "#sps-regions" and not cache_fakes.is_registry(Item["cacheKey"]):
                self.puts.append(Item["cacheKey"])


def region_item(names, fresh_until, fetched="2033-01-01T00:00:00.000000Z"):
    return {"cacheKey": "regions#enabled", "regions": json.dumps(names), "fetchedAt": fetched,
            "freshUntil": fresh_until, "expires": fresh_until + 6 * 86400}


class Lookup:
    """A DescribeRegions stand-in that counts calls; `fail` raises a ClientError."""

    def __init__(self, names=SEVENTEEN, fail=None, delay=0.0):
        self.names, self.fail, self.delay, self.calls = list(names), fail, delay, 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise ClientError({"Error": {"Code": self.fail, "Message": "x"}}, "DescribeRegions")
        return regions.filter_names(self.names)


# ------------------------------------------------------------ names and order
@pytest.mark.parametrize("name,ok", [
    ("us-east-1", True), ("ap-southeast-7", True), ("mx-central-1", True), ("il-central-1", True),
    ("cn-north-1", False), ("cn-northwest-1", False), ("us-gov-west-1", False), ("us-gov-east-1", False),
    ("us-iso-east-1", False), ("us-isob-east-1", False), ("eu-isoe-west-1", False), ("us-isof-south-1", False),
    ("US-EAST-1", False), ("us-east-1a", False), ("us-east", False), ("global", False), ("", False), (None, False),
    ("us-east-10", False), ("u-east-1", False),
])
def test_only_standard_commercial_names_pass(name, ok):
    assert regions.is_commercial(name) is ok


def test_filter_sorts_and_drops_duplicates_and_odd_names():
    assert regions.filter_names(["us-west-2", "cn-north-1", "us-east-1", "us-west-2", "us-gov-west-1", "bogus"]) == [
        "us-east-1", "us-west-2"]


def test_order_is_alphabetical():
    assert regions.order(["eu-west-1", "us-east-1", "ap-south-1", "us-west-2", "us-east-1"]) == [
        "ap-south-1", "eu-west-1", "us-east-1", "us-west-2"]


def test_describe_asks_only_for_enabled_regions():
    class Ec2:
        def __init__(self):
            self.kwargs = None

        def describe_regions(self, **kwargs):
            self.kwargs = kwargs
            return {"Regions": [{"RegionName": n} for n in ("us-west-2", "eu-west-1", "cn-north-1")]}
    ec2 = Ec2()
    assert regions.describe(ec2) == ["eu-west-1", "us-west-2"]
    assert ec2.kwargs == {"AllRegions": False}


# ------------------------------------------------------------ resolve
def test_fresh_item_is_used_without_a_lookup():
    t, look = Table(), Lookup()
    t.items["regions#enabled"] = region_item(["eu-west-1", "us-east-1"], NOW + 10)
    names, stamp, source = regions.resolve(t, look, CONFIGURED, NOW)
    assert names == ["eu-west-1", "us-east-1"] and stamp == "2033-01-01T00:00:00.000000Z"
    assert source == "cache" and look.calls == 0 and t.puts == []


def test_missing_item_asks_once_and_stores_for_a_day():
    t, look = Table(), Lookup()
    names, stamp, source = regions.resolve(t, look, CONFIGURED, NOW)
    assert source == "live" and look.calls == 1 and names == sorted(SEVENTEEN)
    item = t.items["regions#enabled"]
    assert json.loads(item["regions"]) == sorted(SEVENTEEN)
    assert item["freshUntil"] == NOW + 24 * 3600 == NOW + regions.FRESH_SECONDS
    # The item outlives its freshness, so a later failed refresh can still serve it.
    assert item["expires"] > item["freshUntil"]
    assert item["fetchedAt"] == stamp
    assert t.items["#lock#regions#enabled"]["lockedUntil"] == NOW + 60


def test_expired_item_is_asked_again():
    t, look = Table(), Lookup(names=["eu-west-1"])
    t.items["regions#enabled"] = region_item(["ap-south-1"], NOW)
    names, _, source = regions.resolve(t, look, CONFIGURED, NOW)
    assert source == "live" and look.calls == 1 and names == ["eu-west-1"]


def test_failed_lookup_serves_the_stale_list_and_logs_one_line(caplog):
    t, look = Table(), Lookup(fail="UnauthorizedOperation")
    t.items["regions#enabled"] = region_item(["ap-south-1", "us-east-1"], NOW - 5)
    with caplog.at_level(logging.WARNING):
        names, stamp, source = regions.resolve(t, look, CONFIGURED, NOW)
    assert source == "stale" and names == ["ap-south-1", "us-east-1"] and stamp == "2033-01-01T00:00:00.000000Z"
    lines = [r for r in caplog.records if "Region" in r.getMessage()]
    assert len(lines) == 1 and "UnauthorizedOperation" in lines[0].getMessage() and "cached" in lines[0].getMessage()
    # The stale item is not overwritten.
    assert t.items["regions#enabled"]["freshUntil"] == NOW - 5


def test_failed_lookup_without_an_item_falls_back_to_the_given_list(caplog):
    t, look = Table(), Lookup(fail="RequestLimitExceeded")
    with caplog.at_level(logging.WARNING):
        assert regions.resolve(t, look, CONFIGURED, NOW) == (sorted(CONFIGURED), None, "configured")
    assert len([r for r in caplog.records if "Region discovery failed" in r.getMessage()]) == 1
    assert "regions#enabled" not in t.items


@pytest.mark.parametrize("bad", [[], ["cn-north-1", "us-gov-west-1"]])
def test_an_empty_or_foreign_answer_counts_as_failed(bad):
    t = Table()
    assert regions.resolve(t, Lookup(names=bad), CONFIGURED, NOW) == (sorted(CONFIGURED), None, "configured")
    assert "regions#enabled" not in t.items


def test_unreadable_or_foreign_items_are_ignored():
    t = Table()
    t.items["regions#enabled"] = {"cacheKey": "regions#enabled", "regions": "not json", "freshUntil": NOW + 10}
    assert regions.read_item(t, NOW) is None
    t.items["regions#enabled"] = region_item(["cn-north-1"], NOW + 10)
    assert regions.read_item(t, NOW) is None
    t.fail_get = ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
    assert regions.read_item(t, NOW) is None


def test_a_held_lock_skips_the_lookup_and_serves_the_stale_list():
    t, look = Table(), Lookup()
    t.items["regions#enabled"] = region_item(["ap-south-1"], NOW - 1)
    t.items["#lock#regions#enabled"] = {"cacheKey": "#lock#regions#enabled", "lockedUntil": NOW + 30}
    assert regions.resolve(t, look, CONFIGURED, NOW)[2] == "stale" and look.calls == 0


def test_a_held_lock_without_any_item_waits_for_the_holder():
    t, look = Table(), Lookup()
    t.items["#lock#regions#enabled"] = {"cacheKey": "#lock#regions#enabled", "lockedUntil": NOW + 30}
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:  # the holder stores its answer meanwhile
            t.items["regions#enabled"] = region_item(["eu-west-1"], NOW + 100)
    names, _, source = regions.resolve(t, look, CONFIGURED, NOW, sleep=sleep)
    assert names == ["eu-west-1"] and source == "cache" and look.calls == 0 and len(slept) == 3
    # Without an answer it gives up after WAIT_SECONDS.
    t2 = Table()
    t2.items["#lock#regions#enabled"] = {"cacheKey": "#lock#regions#enabled", "lockedUntil": NOW + 30}
    slept.clear()
    assert regions.resolve(t2, look, CONFIGURED, NOW, sleep=slept.append)[2] == "configured"
    assert sum(slept) == pytest.approx(regions.WAIT_SECONDS)


def test_lock_errors_never_raise():
    t = Table()

    def broken():
        raise RuntimeError("boom")
    assert regions.resolve(t, Lookup(), CONFIGURED, NOW, take_lock=broken, sleep=lambda s: None)[2] == "configured"


def test_a_burst_of_callers_makes_one_lookup():
    t, look = Table(delay=0.005), Lookup(delay=0.1)
    results, barrier = [], threading.Barrier(20)

    def one():
        barrier.wait()
        results.append(regions.resolve(t, look, CONFIGURED, NOW))
    threads = [threading.Thread(target=one) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert look.calls == 1
    # Every caller got the full list: the holder from EC2, the others from its item.
    assert all(len(r[0]) == 17 for r in results)


# ------------------------------------------------------------ api layer
@pytest.fixture
def live(live_region_discovery, monkeypatch):
    t = Table()
    look = Lookup()
    clock = [100.0]
    monkeypatch.setattr(api, "REGIONS", list(CONFIGURED))
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    monkeypatch.setattr(api, "_describe_regions", look)
    monkeypatch.setattr(api, "_epoch", lambda: NOW)
    monkeypatch.setattr(api, "_now", lambda: clock[0])
    monkeypatch.setattr(regions.time, "sleep", lambda s: None)
    return t, look, clock


def call(method, path, q=None, body=None, claims=None):
    ev = {"requestContext": {"http": {"method": method},
                             "authorizer": {"jwt": {"claims": {"token_use": "id", **(claims or {"sub": "sub-alice", "cognito:username": "alice"})}}}},
          "rawPath": path, "queryStringParameters": q, "body": json.dumps(body) if body is not None else None}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def test_api_keeps_the_list_in_memory_for_a_minute(live):
    t, look, clock = live
    first = api.read_regions()
    assert first == sorted(SEVENTEEN) and look.calls == 1
    reads = len(t.gets)
    clock[0] += 59
    assert api.read_regions() == first and len(t.gets) == reads
    clock[0] += 2
    # Past the minute: the fresh item, no new lookup.
    assert api.read_regions() == first and len(t.gets) == reads + 1 and look.calls == 1


def test_api_keeps_the_fallback_list_briefly(live):
    t, look, clock = live
    look.fail = "UnauthorizedOperation"
    assert api.read_regions() == sorted(CONFIGURED)
    clock[0] += api.REGIONS_FALLBACK_MEMORY_SECONDS + 1
    look.fail = None
    t.items.pop("#lock#regions#enabled")  # the lock window ended
    assert len(api.read_regions()) == 17


def test_api_discovery_never_fails_a_request(live, monkeypatch):
    def broken():
        raise RuntimeError("table handle failed")
    monkeypatch.setattr(api, "offering_cache_table", broken)
    status, body = call("GET", "/api/config")
    assert status == 200 and body["regions"] == sorted(CONFIGURED) and body["regionsFetchedAt"] is None


def test_config_lists_enabled_regions_probe_regions_and_fetch_time(live):
    status, body = call("GET", "/api/config")
    assert status == 200
    assert body["regions"] == sorted(SEVENTEEN)
    # Test launches run in every listed Region.
    assert body["probeRegions"] == body["regions"]
    assert body["regionsFetchedAt"] == json.loads(json.dumps(api._regions_mem[1]["fetchedAt"]))
    assert body["regionsFetchedAt"].endswith("Z")
    # Every other field stays.
    for key in ("trackedFamilyRule", "spotScoreFamilies", "excludeRegions", "probeEnabled", "spotProbeEnabled",
                "cooldownMinutes", "notificationsEnabled",
                "offeringCacheHours", "spotScoreCacheMinutes", "quotaCacheMinutes", "capacityBlockFamilies", "priceCache"):
        assert key in body


# ------------------------------------------------------------ read vs probe Regions
@pytest.fixture
def offered(live, monkeypatch):
    entry = {"families": {"p5": ["p5.48xlarge"]}, "zones": {"p5.48xlarge": ["eu-west-1a"]},
             "fetchedAt": "t0", "fromCache": True}
    monkeypatch.setattr(api, "_offering_entry", lambda region: {**entry, "zones": {"p5.48xlarge": [f"{region}a"]}})
    return live


def test_read_validation_accepts_any_enabled_region(offered):
    assert api.need_region({"region": "eu-west-1"}) == "eu-west-1"
    assert api.need_region({"region": "us-west-2"}) == "us-west-2"
    for bad in ("eu-west-9", "cn-north-1", "", "us-east-1a"):
        with pytest.raises(api.HttpError) as err:
            api.need_region({"region": bad})
        assert err.value.status == 400 and err.value.code == "region-not-enabled"


@pytest.mark.parametrize("path,q", [
    ("/api/instance-types", {}), ("/api/instance-type-zones", {"instanceType": "p5.48xlarge"}),
    ("/api/capacity-blocks", {"instanceType": "p5.48xlarge"}), ("/api/capacity-blocks/max", {"instanceType": "p5.48xlarge"}),
    ("/api/spot-prices", {"instanceType": "p5.48xlarge"}),
    ("/api/on-demand-price", {"instanceType": "p5.48xlarge"}), ("/api/quotas", {}), ("/api/probes", {}),
])
def test_read_routes_pass_region_validation_for_a_discovered_region(offered, monkeypatch, path, q):
    """The route gets past the Region check (the AWS call behind it is stubbed to fail)."""
    def no_aws(service, region):
        raise RuntimeError(f"stop after validation ({service} {region})")
    monkeypatch.setattr(api, "client", no_aws)
    monkeypatch.setattr(api, "table", no_aws)
    status, body = call("GET", path, {"region": "eu-west-1", **q})
    assert body.get("code") not in ("region-not-enabled", "probe-region-not-allowed"), body
    status, body = call("GET", path, {"region": "eu-west-9", **q})
    assert status == 400 and body["code"] == "region-not-enabled"


def test_alert_subscription_accepts_a_discovered_region(offered, monkeypatch):
    monkeypatch.setattr(api, "ENABLE_NOTIFICATIONS", True)
    seen = []

    def stop(user):
        seen.append(user)
        raise api.HttpError(418, "stop", "stop")
    monkeypatch.setattr(api, "_user_items", stop)
    body = {"region": "eu-west-1", "instanceType": "p5.48xlarge", "signal": "spot-score", "threshold": 5}
    claims = {"sub": "sub-alice", "email": "alice@example.com", "email_verified": "true"}
    status, answer = call("POST", "/api/subscriptions", body=body, claims=claims)
    assert (status, answer["code"]) == (418, "stop") and seen == ["sub-alice"]
    status, answer = call("POST", "/api/subscriptions", body={**body, "region": "eu-west-9"}, claims=claims)
    assert status == 400 and answer["code"] == "region-not-enabled"


def test_probe_is_allowed_in_every_listed_region_only(offered, monkeypatch):
    monkeypatch.setattr(api, "ENABLE_PROBE", True)
    assert api.need_probe_region({"region": "us-west-2"}) == "us-west-2"
    assert api.need_probe_region({"region": "ap-south-1"}) == "ap-south-1"
    # A typo, or a Region that is not enabled, stays region-not-enabled.
    with pytest.raises(api.HttpError) as err:
        api.need_probe_region({"region": "eu-west-9"})
    assert err.value.code == "region-not-enabled"


def test_probe_is_refused_in_an_excluded_region(offered, monkeypatch):
    monkeypatch.setattr(regions, "EXCLUDED", frozenset({"eu-west-1"}))
    monkeypatch.setattr(api, "_regions_mem", None)
    with pytest.raises(api.HttpError) as err:
        api.need_probe_region({"region": "eu-west-1"})
    assert err.value.code == "region-not-enabled"


def test_request_line_logs_discovered_regions_only(offered, caplog):
    with caplog.at_level(logging.INFO):
        call("GET", "/api/quotas", {"region": "eu-west-9"})
    line = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith('{"cache')][-1]
    assert "region" not in line


# ------------------------------------------------------------ Spot score chunks
@pytest.mark.parametrize("size,expected", [(1, [1]), (10, [10]), (11, [10, 1]), (17, [10, 7]), (33, [10, 10, 10, 3])])
def test_chunks_stay_under_the_request_limit(size, expected):
    names = [f"xx-test-{i}" for i in range(size)]
    got = spot_cache.chunks(names)
    assert [len(c) for c in got] == expected
    assert sum(got, []) == sorted(names)
    # Stable: input order and duplicates do not change the chunks or their keys.
    assert spot_cache.chunks(list(reversed(names)) + names[:3]) == got
    assert spot_cache.MAX_REGIONS_PER_REQUEST == 10


def test_one_request_never_takes_more_than_ten_regions():
    with pytest.raises(ValueError):
        spot_configs.build("p5.48xlarge", [f"xx-test-{i}" for i in range(11)], "us-east-1")
    with pytest.raises(ValueError):
        spot_configs.build("p5.48xlarge", [], "us-east-1")


def test_notifier_alerts_may_target_any_discovered_region(monkeypatch, live_region_discovery):
    t = Table()
    t.items["regions#enabled"] = region_item(SEVENTEEN, int(time.time()) + 3600)
    monkeypatch.setattr(notifier, "REGIONS", list(CONFIGURED))
    monkeypatch.setattr(notifier, "_cache_table", t)
    monkeypatch.setattr(notifier, "client", lambda *a, **k: pytest.fail("a fresh list asks no AWS"))
    names = notifier.enabled_regions()
    assert names == regions.order(SEVENTEEN)
    item = {"region": "ap-south-1", "instanceType": "p5.48xlarge", "signal": "spot-score", "threshold": 5}
    assert notifier.alerts.is_valid(item, names, ["p5.*"])
    assert not notifier.alerts.is_valid({**item, "region": "eu-west-9"}, names, ["p5.*"])


def test_api_read_regions_leaves_out_an_excluded_region(live, monkeypatch):
    t, look, clock = live
    monkeypatch.setattr(regions, "EXCLUDED", frozenset({"eu-west-1"}))
    got = api.read_regions()
    assert "eu-west-1" not in got and len(got) == 16


def test_notifier_enabled_regions_leaves_out_an_excluded_region(monkeypatch, live_region_discovery):
    t = Table()
    t.items["regions#enabled"] = region_item(SEVENTEEN, int(time.time()) + 3600)
    monkeypatch.setattr(regions, "EXCLUDED", frozenset({"eu-west-1"}))
    monkeypatch.setattr(notifier, "REGIONS", list(CONFIGURED))
    monkeypatch.setattr(notifier, "_cache_table", t)
    monkeypatch.setattr(notifier, "client", lambda *a, **k: pytest.fail("a fresh list asks no AWS"))
    names = notifier.enabled_regions()
    assert "eu-west-1" not in names and len(names) == 16


def test_notifier_discovery_falls_back_to_its_own_region(monkeypatch, live_region_discovery):
    class Broken:
        def get_item(self, **_):
            raise RuntimeError("no table")

        def put_item(self, **_):
            raise RuntimeError("no table")
    monkeypatch.setattr(notifier, "REGIONS", list(CONFIGURED))
    monkeypatch.setattr(notifier, "_cache_table", Broken())
    monkeypatch.setattr(regions.time, "sleep", lambda s: None)
    assert notifier.enabled_regions() == sorted(CONFIGURED)


# ------------------------------------------------------------ cold-cache fan-out
class SlowOfferings:
    """DescribeInstanceTypeOfferings stand-in: each page call sleeps `delay`; `fail` Regions raise."""

    def __init__(self, region, delay, fail):
        self.region, self.delay, self.fail = region, delay, fail

    def get_paginator(self, name):
        assert name == "describe_instance_type_offerings"
        outer = self

        class Pager:
            def paginate(self, LocationType, Filters):
                time.sleep(outer.delay)
                if outer.region in outer.fail:
                    raise ClientError({"Error": {"Code": "OptInRequired", "Message": "x"}}, "DescribeInstanceTypeOfferings")
                loc = outer.region if LocationType == "region" else f"{outer.region}a"
                return [{"InstanceTypeOfferings": [{"InstanceType": "p5.48xlarge", "Location": loc}]}]
        return Pager()


def cold_matrix(monkeypatch, names, delay, fail=()):
    t = Table()
    monkeypatch.setattr(api, "REGIONS", list(CONFIGURED))
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": regions.order(names), "fetchedAt": "t"})
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    monkeypatch.setattr(api, "client", lambda service, region: SlowOfferings(region, delay, set(fail)))
    started = time.monotonic()
    status, body = call("GET", "/api/offering-matrix")
    return status, body, time.monotonic() - started


def test_matrix_with_17_regions_and_one_failing_region(monkeypatch):
    status, body, _ = cold_matrix(monkeypatch, SEVENTEEN, 0.0, fail=("sa-east-1",))
    assert status == 200 and len(body["regions"]) == 17
    assert [r["region"] for r in body["regions"]] == regions.order(SEVENTEEN)
    bad = [r for r in body["regions"] if "error" in r]
    assert bad == [{"region": "sa-east-1", "error": "not enabled", "errorCode": "not-enabled"}]
    assert all(r["families"] == {"p5": ["p5.48xlarge"]} for r in body["regions"] if "error" not in r)
    assert body["families"] == ["p5"]


# Each EC2 call sleeps SCALE x 1.5 s; the wall-time bound scales the same way from the 25 s timeout.
SCALE = 0.1


@pytest.mark.parametrize("count", [17, 33])
def test_cold_matrix_fits_the_function_timeout(monkeypatch, count):
    names = (SEVENTEEN + [f"xx-extra-{i}" for i in range(count)])[:count]
    status, body, elapsed = cold_matrix(monkeypatch, names, 1.5 * SCALE)
    assert status == 200 and len(body["regions"]) == count and not [r for r in body["regions"] if "error" in r]
    # Two calls per Region on REGION_WORKERS threads: ceil(count / 8) rounds of 2 x 1.5 s.
    rounds = -(-count // api.REGION_WORKERS)
    assert elapsed < (rounds * 2 * 1.5 + 1.5) * SCALE + 0.5
    # Well inside the 25 s timeout at full scale (one DescribeRegions call of 1.5 s added).
    assert rounds * 2 * 1.5 + 1.5 < 25 - 5


def test_a_burst_of_api_requests_makes_one_lookup(live, monkeypatch):
    t, look, _ = live
    look.delay = 0.1
    t.delay = 0.005
    monkeypatch.setattr(regions.time, "sleep", time.sleep)
    barrier, results = threading.Barrier(12), []

    def one():
        barrier.wait()
        results.append(api._region_info()["regions"])
    threads = [threading.Thread(target=one) for _ in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert look.calls == 1 and all(len(r) == 17 for r in results)
    # The API takes the same lock item as its forced refreshes.
    assert t.items["#lock#regions#enabled"]["lockedUntil"] == NOW + api.REFRESH_WINDOW_SECONDS


def test_notifier_run_checks_alerts_in_discovered_regions(monkeypatch):
    names = regions.order(SEVENTEEN)
    alert = {"userId": "u", "sk": "ap-south-1#p5.48xlarge#spot-score#5", "region": "ap-south-1",
             "instanceType": "p5.48xlarge", "signal": "spot-score", "threshold": 5, "lastState": "pending"}
    typo = {**alert, "sk": "eu-west-9#p5.48xlarge#spot-score#5", "region": "eu-west-9"}
    monkeypatch.setattr(notifier, "REGIONS", list(CONFIGURED))
    monkeypatch.setattr(notifier, "SPOT_SCORE_FAMILIES", ["p5.*"])
    monkeypatch.setattr(notifier, "scan_items", lambda: [alert, typo])
    monkeypatch.setattr(notifier, "enabled_regions", lambda: list(names))
    seen = []
    monkeypatch.setattr(notifier, "fetch", lambda key, spot, regions_=None: seen.append((key, regions_)) or 2)
    monkeypatch.setattr(notifier, "save_state", lambda *a, **k: None)
    counts = notifier.handler({}, None)
    assert counts["skipped"] == 1 and counts["groups"] == 1
    assert seen == [(("spot-score", "ap-south-1", "p5.48xlarge"), names)]
