"""Quota usage, Region error handling, and the Region x family offering matrix.
Service calls go through botocore Stubbers, so request and response shapes are checked
against the real service models."""
import json
import os
import time

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import families  # noqa: E402
import pytest  # noqa: E402
from unittest.mock import ANY  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

FAMILIES = ["p5.*", "g6e.*", "g7*"]


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(api, "REGIONS", ["us-east-1", "us-west-2"])
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", FAMILIES)
    monkeypatch.setattr(api, "OFFERING_CACHE_HOURS", 6)
    monkeypatch.setattr(api, "_clients", {})
    monkeypatch.setattr(api, "_quota_cache", {})
    monkeypatch.setattr(api, "_offering_mem", {})
    stubbers = []

    def make(service, region="us-east-1"):
        c = boto3.client(service, region_name=region, aws_access_key_id="x", aws_secret_access_key="x")
        s = Stubber(c)
        api._clients[(service, region)] = c
        stubbers.append(s)
        return c, s

    yield make
    for s in stubbers:
        s.assert_no_pending_responses()


def call(path, q=None):
    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": path, "queryStringParameters": q}, None)
    return r["statusCode"], json.loads(r["body"])


def quota(code, name, value, unit="None", adjustable=True, metric=None):
    item = {"ServiceCode": "ec2", "QuotaCode": code, "QuotaName": name, "Value": value, "Unit": unit,
            "Adjustable": adjustable, "GlobalQuota": False}
    if metric:
        item["UsageMetric"] = metric
    return item


def vcpu_metric(resource):
    return {"MetricNamespace": "AWS/Usage", "MetricName": "ResourceCount",
            "MetricDimensions": {"Class": resource, "Resource": "vCPU", "Service": "EC2", "Type": "Resource"},
            "MetricStatisticRecommendation": "Maximum"}


SQ_PARAMS = {"ServiceCode": "ec2", "MaxResults": 100}


# --------------------------------------------------------------- categories
@pytest.mark.parametrize("name,category", [
    ("Running On-Demand P instances", "on-demand"),
    ("Running On-Demand G and VT instances", "on-demand"),
    ("All P5 Spot Instance Requests", "spot"),
    ("All G and VT Spot Instance Requests", "spot"),
    ("Concurrent P5 Capacity Blocks per account", "capacity-block"),
    ("Concurrent P5en Capacity Blocks per organization", "capacity-block"),
    ("Concurrent Trn2 Capacity Blocks per account", "capacity-block"),
    ("Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances", None),
    ("All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests", None),
    ("Running On-Demand Trn instances", "on-demand"),
    ("All Trn Spot Instance Requests", "spot"),
    ("Running On-Demand Inf instances", "on-demand"),
    ("All Inf Spot Instance Requests", "spot"),
    # The DL quotas (Gaudi and AI 100 instances) are not shown.
    ("Running On-Demand DL instances", None),
    ("All DL Spot Instance Requests", None),
    ("Running On-Demand F instances", None),
    ("Running On-Demand X instances", None),
    ("Running Dedicated trn1 Hosts", None),
    ("Running Dedicated dl1 Hosts", None),
    ("AcceptCapacityReservationBillingOwnership request bucket refill rate", None),
    ("EC2-VPC Elastic IPs", None),
])
def test_quota_category(name, category):
    assert api.quota_category(name) == category


# Names as Service Quotas lists them for EC2 (checked with ListAWSDefaultServiceQuotas).
@pytest.mark.parametrize("name,family", [
    ("Running On-Demand P instances", "p"),
    ("All P Spot Instance Requests", "p"),
    ("All P5 Spot Instance Requests", "p"),
    ("Running On-Demand G and VT instances", "g"),
    ("All G and VT Spot Instance Requests", "g"),
    ("Running On-Demand Trn instances", "trn"),
    ("All Trn Spot Instance Requests", "trn"),
    ("Running On-Demand Inf instances", "inf"),
    ("All Inf Spot Instance Requests", "inf"),
    ("Running On-Demand DL instances", None),
    ("All DL Spot Instance Requests", None),
    ("Concurrent P5 Capacity Blocks per account", "cb:p5"),
    ("Concurrent P5en Capacity Blocks per organization", "cb:p5en"),
    ("Concurrent P6-B200 Capacity Blocks per account", "cb:p6-b200"),
    ("Concurrent P6e-GB200 Capacity Blocks per account", "cb:p6e-gb200"),
    ("Concurrent TRN2 Capacity Blocks per account", "cb:trn2"),
    ("Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances", None),
])
def test_quota_family(name, family):
    assert api.quota_family(name) == family


@pytest.mark.parametrize("used,value,expected", [
    (0.0, 192.0, 0.0), (96.0, 192.0, 50.0), (160.0, 192.0, 83.3), (192.0, 192.0, 100.0), (200.0, 192.0, 104.2),
    (None, 192.0, None), (5.0, 0.0, None), (0.0, None, None),
])
def test_percent(used, value, expected):
    assert api.percent(used, value) == expected


# ------------------------------------------------------------ quota usage
def test_quotas_with_usage_units_and_applied_flag(stub):
    _, sq = stub("service-quotas")
    _, cw = stub("cloudwatch")
    sq.add_response("list_aws_default_service_quotas", {"Quotas": [
        quota("L-417A185B", "Running On-Demand P instances", 0.0, metric=vcpu_metric("P/OnDemand")),
        quota("L-7212CCBC", "All P5 Spot Instance Requests", 0.0, metric=vcpu_metric("P5/Spot")),
        quota("L-CB1", "Concurrent P5 Capacity Blocks per account", 2.0, unit="Count", adjustable=False),
        quota("L-CB2", "Concurrent P5 Capacity Blocks per organization", 4.0, unit="Count", adjustable=False),
        quota("L-1216C47A", "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances", 5.0),
    ], "NextToken": "page2"}, SQ_PARAMS)
    sq.add_response("list_aws_default_service_quotas", {"Quotas": [
        quota("L-DB2E81BA", "Running On-Demand G and VT instances", 0.0, metric=vcpu_metric("G/OnDemand")),
    ]}, {**SQ_PARAMS, "NextToken": "page2"})
    # Applied values: no UsageMetric on this row, so the metric from the default row is kept.
    sq.add_response("list_service_quotas", {"Quotas": [
        quota("L-417A185B", "Running On-Demand P instances", 192.0),
        quota("L-DB2E81BA", "Running On-Demand G and VT instances", 64.0, metric=vcpu_metric("G/OnDemand")),
    ]}, SQ_PARAMS)
    query = lambda qid, cls: {"Id": qid, "ReturnData": True, "MetricStat": {
        "Metric": {"Namespace": "AWS/Usage", "MetricName": "ResourceCount", "Dimensions": [
            {"Name": "Class", "Value": cls}, {"Name": "Resource", "Value": "vCPU"},
            {"Name": "Service", "Value": "EC2"}, {"Name": "Type", "Value": "Resource"}]},
        "Period": 300, "Stat": "Maximum"}}
    # Rows sort as on-demand (G, P), spot (P5), capacity-block (account, organization).
    cw.add_response("get_metric_data", {"MetricDataResults": [
        {"Id": "q0", "Label": "g", "Values": [], "StatusCode": "Complete"},
        {"Id": "q1", "Label": "p", "Values": [96.0, 160.0, 120.0], "Timestamps": [api.datetime.now(api.timezone.utc)] * 3,
         "StatusCode": "Complete"},
        {"Id": "q2", "Label": "p5", "Values": [8.0], "StatusCode": "Complete"},
    ]}, {"MetricDataQueries": [query("q0", "G/OnDemand"), query("q1", "P/OnDemand"), query("q2", "P5/Spot")],
         "StartTime": ANY, "EndTime": ANY})
    sq.activate()
    cw.activate()
    status, body = call("/api/quotas", {"region": "us-east-1"})
    assert status == 200 and body["usageError"] is None and "usageErrorCode" not in body
    rows = {r["name"]: r for r in body["quotas"]}
    assert [r["category"] for r in body["quotas"]] == ["on-demand", "on-demand", "spot", "capacity-block", "capacity-block"]
    assert rows["Running On-Demand P instances"] == {
        "code": "L-417A185B", "name": "Running On-Demand P instances", "category": "on-demand", "family": "p", "value": 192.0,
        "unit": "None", "adjustable": True, "applied": True, "used": 160.0, "percent": 83.3}
    # A metric with no data point in the hour counts as zero use.
    assert rows["Running On-Demand G and VT instances"]["used"] == 0.0
    assert rows["All P5 Spot Instance Requests"]["applied"] is False
    assert rows["All P5 Spot Instance Requests"]["percent"] is None  # quota of 0
    cb = rows["Concurrent P5 Capacity Blocks per account"]
    assert (cb["unit"], cb["adjustable"], cb["used"], cb["percent"], cb["applied"]) == ("Count", False, None, None, False)
    assert "Concurrent P5 Capacity Blocks per organization" in rows
    assert "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances" not in rows
    assert body["fromCache"] is False and body["fetchedAt"].endswith("Z")
    # Served from the 60-second cache: no further stubbed responses are needed.
    assert call("/api/quotas", {"region": "us-east-1"})[1] == {**body, "fromCache": True}


def test_quota_cache_expires_after_a_minute(stub, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(api, "_now", lambda: clock[0])
    _, sq = stub("service-quotas")
    for value in (1.0, 2.0):
        sq.add_response("list_aws_default_service_quotas", {"Quotas": []})
        sq.add_response("list_service_quotas", {"Quotas": [quota("L-1", "Running On-Demand P instances", value)]})
    sq.activate()
    assert call("/api/quotas", {"region": "us-east-1"})[1]["quotas"][0]["value"] == 1.0
    clock[0] += 59
    assert call("/api/quotas", {"region": "us-east-1"})[1]["quotas"][0]["value"] == 1.0
    clock[0] += 2
    assert call("/api/quotas", {"region": "us-east-1"})[1]["quotas"][0]["value"] == 2.0


def test_usage_queries_are_batched_500_per_call(stub, monkeypatch):
    _, cw = stub("cloudwatch")
    rows = [{"_metric": vcpu_metric(f"C{i}/OnDemand")} for i in range(501)] + [{"_metric": None}]
    cw.add_response("get_metric_data", {"MetricDataResults": [{"Id": "q0", "Values": [3.0]}]})
    cw.add_response("get_metric_data", {"MetricDataResults": [{"Id": "q500", "Values": [7.0]}]})
    seen = []
    cw.client.meta.events.register("provide-client-params.cloudwatch.GetMetricData",
                                   lambda params, **_: seen.append(len(params["MetricDataQueries"])))
    cw.activate()
    usage = api._usage("us-east-1", rows)
    assert seen == [500, 1]
    assert usage[0] == 3.0 and usage[500] == 7.0 and usage[1] == 0.0 and 501 not in usage


def test_usage_failure_keeps_the_quota_rows(stub):
    _, sq = stub("service-quotas")
    _, cw = stub("cloudwatch")
    sq.add_response("list_aws_default_service_quotas", {"Quotas": [
        quota("L-1", "Running On-Demand P instances", 8.0, metric=vcpu_metric("P/OnDemand"))]})
    sq.add_response("list_service_quotas", {"Quotas": []})
    cw.add_client_error("get_metric_data", "AccessDenied", http_status_code=403)
    sq.activate()
    cw.activate()
    status, body = call("/api/quotas", {"region": "us-east-1"})
    assert status == 200
    assert body["usageError"] == "usage metrics unavailable (AccessDenied)"
    assert body["usageErrorCode"] == "usage-unavailable" and body["usageErrorParams"] == {"awsCode": "AccessDenied"}
    assert body["quotas"][0]["value"] == 8.0 and body["quotas"][0]["used"] is None


# ---------------------------------------------------------- Region errors
def test_region_error_text():
    err = lambda code: ClientError({"Error": {"Code": code}}, "Op")
    assert api.region_error(err("AuthFailure")) == "not enabled"
    assert api.region_error(err("OptInRequired")) == "not enabled"
    assert api.region_error(err("Throttling")) == "AWS call failed (Throttling)"
    assert api.region_error(TimeoutError()) == "the Region did not answer"


def test_region_error_codes_for_the_page():
    err = lambda code: ClientError({"Error": {"Code": code}}, "Op")
    assert api.region_error_code(err("AuthFailure")) == ("not-enabled", None)
    assert api.region_error_code(err("UnrecognizedClientException")) == ("not-enabled", None)
    assert api.region_error_code(err("Throttling")) == ("aws-error", {"awsCode": "Throttling"})
    assert api.region_error_code(TimeoutError()) == ("no-answer", None)
    assert api.region_failure("eu-west-1", err("Throttling")) == {
        "region": "eu-west-1", "error": "AWS call failed (Throttling)", "errorCode": "aws-error", "errorParams": {"awsCode": "Throttling"}}


def test_each_region_keeps_order_and_isolates_failures(monkeypatch):
    monkeypatch.setattr(api, "REGIONS", ["a-1", "b-1", "c-1"])

    def fn(region):
        if region == "b-1":
            raise ValueError("boom")
        return {"ok": region}

    assert api.each_region(fn) == [{"region": "a-1", "ok": "a-1"}, {"region": "b-1", "error": "the Region did not answer", "errorCode": "no-answer"},
                                   {"region": "c-1", "ok": "c-1"}]
    assert api.each_region(fn, []) == []


# --------------------------------------------------------- offering matrix
class CacheTable:
    """In-memory stand-in for the offering cache table, with the refresh-lock condition."""
    def __init__(self, items=None, fail_get=False):
        self.items = dict(items or {})
        self.gets = 0
        self.puts = []
        self.fail_get = fail_get

    def get_item(self, Key, **_):
        self.gets += 1
        if self.fail_get:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        cur = self.items.get(Item["cacheKey"])
        if ConditionExpression:
            assert ConditionExpression == "attribute_not_exists(cacheKey) OR lockedUntil <= :now"
            if cur is not None and not cur["lockedUntil"] <= ExpressionAttributeValues[":now"]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[Item["cacheKey"]] = Item
        self.puts.append(Item["cacheKey"])


def offerings(s, types, token=None, next_token=None):
    resp = {"InstanceTypeOfferings": [{"InstanceType": t, "LocationType": "region", "Location": "x"} for t in types]}
    if next_token:
        resp["NextToken"] = next_token
    params = {"LocationType": "region", "Filters": [{"Name": "instance-type", "Values": api.region_offering_patterns()}]}
    if token:
        params["NextToken"] = token
    s.add_response("describe_instance_type_offerings", resp, params)


def zones(s, pairs, token=None, next_token=None):
    """The zone-level call of the same refresh cycle: (type, zone) pairs for the allowed families."""
    resp = {"InstanceTypeOfferings": [{"InstanceType": t, "LocationType": "availability-zone", "Location": z} for t, z in pairs]}
    if next_token:
        resp["NextToken"] = next_token
    params = {"LocationType": "availability-zone", "Filters": [{"Name": "instance-type", "Values": families.filter_values()}]}
    if token:
        params["NextToken"] = token
    s.add_response("describe_instance_type_offerings", resp, params)


def item(region, offered, zone_map=None, fetched="2033-05-18T01:00:00.000000Z", expires=2_000_000_010, patterns=None):
    return {"cacheKey": f"offerings#{region}", "data": json.dumps(offered), "zones": json.dumps(zone_map or {}),
            "fetchedAt": fetched, "patterns": patterns or families.cache_tag(), "expires": expires}


@pytest.fixture
def matrix(stub, monkeypatch):
    clock = [2_000_000_000]
    monkeypatch.setattr(api, "_epoch", lambda: clock[0])
    t = CacheTable()
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    return stub, t, clock


def test_matrix_miss_fetches_live_paginates_groups_and_caches(matrix):
    stub, t, clock = matrix
    _, east = stub("ec2", "us-east-1")
    _, west = stub("ec2", "us-west-2")
    offerings(east, ["p5.48xlarge", "m5.large"], next_token="n")
    offerings(east, ["g6e.12xlarge", "g6e.2xlarge", "g7e.8xlarge"], token="n")
    zones(east, [("p5.48xlarge", "us-east-1b"), ("m5.large", "us-east-1a")], next_token="z")
    zones(east, [("p5.48xlarge", "us-east-1a"), ("g6e.2xlarge", "us-east-1c")], token="z")
    west.add_client_error("describe_instance_type_offerings", "AuthFailure", http_status_code=401)
    east.activate()
    west.activate()
    status, body = call("/api/offering-matrix")
    assert status == 200 and body["fromCache"] is False and body["cacheHours"] == 6
    east_row, west_row = body["regions"]
    # Natural order: families g6e < g7e < p5, sizes by number (2xlarge before 12xlarge).
    assert east_row["families"] == {"g6e": ["g6e.2xlarge", "g6e.12xlarge"], "g7e": ["g7e.8xlarge"], "p5": ["p5.48xlarge"]}
    assert list(east_row["families"]) == ["g6e", "g7e", "p5"]
    assert east_row["fromCache"] is False and east_row["fetchedAt"].endswith("Z")
    # The matrix row does not carry the zones, and no refresh fields without refresh=true.
    assert set(east_row) == {"region", "families", "fetchedAt", "fromCache"}
    assert west_row == {"region": "us-west-2", "error": "not enabled", "errorCode": "not-enabled"}
    assert body["families"] == ["g6e", "g7e", "p5"]
    # Only the Region that answered is cached; the failed one is not poisoned.
    assert t.puts == ["offerings#us-east-1"]
    stored = t.items["offerings#us-east-1"]
    # The freshness window is offeringCacheHours; the item itself is kept to the max-stale bound.
    assert stored["refreshAfter"] == clock[0] + 6 * 3600 and stored["patterns"] == families.cache_tag()
    assert stored["expires"] == clock[0] + api.OFFERING_KEEP_SECONDS > stored["refreshAfter"]
    assert json.loads(stored["data"]) == east_row["families"] and stored["fetchedAt"] == east_row["fetchedAt"]
    # Zones per type, sorted, only for the allowed families.
    assert json.loads(stored["zones"]) == {"g6e.2xlarge": ["us-east-1c"], "p5.48xlarge": ["us-east-1a", "us-east-1b"]}


def test_matrix_hit_serves_cache_without_ec2(matrix):
    stub, t, clock = matrix
    for r in ("us-east-1", "us-west-2"):
        t.items[f"offerings#{r}"] = item(r, {"p5": ["p5.48xlarge"]})
    stub("ec2", "us-east-1")[1].activate()  # no responses queued: any EC2 call fails the test
    status, body = call("/api/offering-matrix")
    assert status == 200 and body["fromCache"] is True
    assert all(r["fromCache"] and r["families"] == {"p5": ["p5.48xlarge"]} for r in body["regions"])
    # Each Region's fetchedAt is the stored one, not the request time.
    assert [r["fetchedAt"] for r in body["regions"]] == ["2033-05-18T01:00:00.000000Z"] * 2
    assert t.puts == []


# ------------------------------------------- stale offering items
def stale_item(region, offered, **kwargs):
    """An offering item past its freshness window but inside its max-stale bound."""
    return {**item(region, offered, expires=2_000_000_000 + api.OFFERING_KEEP_SECONDS, **kwargs),
            "refreshAfter": 2_000_000_000 - 1}


def test_one_caller_refreshes_a_stale_item_under_its_lock_within_the_budget(matrix):
    """The Region past its freshness window is asked about again by the caller that takes the item's
    refresh lock, in the same request. That caller answers with what it got; the fresh item is
    stored, so the Region's other readers are fresh too. The fresh Regions are not asked again."""
    stub, t, clock = matrix
    t.items["offerings#us-east-1"] = stale_item("us-east-1", {"p5": ["p5.48xlarge"]})
    t.items["offerings#us-west-2"] = item("us-west-2", {"g6e": ["g6e.2xlarge"]})
    _, east = stub("ec2", "us-east-1")
    offerings(east, ["g7e.8xlarge"])
    zones(east, [("g7e.8xlarge", "us-east-1a")])
    east.activate()
    status, body = call("/api/offering-matrix")
    east_row, west_row = body["regions"]
    assert status == 200 and east_row["families"] == {"g7e": ["g7e.8xlarge"]} and east_row["fromCache"] is False
    assert "stale" not in east_row and "stale" not in west_row
    assert west_row["families"] == {"g6e": ["g6e.2xlarge"]} and west_row["fromCache"] is True
    assert t.items["#lock#offerings#us-east-1"]["lockedUntil"] == clock[0] + api.REFRESH_WINDOW_SECONDS
    stored = t.items["offerings#us-east-1"]
    assert json.loads(stored["data"]) == {"g7e": ["g7e.8xlarge"]}
    assert stored["refreshAfter"] == clock[0] + 6 * 3600
    assert api._read_cached("us-east-1", clock[0]) == {
        "families": {"g7e": ["g7e.8xlarge"]}, "zones": {"g7e.8xlarge": ["us-east-1a"]}, "accelerators": ["g7e"],
        "blockTypes": [], "fetchedAt": ANY, "fromCache": True}


def test_a_refresh_that_misses_the_budget_serves_the_stale_item_and_marks_no_region(matrix, monkeypatch):
    """The refresh has a tight budget, so a slow Region cannot hold the answer. The stale data is
    the answer, and missing such a small budget is no reason to skip the Region for minutes."""
    stub, t, clock = matrix
    t.items["offerings#us-east-1"] = stale_item("us-east-1", {"p5": ["p5.48xlarge"]})
    monkeypatch.setattr(api, "STALE_REFRESH_BUDGET_SECONDS", 0.05)
    slow = lambda r: (time.sleep(0.5), {"families": {}, "zones": {}, "accelerators": []})[1]  # noqa: E731
    monkeypatch.setattr(api, "_fetch_region_offerings", slow)
    body = call("/api/instance-types", {"region": "us-east-1"})[1]
    assert body["instanceTypes"] == ["p5.48xlarge"] and body["stale"] is True
    assert api._region_down == {} and "#down#us-east-1" not in t.items


def test_another_caller_holds_the_lock_so_the_stale_item_is_served_without_calling_ec2(matrix):
    stub, t, clock = matrix
    t.items["offerings#us-east-1"] = stale_item("us-east-1", {"p5": ["p5.48xlarge"]})
    t.items["#lock#offerings#us-east-1"] = {"cacheKey": "#lock#offerings#us-east-1", "lockedUntil": clock[0] + 30}
    stub("ec2", "us-east-1")[1].activate()  # no responses queued: any EC2 call fails the test
    body = call("/api/instance-types", {"region": "us-east-1"})[1]
    assert body["instanceTypes"] == ["p5.48xlarge"] and body["stale"] is True and body["fromCache"] is True


def test_a_failed_refresh_keeps_the_stale_answer_and_stores_nothing(matrix, monkeypatch):
    stub, t, clock = matrix
    t.items["offerings#us-east-1"] = stale_item("us-east-1", {"p5": ["p5.48xlarge"]}, zone_map={"p5.48xlarge": ["us-east-1a"]})
    monkeypatch.setattr(api, "_fetch_region_offerings",
                        lambda r: (_ for _ in ()).throw(ClientError({"Error": {"Code": "RequestLimitExceeded"}},
                                                                    "DescribeInstanceTypeOfferings")))
    body = call("/api/instance-type-zones", {"region": "us-east-1", "instanceType": "p5.48xlarge"})[1]
    assert body["zones"] == ["us-east-1a"] and body["stale"] is True
    # The failed lookup wrote no item: a stale answer is never replaced by an empty one.
    assert json.loads(t.items["offerings#us-east-1"]["data"]) == {"p5": ["p5.48xlarge"]}
    assert "us-east-1" not in api._region_down


def test_a_refresh_of_stale_data_never_eats_the_time_the_answer_needs(matrix, monkeypatch):
    """The refresh budget is the invocation's remaining time less the reserve, so a request near its
    deadline answers from the stale item and asks nobody."""
    stub, t, clock = matrix
    t.items["offerings#us-east-1"] = stale_item("us-east-1", {"p5": ["p5.48xlarge"]})
    stub("ec2", "us-east-1")[1].activate()  # no responses queued: any EC2 call fails the test

    class Context:
        @staticmethod
        def get_remaining_time_in_millis():
            return int((api.DEADLINE_RESERVE_SECONDS - 1) * 1000)

    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": "/api/instance-types",
                     "queryStringParameters": {"region": "us-east-1"}}, Context())
    assert json.loads(r["body"])["stale"] is True
    assert t.puts == [] and api._revalidate_budget() == 0


def test_a_whole_lambda_timeout_leaves_room_for_the_refresh_budget(monkeypatch):
    class Context:
        @staticmethod
        def get_remaining_time_in_millis():
            return 25_000

    api._set_deadline(Context())
    assert api._revalidate_budget() == api.STALE_REFRESH_BUDGET_SECONDS
    # Without a usable context (a local run or a test) the budget falls back to its own limit.
    api._set_deadline(None)
    assert api._time_left() is None and api._revalidate_budget() == api.STALE_REFRESH_BUDGET_SECONDS


def test_an_item_written_before_the_freshness_window_existed_is_fresh_until_it_expires(matrix):
    stub, t, clock = matrix
    old = item("us-east-1", {"p5": ["p5.48xlarge"]})
    assert "refreshAfter" not in old
    t.items["offerings#us-east-1"] = old
    stub("ec2", "us-east-1")[1].activate()  # no responses queued: any EC2 call fails the test
    body = call("/api/instance-types", {"region": "us-east-1"})[1]
    assert body["instanceTypes"] == ["p5.48xlarge"] and "stale" not in body


def test_matrix_expired_other_patterns_or_no_zones_count_as_miss(matrix):
    stub, t, clock = matrix
    t.items["offerings#us-east-1"] = item("us-east-1", {}, expires=clock[0])
    t.items["offerings#us-west-2"] = item("us-west-2", {}, patterns="p4d.*")
    _, east = stub("ec2", "us-east-1")
    _, west = stub("ec2", "us-west-2")
    offerings(east, ["p5.48xlarge"])
    zones(east, [])
    offerings(west, [])
    zones(west, [])
    east.activate()
    west.activate()
    body = call("/api/offering-matrix")[1]
    assert body["regions"][1]["families"] == {} and body["families"] == ["p5"]
    assert sorted(t.puts) == ["offerings#us-east-1", "offerings#us-west-2"]


def test_item_written_before_zones_existed_is_fetched_again(matrix):
    stub, t, clock = matrix
    old = item("us-east-1", {"p5": ["p5.48xlarge"]})
    del old["zones"]
    t.items["offerings#us-east-1"] = old
    _, east = stub("ec2", "us-east-1")
    offerings(east, ["p5.48xlarge"])
    zones(east, [("p5.48xlarge", "us-east-1a")])
    east.activate()
    status, body = call("/api/instance-type-zones", {"region": "us-east-1", "instanceType": "p5.48xlarge"})
    assert status == 200 and body["zones"] == ["us-east-1a"]
    assert json.loads(t.items["offerings#us-east-1"]["zones"]) == {"p5.48xlarge": ["us-east-1a"]}


def test_matrix_cache_read_failure_falls_back_to_live(matrix, monkeypatch):
    stub, _, _ = matrix
    t = CacheTable(fail_get=True)
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    for region in ("us-east-1", "us-west-2"):
        _, s = stub("ec2", region)
        offerings(s, ["p5.48xlarge"])
        zones(s, [])
        s.activate()
    body = call("/api/offering-matrix")[1]
    # Two offering items and the two no-answer markers of the fan-out; every read fails, so every
    # Region is fetched live and none is skipped.
    assert body["fromCache"] is False and t.gets == 4


def test_matrix_refresh_takes_one_lock_per_region_for_a_minute(matrix):
    stub, t, clock = matrix
    for r in ("us-east-1", "us-west-2"):
        t.items[f"offerings#{r}"] = item(r, {"g6e": ["g6e.2xlarge"]}, fetched="old", expires=clock[0] + 3600)
    # us-west-2's window is still running (another request refreshed it 20 seconds ago).
    t.items["#lock#offerings#us-west-2"] = {"cacheKey": "#lock#offerings#us-west-2", "lockedUntil": clock[0] + 40}
    _, east = stub("ec2", "us-east-1")
    offerings(east, ["p5.48xlarge"])
    zones(east, [])
    offerings(east, ["p5.48xlarge", "g7e.2xlarge"])
    zones(east, [])
    east.activate()
    # For the second call, when us-west-2's window has ended.
    _, west = stub("ec2", "us-west-2")
    offerings(west, ["g6e.2xlarge"])
    zones(west, [])
    west.activate()
    body = call("/api/offering-matrix", {"refresh": "true"})[1]
    east_row, west_row = body["regions"]
    assert east_row["families"] == {"p5": ["p5.48xlarge"]} and east_row["fromCache"] is False
    assert east_row["refreshed"] is True and "retryAfterSeconds" not in east_row
    # The blocked Region says so, with the seconds until its window ends.
    assert west_row["fromCache"] is True and west_row["fetchedAt"] == "old"
    assert (west_row["refreshed"], west_row["retryAfterSeconds"]) == (False, 40)
    assert t.items["#lock#offerings#us-east-1"]["lockedUntil"] == clock[0] + 60
    assert "note" not in body
    # Inside us-east-1's window: the cached (now fresh) item, and the wait until the window ends.
    clock[0] += 59
    east_row, west_row = call("/api/offering-matrix", {"refresh": "true"})[1]["regions"]
    assert east_row["fromCache"] is True and (east_row["refreshed"], east_row["retryAfterSeconds"]) == (False, 1)
    assert west_row["fromCache"] is False and west_row["refreshed"] is True
    # After the window, a refresh goes live again.
    clock[0] += 2
    east_row, west_row = call("/api/offering-matrix", {"refresh": "true"})[1]["regions"]
    assert east_row["fromCache"] is False and east_row["families"]["g7e"] == ["g7e.2xlarge"]
    assert (west_row["fromCache"], west_row["refreshed"], west_row["retryAfterSeconds"]) == (True, False, 58)


def test_matrix_rejects_bad_refresh_value(matrix):
    assert call("/api/offering-matrix", {"refresh": "yes"}) == (400, {"error": "refresh must be true or false", "code": "bad-parameter",
                                                                      "params": {"name": "refresh"}})


def test_matrix_all_regions_failing_is_not_from_cache(matrix):
    stub, _, _ = matrix
    for region in ("us-east-1", "us-west-2"):
        _, s = stub("ec2", region)
        s.add_client_error("describe_instance_type_offerings", "RequestLimitExceeded")
        s.activate()
    body = call("/api/offering-matrix")[1]
    assert body["fromCache"] is False and body["families"] == []
    assert [r["error"] for r in body["regions"]] == ["AWS call failed (RequestLimitExceeded)"] * 2
    assert [(r["errorCode"], r["errorParams"]) for r in body["regions"]] == [("aws-error", {"awsCode": "RequestLimitExceeded"})] * 2


# ------------------------------------------- one source for the instance lists
def test_lists_validation_and_matrix_read_the_same_item(matrix, monkeypatch):
    """/api/instance-types, /api/instance-type-zones, the validation helpers, and the matrix all read
    offerings#<Region>: one live fetch serves all of them, with one fetchedAt."""
    stub, t, clock = matrix
    _, east = stub("ec2", "us-east-1")
    offerings(east, ["p5.48xlarge", "g6e.2xlarge"])
    zones(east, [("p5.48xlarge", "us-east-1b"), ("p5.48xlarge", "us-east-1a")])
    east.activate()
    status, types = call("/api/instance-types", {"region": "us-east-1"})
    assert status == 200 and types["instanceTypes"] == ["g6e.2xlarge", "p5.48xlarge"] and types["fromCache"] is False
    # Same container: the memory copy. Same fetchedAt everywhere.
    status, zone_body = call("/api/instance-type-zones", {"region": "us-east-1", "instanceType": "p5.48xlarge"})
    assert zone_body["zones"] == ["us-east-1a", "us-east-1b"] and zone_body["fromCache"] is True
    assert zone_body["fetchedAt"] == types["fetchedAt"]
    assert api.region_offerings("us-east-1") == ["g6e.2xlarge", "p5.48xlarge"]
    assert call("/api/instance-type-zones", {"region": "us-east-1", "instanceType": "g6e.2xlarge"})[1]["zones"] == []
    # A new container (empty memory) reads the DynamoDB item, no EC2 call.
    monkeypatch.setattr(api, "_offering_mem", {})
    t.items["offerings#us-west-2"] = item("us-west-2", {})
    row = call("/api/offering-matrix")[1]["regions"][0]
    assert row["fromCache"] is True and row["fetchedAt"] == types["fetchedAt"]
    assert t.puts == ["offerings#us-east-1"]


def test_memory_copy_lasts_a_minute_then_dynamodb_then_live(matrix, monkeypatch):
    stub, t, clock = matrix
    mono = [5000.0]
    monkeypatch.setattr(api, "_now", lambda: mono[0])
    _, east = stub("ec2", "us-east-1")
    offerings(east, ["p5.48xlarge"])
    zones(east, [])
    offerings(east, ["p5.48xlarge", "g7e.2xlarge"])
    zones(east, [])
    east.activate()
    assert api.region_offerings("us-east-1") == ["p5.48xlarge"]
    t.gets = 0
    mono[0] += 59
    api.region_offerings("us-east-1")
    assert t.gets == 0  # memory
    mono[0] += 2
    api.region_offerings("us-east-1")
    assert t.gets == 1  # DynamoDB, still no EC2 call
    # Past offeringCacheHours the item is ignored and EC2 is asked again.
    clock[0] += 6 * 3600
    mono[0] += 61
    assert api.region_offerings("us-east-1") == ["g7e.2xlarge", "p5.48xlarge"]


def test_failed_lookup_is_not_cached_and_raises_for_one_region(matrix):
    stub, t, _ = matrix
    _, east = stub("ec2", "us-east-1")
    east.add_client_error("describe_instance_type_offerings", "RequestLimitExceeded")
    offerings(east, ["p5.48xlarge"])
    zones(east, [])
    east.activate()
    status, body = call("/api/instance-types", {"region": "us-east-1"})
    assert (status, body["code"]) == (502, "aws-error") and t.puts == [] and api._offering_mem == {}
    assert call("/api/instance-types", {"region": "us-east-1"})[1]["instanceTypes"] == ["p5.48xlarge"]


def test_cache_items_are_valid_dynamodb_requests(monkeypatch):
    """The real table resource accepts the cache and lock items (shape check via Stubber)."""
    res = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    tbl = res.Table("c")
    s = Stubber(tbl.meta.client)
    monkeypatch.setattr(api, "offering_cache_table", lambda: tbl)
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", FAMILIES)
    lock = {"TableName": "c", "Item": {"cacheKey": "#lock#offerings#us-east-1", "lockedUntil": 160, "expires": 160 + 86400},
            "ConditionExpression": "attribute_not_exists(cacheKey) OR lockedUntil <= :now",
            "ExpressionAttributeValues": {":now": 100}}
    s.add_response("put_item", {}, lock)
    s.add_client_error("put_item", "ConditionalCheckFailedException", http_status_code=400, expected_params=lock)
    # A blocked claim reads the lock (strongly consistent) to say how long the window still runs.
    s.add_response("get_item", {"Item": {"cacheKey": {"S": "#lock#offerings#us-east-1"}, "lockedUntil": {"N": "160"}}},
                   {"TableName": "c", "Key": {"cacheKey": "#lock#offerings#us-east-1"}, "ConsistentRead": True})
    s.add_response("get_item", {"Item": {"cacheKey": {"S": "offerings#us-east-1"}, "data": {"S": '{"p5": ["p5.48xlarge"]}'},
                                         "zones": {"S": '{"p5.48xlarge": ["us-east-1a"]}'},
                                         "fetchedAt": {"S": "t0"}, "patterns": {"S": families.cache_tag()}, "expires": {"N": "500"}}},
                   {"TableName": "c", "Key": {"cacheKey": "offerings#us-east-1"}})
    s.add_response("put_item", {}, {"TableName": "c", "Item": {
        "cacheKey": "offerings#us-west-2", "data": '{"p5": ["p5.48xlarge"]}', "zones": '{"p5.48xlarge": ["us-west-2a"]}',
        "accelerators": '["g6e", "p5"]', "fetchedAt": "t1", "patterns": families.cache_tag(),
        "refreshAfter": 100 + api.OFFERING_CACHE_HOURS * 3600, "expires": 100 + api.OFFERING_KEEP_SECONDS}})
    s.activate()
    assert api._claim_refresh(100, "offerings#us-east-1") == 0
    assert api._claim_refresh(100, "offerings#us-east-1") == 60
    # An item written before the accelerator list existed still serves; its list is unknown (None).
    assert api._read_cached("us-east-1", 100) == {"families": {"p5": ["p5.48xlarge"]}, "zones": {"p5.48xlarge": ["us-east-1a"]},
                                                  "accelerators": None, "blockTypes": None, "fetchedAt": "t0",
                                                  "fromCache": True}
    api._write_cached("us-west-2", {"families": {"p5": ["p5.48xlarge"]}, "zones": {"p5.48xlarge": ["us-west-2a"]},
                                    "accelerators": ["g6e", "p5"], "fetchedAt": "t1"}, 100)
    s.assert_no_pending_responses()


def test_config_reports_offering_cache_hours(monkeypatch):
    monkeypatch.setattr(api, "OFFERING_CACHE_HOURS", 12)
    assert call("/api/config")[1]["offeringCacheHours"] == 12


def test_recent_probes_follows_pages_and_returns_oldest_first(monkeypatch):
    tbl = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x").Table("t")
    s = Stubber(tbl.meta.client)

    def item(ts):
        return {"region": {"S": "us-east-1"}, "sk": {"S": ts}}

    # Separate dict objects: the resource layer deserializes the response in place.
    s.add_response("query", {
        "Items": [item("2026-01-01T00:00:03.000000Z#c"), item("2026-01-01T00:00:02.000000Z#b")],
        "LastEvaluatedKey": item("2026-01-01T00:00:02.000000Z#b"),
    })
    s.add_response("query", {"Items": [item("2026-01-01T00:00:01.000000Z#a")]})
    s.add_response("query", {"Items": []})  # must stay unused
    s.activate()
    monkeypatch.setattr(api, "table", lambda: tbl)
    since = api.datetime(2026, 1, 1, tzinfo=api.timezone.utc)
    items = api._recent_probes("us-east-1", since)
    assert [i["sk"][:21] for i in items] == [
        "2026-01-01T00:00:01.0", "2026-01-01T00:00:02.0", "2026-01-01T00:00:03.0"]
    assert len(s._queue) == 1


def test_quota_rows_cover_trn_inf_and_name_their_family_without_dl(stub):
    _, sq = stub("service-quotas")
    sq.add_response("list_aws_default_service_quotas", {"Quotas": [
        quota("L-T", "Running On-Demand Trn instances", 0.0),
        quota("L-TS", "All Trn Spot Instance Requests", 0.0),
        quota("L-I", "Running On-Demand Inf instances", 0.0),
        quota("L-D", "Running On-Demand DL instances", 0.0),
        quota("L-DS", "All DL Spot Instance Requests", 0.0),
        quota("L-G", "Running On-Demand G and VT instances", 0.0),
        quota("L-CB", "Concurrent TRN2 Capacity Blocks per account", 1.0, unit="Count"),
        quota("L-F", "Running On-Demand F instances", 0.0),
    ]})
    sq.add_response("list_service_quotas", {"Quotas": []})
    sq.activate()
    rows = call("/api/quotas", {"region": "us-east-1"})[1]["quotas"]
    assert {r["name"]: (r["category"], r["family"]) for r in rows} == {
        "Running On-Demand G and VT instances": ("on-demand", "g"),
        "Running On-Demand Inf instances": ("on-demand", "inf"),
        "Running On-Demand Trn instances": ("on-demand", "trn"),
        "All Trn Spot Instance Requests": ("spot", "trn"),
        "Concurrent TRN2 Capacity Blocks per account": ("capacity-block", "cb:trn2"),
    }
