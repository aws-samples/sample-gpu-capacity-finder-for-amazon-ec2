"""Routes that return instance types use the natural order from type_order.py."""
import json
import os

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import families  # noqa: E402
import pytest  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

FAMILIES = ["p5.*", "g6e.*", "g7*"]
RAW = ["p5.48xlarge", "g7e.48xlarge", "g6e.12xlarge", "g7.2xlarge", "g6e.2xlarge", "p5.4xlarge", "g7e.8xlarge", "g6e.xlarge"]
ORDERED = ["g6e.xlarge", "g6e.2xlarge", "g6e.12xlarge", "g7.2xlarge", "g7e.8xlarge", "g7e.48xlarge", "p5.4xlarge", "p5.48xlarge"]


@pytest.fixture
def ec2(monkeypatch):
    monkeypatch.setattr(api, "REGIONS", ["us-east-1", "us-west-2"])
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", FAMILIES)
    monkeypatch.setattr(api, "_offering_mem", {})
    monkeypatch.setattr(api, "_clients", {})
    stubbers = {}
    for region in ("us-east-1", "us-west-2"):
        c = boto3.client("ec2", region_name=region, aws_access_key_id="x", aws_secret_access_key="x")
        api._clients[("ec2", region)] = c
        stubbers[region] = Stubber(c)
    yield stubbers
    for s in stubbers.values():
        s.assert_no_pending_responses()


def offer(s, types, kind="region", location="us-east-1"):
    s.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [
        {"InstanceType": t, "LocationType": kind, "Location": location} for t in types]})


def call(path, q=None):
    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"sub": "u", "token_use": "id"}}}},
                     "rawPath": path, "queryStringParameters": q}, None)
    return r["statusCode"], json.loads(r["body"])


def test_instance_types_in_natural_order(ec2):
    offer(ec2["us-east-1"], RAW)
    offer(ec2["us-east-1"], [], "availability-zone")
    ec2["us-east-1"].activate()
    assert call("/api/instance-types", {"region": "us-east-1"})[1]["instanceTypes"] == ORDERED


def test_instance_type_zones_sorted_by_name(ec2):
    s = ec2["us-east-1"]
    offer(s, RAW)
    # One zone-level call for every allowed type, in the same refresh cycle as the Region list.
    s.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [
        {"InstanceType": "g6e.2xlarge", "LocationType": "availability-zone", "Location": z}
        for z in ("us-east-1f", "us-east-1a", "us-east-1c")]})
    s.activate()
    body = call("/api/instance-type-zones", {"region": "us-east-1", "instanceType": "g6e.2xlarge"})[1]
    assert body["zones"] == ["us-east-1a", "us-east-1c", "us-east-1f"]


class Cache:
    def __init__(self, items=None):
        self.items = dict(items or {})

    def get_item(self, Key):
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, **_):
        self.items[Item["cacheKey"]] = Item


def test_offering_matrix_families_and_sizes_in_natural_order(ec2, monkeypatch):
    # us-east-1 answers live; us-west-2 comes from a cache item written in the old plain-text order.
    old = {"p5": ["p5.48xlarge", "p5.4xlarge"], "g6e": ["g6e.12xlarge", "g6e.2xlarge"]}
    cache = Cache({"offerings#us-west-2": {"cacheKey": "offerings#us-west-2", "data": json.dumps(old), "zones": "{}", "fetchedAt": "t",
                                           "patterns": families.cache_tag(), "expires": 2_000_000_100}})
    monkeypatch.setattr(api, "offering_cache_table", lambda: cache)
    monkeypatch.setattr(api, "_epoch", lambda: 2_000_000_000)
    offer(ec2["us-east-1"], RAW)
    offer(ec2["us-east-1"], [], "availability-zone")
    ec2["us-east-1"].activate()
    body = call("/api/offering-matrix")[1]
    east, west = body["regions"]
    assert list(east["families"]) == ["g6e", "g7", "g7e", "p5"]
    assert [t for f in east["families"].values() for t in f] == ORDERED
    assert list(west["families"]) == ["g6e", "p5"] and west["families"]["g6e"] == ["g6e.2xlarge", "g6e.12xlarge"]
    assert west["families"]["p5"] == ["p5.4xlarge", "p5.48xlarge"]
    assert body["families"] == ["g6e", "g7", "g7e", "p5"]
