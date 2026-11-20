"""Caches that cut AWS calls: Service Quotas rows and Spot prices in the DynamoDB cache table, each
with a short in-memory copy, and the per-item forced-refresh locks with their refreshed /
retryAfterSeconds answer. (The Spot score items: tests/test_spot_score_routes.py.)
AWS calls go through botocore Stubbers; the cache table is an in-memory fake with the lock
condition, plus one Stubber test for the real DynamoDB request shapes."""
import hashlib
import json
import os

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import cache_fakes  # noqa: E402
import spot_cache  # noqa: E402
import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import ANY, Stubber  # noqa: E402

REGIONS = ["us-east-1", "us-west-2"]
HASH = hashlib.sha256(b"us-east-1,us-west-2").hexdigest()[:16]
NOW = 2_000_000_000
# EC2 sells p5.48xlarge in us-east-1 only and g6e.2xlarge in both Regions.
OFFERED = {"us-east-1": ["p5.48xlarge", "g6e.2xlarge"], "us-west-2": ["g6e.2xlarge"]}
LOCK = "attribute_not_exists(cacheKey) OR lockedUntil <= :now"
LIMIT_TEXT = "You have exceeded the maximum number of new Spot placement score request configurations in a 24-hour period."


class CacheTable:
    def __init__(self):
        self.items = {}
        self.puts = []

    def get_item(self, Key, **_):
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        cache_fakes.conditional_put(self.items, Item, ConditionExpression, ExpressionAttributeValues)
        self.puts.append(Item["cacheKey"])

    def data_puts(self):
        """Cache data only: no locks, Region sets, or Spot score registry items."""
        return [k for k in self.puts if not k.startswith("#") and not cache_fakes.is_registry(k)]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(api, "REGIONS", list(REGIONS))
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", ["p5.*", "g6e.*", "g7*"])
    monkeypatch.setattr(api, "SPOT_SCORE_CACHE_MINUTES", 30)
    monkeypatch.setattr(api, "QUOTA_CACHE_MINUTES", 10)
    monkeypatch.setattr(api, "HEATMAP_WORKERS", 1)
    monkeypatch.setattr(api, "_clients", {})
    monkeypatch.setattr(api, "_offering_mem", {})
    monkeypatch.setattr(api, "_quota_cache", {})
    monkeypatch.setattr(api, "_price_cache", {})
    offered = dict(OFFERED)
    monkeypatch.setattr(api, "region_offerings", lambda r: offered[r])
    clock = {"epoch": NOW, "mono": 1000.0}
    monkeypatch.setattr(api, "_epoch", lambda: clock["epoch"])
    monkeypatch.setattr(api, "_now", lambda: clock["mono"])
    t = CacheTable()
    cache_fakes.seeded(t.items)
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    stubbers = {}

    def stub(service, region="us-east-1"):
        if (service, region) not in stubbers:
            c = boto3.client(service, region_name=region, aws_access_key_id="x", aws_secret_access_key="x")
            api._clients[(service, region)] = c
            stubbers[(service, region)] = Stubber(c)
            stubbers[(service, region)].activate()
        return stubbers[(service, region)]

    yield stub, t, clock
    for s in stubbers.values():
        s.assert_no_pending_responses()


def call(path, q=None, sub="sub-alice"):
    ev = {"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"sub": sub, "token_use": "id"}}}},
          "rawPath": path, "queryStringParameters": q}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def advance(clock, seconds):
    clock["epoch"] += seconds
    clock["mono"] += seconds


# ------------------------------------------------------------- spot prices
AZ_Q = {"region": "us-east-1", "instanceType": "p5.48xlarge"}
# The scored types us-east-1 sells, in one DescribeSpotPriceHistory call (spotScoreFamilies p5.*, g6e.*, g7*).
SCORED_TYPES = ["g6e.2xlarge", "p5.48xlarge"]


def prices(s, value, types=SCORED_TYPES):
    s.add_response("describe_spot_price_history", {"SpotPriceHistory": [
        {"AvailabilityZone": "us-east-1a", "InstanceType": "p5.48xlarge", "ProductDescription": "Linux/UNIX",
         "SpotPrice": value}]},
        {"InstanceTypes": types, "ProductDescriptions": ["Linux/UNIX"], "StartTime": ANY})


def test_spot_prices_use_one_region_item_for_thirty_minutes(env, monkeypatch):
    stub, t, clock = env
    s = stub("ec2")
    prices(s, "1.0")
    status, body = call("/api/spot-prices", AZ_Q)
    assert status == 200 and body["prices"] == {"us-east-1a": "1.0"}
    assert body["fromCache"] is False and body["fetchedAt"].endswith("Z")
    assert set(body) == {"region", "instanceType", "prices", "fetchedAt", "fromCache"}
    # One item for the Region with every scored type it sells.
    item = t.items["price-spot#us-east-1"]
    assert json.loads(item["data"]) == {"p5.48xlarge": {"us-east-1a": "1.0"}} and item["freshUntil"] == NOW + 1800
    assert item["expires"] == NOW + 1800 + 600 and item["fetchedAt"] == body["fetchedAt"]
    # Another scored type of the Region: the same item, no call.
    assert call("/api/spot-prices", {"region": "us-east-1", "instanceType": "g6e.2xlarge"})[1]["prices"] == {}
    # A new container (empty memory) 29 minutes later: the DynamoDB item, with its fetchedAt.
    monkeypatch.setattr(api, "_price_cache", {})
    advance(clock, 29 * 60)
    assert call("/api/spot-prices", AZ_Q)[1] == {**body, "fromCache": True}
    # Past thirty minutes: EC2 again.
    advance(clock, 61)
    prices(s, "2.0")
    again = call("/api/spot-prices", AZ_Q)[1]
    assert again["prices"] == {"us-east-1a": "2.0"} and again["fromCache"] is False


def test_a_stale_region_item_still_prices_the_score_groups_but_the_route_looks_up_again(env, monkeypatch):
    # Past freshUntil but inside the grace: the score groups read the item as is (no call), while
    # /api/spot-prices asks EC2 again and stores a new item.
    stub, t, clock = env
    s = stub("ec2")
    prices(s, "1.0")
    call("/api/spot-prices", AZ_Q)
    advance(clock, 1800 + 60)
    now = NOW + 1860
    assert api._price_items(["us-east-1"], now) == {"us-east-1": {"p5.48xlarge": {"us-east-1a": "1.0"}}}
    monkeypatch.setattr(api, "_price_cache", {})
    prices(s, "2.0")
    again = call("/api/spot-prices", AZ_Q)[1]
    assert again["prices"] == {"us-east-1a": "2.0"} and again["fromCache"] is False
    assert t.items["price-spot#us-east-1"]["freshUntil"] == now + 1800
    # Past the grace as well: no prices for the score groups.
    assert api._price_items(["us-east-1"], now + 1800 + 600) == {}


def test_the_region_answer_lists_every_scored_type(env):
    stub, t, _ = env
    prices(stub("ec2"), "1.5")
    status, body = call("/api/spot-prices", {"region": "us-east-1"})
    assert status == 200 and body["prices"] == {"p5.48xlarge": {"us-east-1a": "1.5"}} and body["groups"] == {}
    assert set(body) == {"region", "prices", "groups", "fetchedAt", "fromCache"}


def test_a_type_outside_the_score_set_has_its_own_item(env, monkeypatch):
    stub, t, _ = env
    offered = {"us-east-1": ["g5.xlarge"], "us-west-2": []}
    monkeypatch.setattr(api, "region_offerings", lambda r: offered[r])
    s = stub("ec2")
    s.add_response("describe_spot_price_history", {"SpotPriceHistory": []},
                   {"InstanceTypes": ["g5.xlarge"], "ProductDescriptions": ["Linux/UNIX"], "StartTime": ANY})
    assert call("/api/spot-prices", {"region": "us-east-1", "instanceType": "g5.xlarge"})[1]["prices"] == {}
    assert "price-spot#g5.xlarge#us-east-1" in t.items and "price-spot#us-east-1" not in t.items


def test_spot_price_memory_copy_skips_dynamodb_for_a_minute(env):
    stub, t, clock = env
    prices(stub("ec2"), "1.0")
    call("/api/spot-prices", AZ_Q)
    t.items.clear()
    advance(clock, 59)
    assert call("/api/spot-prices", AZ_Q)[1]["fromCache"] is True  # memory, the item is gone


def test_spot_price_memory_copy_is_bounded(env, monkeypatch):
    stub, _, _ = env
    monkeypatch.setattr(api, "MEMORY_CACHE_MAX", 2)
    api._price_cache.update({str(i): (1000.0, {}) for i in range(3)})
    prices(stub("ec2"), "1.0")
    call("/api/spot-prices", AZ_Q)
    assert list(api._price_cache) == ["price-spot#us-east-1"]


def test_spot_price_errors_are_not_cached(env):
    stub, t, _ = env
    s = stub("ec2")
    s.add_client_error("describe_spot_price_history", "Throttling", http_status_code=400)
    assert call("/api/spot-prices", AZ_Q)[0] == 502
    assert t.data_puts() == [] and api._price_cache == {}
    prices(s, "3.0")
    assert call("/api/spot-prices", AZ_Q)[1]["prices"] == {"us-east-1a": "3.0"}


# ------------------------------------------------------------------ quotas
def quotas(s, value):
    s.add_response("list_aws_default_service_quotas", {"Quotas": []})
    s.add_response("list_service_quotas", {"Quotas": [{
        "ServiceCode": "ec2", "QuotaCode": "L-1", "QuotaName": "Running On-Demand P instances", "Value": value,
        "Unit": "None", "Adjustable": True, "GlobalQuota": False}]})


def test_quotas_miss_writes_dynamodb_and_a_new_container_reads_it(env, monkeypatch):
    stub, t, clock = env
    quotas(stub("service-quotas"), 8.0)
    status, body = call("/api/quotas", {"region": "us-east-1"})
    assert status == 200 and body["fromCache"] is False and body["fetchedAt"].endswith("Z")
    assert body["quotas"][0]["value"] == 8.0
    item = t.items["quota#us-east-1"]
    assert set(item) == {"cacheKey", "rows", "fetchedAt", "expires"}
    assert item["expires"] == NOW + 10 * 60 and item["fetchedAt"] == body["fetchedAt"]
    assert json.loads(item["rows"]) == {"quotas": body["quotas"], "usageError": None}
    # A fresh container (empty memory) past the memory TTL: served from DynamoDB, no AWS call.
    monkeypatch.setattr(api, "_quota_cache", {})
    advance(clock, 120)
    status, again = call("/api/quotas", {"region": "us-east-1"})
    assert status == 200 and again == {**body, "fromCache": True}
    # Past quotaCacheMinutes the item is ignored and the Region is fetched again.
    advance(clock, 10 * 60)
    quotas(stub("service-quotas"), 16.0)
    status, live = call("/api/quotas", {"region": "us-east-1"})
    assert live["fromCache"] is False and live["quotas"][0]["value"] == 16.0


def test_quota_refresh_bypasses_both_layers_once_per_minute(env):
    stub, t, clock = env
    s = stub("service-quotas")
    quotas(s, 8.0)
    call("/api/quotas", {"region": "us-east-1"})
    quotas(s, 9.0)
    status, live = call("/api/quotas", {"region": "us-east-1", "refresh": "true"})
    assert live["fromCache"] is False and live["quotas"][0]["value"] == 9.0 and live["refreshed"] is True
    assert t.items["#lock#quota#us-east-1"]["lockedUntil"] == NOW + 60
    assert json.loads(t.items["quota#us-east-1"]["rows"])["quotas"][0]["value"] == 9.0
    advance(clock, 30)
    status, blocked = call("/api/quotas", {"region": "us-east-1", "refresh": "true"})
    assert status == 200 and blocked["fromCache"] is True and blocked["quotas"][0]["value"] == 9.0
    assert (blocked["refreshed"], blocked["retryAfterSeconds"]) == (False, 30)
    assert blocked["fetchedAt"] == live["fetchedAt"]
    advance(clock, 30)
    quotas(s, 10.0)
    assert call("/api/quotas", {"region": "us-east-1", "refresh": "true"})[1]["quotas"][0]["value"] == 10.0
    assert call("/api/quotas", {"region": "us-east-1", "refresh": "maybe"})[0] == 400


@pytest.mark.parametrize("error", [
    ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"),
    api.ConnectTimeoutError(endpoint_url="https://dynamodb.example"),
])
def test_a_failed_refresh_lock_write_serves_the_cached_answer(env, monkeypatch, error):
    """A lock write that fails for a reason other than a held lock counts as a held lock: the
    Refresh answers from the cache instead of failing the whole request."""
    stub, t, clock = env
    quotas(stub("service-quotas"), 8.0)
    first = call("/api/quotas", {"region": "us-east-1"})[1]
    put = t.put_item

    def failing(Item, ConditionExpression=None, ExpressionAttributeValues=None):
        if Item["cacheKey"].startswith("#lock#"):
            raise error
        return put(Item, ConditionExpression, ExpressionAttributeValues)
    monkeypatch.setattr(t, "put_item", failing)
    advance(clock, 5)
    status, body = call("/api/quotas", {"region": "us-east-1", "refresh": "true"})
    assert status == 200 and body["fromCache"] is True and body["quotas"] == first["quotas"]
    assert (body["refreshed"], body["retryAfterSeconds"]) == (False, api.REFRESH_WINDOW_SECONDS)
    # One item at a time and several at once give the same answer.
    assert api._claim_refresh(NOW, "quota#us-west-2") == api.REFRESH_WINDOW_SECONDS
    assert api._claim_refreshes(NOW, ["sps#a", "sps#b"]) == [api.REFRESH_WINDOW_SECONDS] * 2


def test_quota_failure_is_not_cached(env):
    stub, t, _ = env
    s = stub("service-quotas")
    s.add_client_error("list_aws_default_service_quotas", "TooManyRequestsException", http_status_code=400)
    assert call("/api/quotas", {"region": "us-east-1"})[0] == 502
    assert t.data_puts() == [] and api._quota_cache == {}


def test_quota_rows_with_a_usage_error_stay_out_of_dynamodb(env):
    stub, t, _ = env
    sq, cw = stub("service-quotas"), stub("cloudwatch")
    sq.add_response("list_aws_default_service_quotas", {"Quotas": [{
        "ServiceCode": "ec2", "QuotaCode": "L-1", "QuotaName": "Running On-Demand P instances", "Value": 8.0,
        "UsageMetric": {"MetricNamespace": "AWS/Usage", "MetricName": "ResourceCount", "MetricDimensions": {"Class": "P/OnDemand"}}}]})
    sq.add_response("list_service_quotas", {"Quotas": []})
    cw.add_client_error("get_metric_data", "AccessDenied", http_status_code=403)
    status, body = call("/api/quotas", {"region": "us-east-1"})
    assert status == 200 and body["usageErrorCode"] == "usage-unavailable"
    assert t.data_puts() == [] and "us-east-1" in api._quota_cache


def test_quota_item_read_from_dynamodb_without_aws_and_old_rows_get_a_family(env):
    stub, t, _ = env
    t.items["quota#us-east-1"] = {"cacheKey": "quota#us-east-1", "fetchedAt": "2033-05-18T03:00:00.000000Z",
                                  "rows": json.dumps({"quotas": [{"name": "x"}], "usageError": None}), "expires": NOW + 60}
    stub("service-quotas")  # no responses queued: an AWS call would fail the test
    status, body = call("/api/quotas", {"region": "us-east-1"})
    assert status == 200
    # A row stored before rows carried a family gets one from its name (none for "x").
    assert body == {"region": "us-east-1", "quotas": [{"name": "x", "family": None}], "usageError": None,
                    "fetchedAt": "2033-05-18T03:00:00.000000Z", "fromCache": True}
    assert t.data_puts() == []


# ------------------------------------------------------------ DynamoDB shapes
def test_new_cache_items_are_valid_dynamodb_requests(monkeypatch):
    res = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    tbl = res.Table("c")
    s = Stubber(tbl.meta.client)
    monkeypatch.setattr(api, "offering_cache_table", lambda: tbl)
    monkeypatch.setattr(api, "SPOT_SCORE_CACHE_MINUTES", 30)
    monkeypatch.setattr(api, "QUOTA_CACHE_MINUTES", 10)
    key = spot_cache.cache_key("g6e/4", ["us-east-1"])
    s.add_response("put_item", {}, {"TableName": "c", "Item": {
        "cacheKey": key, "scores": json.dumps({"us-east-1": 3}), "fetchedAt": "t1", "expires": 100 + 40 * 60}})
    result = {"quotas": [{"name": "x", "family": None, "value": 1.0}], "usageError": None, "fetchedAt": "t2", "fromCache": False}
    s.add_response("put_item", {}, {"TableName": "c", "Item": {
        "cacheKey": "quota#us-east-1", "rows": json.dumps({"quotas": result["quotas"], "usageError": None}, sort_keys=True),
        "fetchedAt": "t2", "expires": 100 + 10 * 60}})
    s.add_response("get_item", {"Item": {"cacheKey": {"S": "quota#us-east-1"},
                                         "rows": {"S": json.dumps({"quotas": result["quotas"], "usageError": None})},
                                         "fetchedAt": {"S": "t2"}, "expires": {"N": "700"}}},
                   {"TableName": "c", "Key": {"cacheKey": "quota#us-east-1"}})
    for key in ("#lock#sps#g6e/4", "#lock#quota#us-east-1"):
        s.add_response("put_item", {}, {"TableName": "c", "Item": {"cacheKey": key, "lockedUntil": 160, "expires": 160 + 86400},
                                        "ConditionExpression": LOCK, "ExpressionAttributeValues": {":now": 100}})
    s.activate()
    api._write_sps("g6e/4", ["us-east-1"], {"us-east-1": 3}, "t1", 100)
    api._write_quota_cache("us-east-1", result, 100)
    assert api._read_quota_cache("us-east-1", 100) == {**result, "fromCache": True}
    assert api._claim_refresh(100, "sps#g6e/4") == 0
    assert api._claim_refresh(100, "quota#us-east-1") == 0
    s.assert_no_pending_responses()


def test_cached_quota_items_drop_old_dl_rows(monkeypatch):
    rows = [
        {"name": "Running On-Demand P instances", "family": "p"},
        {"name": "Running On-Demand DL instances", "family": "dl"},
        {"name": "All DL Spot Instance Requests"},
        {"name": "Running On-Demand Inf instances"},
        {"name": "x", "family": None},
    ]

    class Table:
        def get_item(self, **_):
            return {"Item": {"rows": json.dumps({"quotas": rows, "usageError": None}), "fetchedAt": "t", "expires": 700}}

    monkeypatch.setattr(api, "offering_cache_table", lambda: Table())
    got = api._read_quota_cache("us-east-1", 100)["quotas"]
    assert [(r["name"], r["family"]) for r in got] == [
        ("Running On-Demand P instances", "p"), ("Running On-Demand Inf instances", "inf"), ("x", None)]


def test_cache_read_failures_fall_back_to_live(env, monkeypatch):
    stub, _, _ = env

    class Broken:
        def get_item(self, **_):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")

        def put_item(self, **_):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")

    monkeypatch.setattr(api, "offering_cache_table", lambda: Broken())
    # Spot scores need the stored groups: an unreadable state is a 502, never a new request.
    status, body = call("/api/spot-scores")
    assert (status, body["code"]) == (502, "sps-state-unavailable")
    quotas(stub("service-quotas"), 1.0)
    assert call("/api/quotas", {"region": "us-east-1"})[1]["fromCache"] is False


# ------------------------------------------------------------------ config
def test_config_reports_cache_and_throttle_settings(monkeypatch):
    monkeypatch.setattr(api, "SPOT_SCORE_CACHE_MINUTES", 45)
    monkeypatch.setattr(api, "QUOTA_CACHE_MINUTES", 15)
    monkeypatch.setattr(api, "API_RATE_LIMIT", 20)
    _, body = call("/api/config")
    assert (body["spotScoreCacheMinutes"], body["quotaCacheMinutes"], body["apiRateLimit"]) == (45, 15, 20)
    assert "apiBurstLimit" not in body


def test_config_reports_the_spot_score_budget(env, monkeypatch):
    _, t, _ = env
    monkeypatch.setattr(api, "SPOT_SCORE_BUDGET", api.spot_configs.Budget(12, 12))
    _, body = call("/api/config")
    assert (body["spotScoreConfigsPerDay"], body["remainingNewConfigs"], body["spotScoreConfigsInUse"],
            body["spotScoreLimitedUntil"]) == (12, 12, 0, None)
    assert "newConfigReservePerDay" not in body and "maxSpotScoreConfigs" not in body
    # Two new configurations in the window, then a limit refusal: nothing left until the cool-down ends.
    # "b" left the window (24 hours and the release margin ago).
    t.items["#sps-budget"]["new24h"] = json.dumps([{"fp": "a", "at": NOW - 60},
                                                   {"fp": "b", "at": NOW - api.spot_configs.COUNT_SECONDS}])
    _, body = call("/api/config")
    assert body["remainingNewConfigs"] == 11 and body["spotScoreConfigsInUse"] == 1
    t.items["#sps-budget"]["limitedUntil"] = NOW + 600
    _, body = call("/api/config")
    assert body["remainingNewConfigs"] == 0 and body["spotScoreLimitedUntil"] == "2033-05-18T03:43:20Z"
