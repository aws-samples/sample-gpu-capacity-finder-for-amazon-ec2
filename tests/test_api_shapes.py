"""Run the handlers against botocore Stubbers: request and response shapes are checked
against the real service models, so a wrong parameter name or type fails here."""
import json
import os
from datetime import datetime, timezone

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import families  # noqa: E402
import pytest  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(api, "_clients", {})
    monkeypatch.setattr(api, "_ami_cache", {})
    monkeypatch.setattr(api, "_sg_cache", {})
    monkeypatch.setattr(api, "_offering_mem", {})
    monkeypatch.setattr(api, "_quota_cache", {})
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


def offer(s, types, extra_page=None):
    row = lambda t: {"InstanceType": t, "LocationType": "region", "Location": "us-east-1"}
    if extra_page:
        s.add_response("describe_instance_type_offerings",
                       {"InstanceTypeOfferings": [row(t) for t in extra_page], "NextToken": "n"})
    s.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [row(t) for t in types]})
    # The zone-level call of the same refresh cycle (no zones needed by these tests).
    s.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": []}, AZ_PARAMS)


AZ_PARAMS = {"LocationType": "availability-zone", "Filters": [{"Name": "instance-type", "Values": families.filter_values()}]}


def test_instance_types_paginates_filters_and_caches(stub):
    _, s = stub("ec2")
    s.add_response(
        "describe_instance_type_offerings",
        {"InstanceTypeOfferings": [{"InstanceType": "p5.48xlarge", "LocationType": "region", "Location": "us-east-1"}],
         "NextToken": "n"},
        {"LocationType": "region", "Filters": [{"Name": "instance-type", "Values": api.region_offering_patterns()}]},
    )
    s.add_response(
        "describe_instance_type_offerings",
        {"InstanceTypeOfferings": [{"InstanceType": "g7e.2xlarge", "LocationType": "region", "Location": "us-east-1"},
                                   {"InstanceType": "m5.large", "LocationType": "region", "Location": "us-east-1"}]},
        {"LocationType": "region", "Filters": [{"Name": "instance-type", "Values": api.region_offering_patterns()}], "NextToken": "n"},
    )
    s.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": []}, AZ_PARAMS)
    s.activate()
    status, body = call("/api/instance-types", {"region": "us-east-1"})
    assert status == 200 and body["instanceTypes"] == ["g7e.2xlarge", "p5.48xlarge"]
    assert body["fromCache"] is False and body["fetchedAt"].endswith("Z")
    # Served from the cache, no extra stub; fetchedAt is the stored one, not this request's time.
    assert call("/api/instance-types", {"region": "us-east-1"})[1] == {**body, "fromCache": True}


def test_instance_type_zones_lists_only_offering_zones(stub):
    _, s = stub("ec2")
    s.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [
        {"InstanceType": t, "LocationType": "region", "Location": "us-east-1"} for t in ("p5.48xlarge", "g6e.2xlarge")]})
    s.add_response(
        "describe_instance_type_offerings",
        {"InstanceTypeOfferings": [{"InstanceType": "p5.48xlarge", "LocationType": "availability-zone", "Location": "us-east-1b"},
                                   {"InstanceType": "g6e.2xlarge", "LocationType": "availability-zone", "Location": "us-east-1c"},
                                   {"InstanceType": "p5.48xlarge", "LocationType": "availability-zone", "Location": "us-east-1a"}]},
        AZ_PARAMS,
    )
    s.activate()
    status, body = call("/api/instance-type-zones", {"region": "us-east-1", "instanceType": "p5.48xlarge"})
    assert status == 200 and body["zones"] == ["us-east-1a", "us-east-1b"]
    assert body["fromCache"] is False and body["fetchedAt"].endswith("Z")
    # Another type in the same Region: same cache item, no further EC2 call.
    status, other = call("/api/instance-type-zones", {"region": "us-east-1", "instanceType": "g6e.2xlarge"})
    assert other["zones"] == ["us-east-1c"] and other["fromCache"] is True and other["fetchedAt"] == body["fetchedAt"]


def test_type_not_offered_in_region_is_rejected(stub):
    _, s = stub("ec2")
    offer(s, ["g6e.12xlarge"])
    s.activate()
    status, body = call("/api/spot-prices", {"region": "us-east-1", "instanceType": "p5.48xlarge"})
    assert status == 400 and "not offered" in body["error"]


def test_capacity_blocks_paginates_and_sorts(stub):
    _, s = stub("ec2")
    off = lambda day, az: {"CapacityBlockOfferingId": f"cbr-{day}", "InstanceType": "p5.48xlarge", "AvailabilityZone": az,
                           "InstanceCount": 1, "StartDate": datetime(2030, 1, day, tzinfo=timezone.utc),
                           "EndDate": datetime(2030, 1, day + 1, tzinfo=timezone.utc), "CapacityBlockDurationHours": 24,
                           "UpfrontFee": "100.00", "CurrencyCode": "USD", "Tenancy": "default"}
    s.add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": [off(9, "us-east-1b")], "NextToken": "n"})
    s.add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": [off(3, "us-east-1a")]})
    s.activate()
    status, body = call("/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p5.48xlarge", "days": "1", "count": "2"})
    assert status == 200
    assert [o["AvailabilityZone"] for o in body["offerings"]] == ["us-east-1a", "us-east-1b"]


def test_spot_prices_keeps_newest_per_zone(stub):
    _, s = stub("ec2")
    offer(s, ["p5.48xlarge"])
    row = lambda az, price: {"AvailabilityZone": az, "InstanceType": "p5.48xlarge", "ProductDescription": "Linux/UNIX",
                             "SpotPrice": price, "Timestamp": NOW}
    s.add_response("describe_spot_price_history", {"SpotPriceHistory": [row("us-east-1a", "1.0"), row("us-east-1a", "9.9"), row("us-east-1b", "2.0")]})
    s.activate()
    _, body = call("/api/spot-prices", {"region": "us-east-1", "instanceType": "p5.48xlarge"})
    assert body["prices"] == {"us-east-1a": "1.0", "us-east-1b": "2.0"}


def test_quotas_match_gpu_names_and_prefer_applied_values(stub):
    _, s = stub("service-quotas")
    q = lambda code, name, v: {"QuotaCode": code, "QuotaName": name, "Value": v}
    s.add_response("list_aws_default_service_quotas", {"Quotas": [
        q("L-1", "Running On-Demand P instances", 0.0),
        q("L-2", "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances", 5.0),
        q("L-3", "All P5 Spot Instance Requests", 0.0),
        q("L-4", "All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests", 5.0),
    ]})
    s.add_response("list_service_quotas", {"Quotas": [
        q("L-1", "Running On-Demand P instances", 192.0),
        q("L-5", "Running On-Demand G and VT instances", 64.0),
    ]})
    s.activate()
    status, body = call("/api/quotas", {"region": "us-east-1"})
    assert status == 200
    assert {x["name"]: x["value"] for x in body["quotas"]} == {
        "All P5 Spot Instance Requests": 0.0,
        "Running On-Demand G and VT instances": 64.0,
        "Running On-Demand P instances": 192.0,
    }


# The test launch request shapes (CreateFleet instant, launch template, counters, cooldown lock) are
# in test_fleet_launch.py.
