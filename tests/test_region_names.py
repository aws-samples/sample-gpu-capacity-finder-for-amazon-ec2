"""Region long names in /api/config: SSM public parameters (GetParameters, 10 names per call) in
the function's own Region, one shared 24-hour cache item with a 60-second memory layer, never an
error. The SSM client is a botocore Stubber; the cache table is an in-memory fake."""
import json
import os

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import cache_fakes  # noqa: E402
import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

NOW = 2_000_000_000
PARAM = "/aws/service/global-infrastructure/regions/{}/longName"
NAMES = {"us-east-1": "US East (N. Virginia)", "us-west-2": "US West (Oregon)", "ap-northeast-1": "Asia Pacific (Tokyo)"}


class Table:
    def __init__(self):
        self.items, self.gets, self.puts = {}, [], []
        self.fail_get = self.fail_put = False

    def get_item(self, Key, **_):
        # /api/config's Spot score budget and Region group reads are counted apart.
        if not cache_fakes.is_registry(Key["cacheKey"]):
            self.gets.append(Key["cacheKey"])
        if self.fail_get:
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, **_):
        if self.fail_put:
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "PutItem")
        if not cache_fakes.is_registry(Item["cacheKey"]):
            self.puts.append(Item["cacheKey"])
        self.items[Item["cacheKey"]] = Item


@pytest.fixture
def env(monkeypatch, live_region_names):
    regions = ["us-east-1", "us-west-2", "ap-northeast-1"]
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": list(regions), "fetchedAt": None})
    clock = {"epoch": NOW, "mono": 1000.0}
    monkeypatch.setattr(api, "_epoch", lambda: clock["epoch"])
    monkeypatch.setattr(api, "_now", lambda: clock["mono"])
    t = Table()
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    ssm = boto3.client("ssm", region_name="us-west-2", aws_access_key_id="x", aws_secret_access_key="x")
    s = Stubber(ssm)
    s.activate()
    monkeypatch.setattr(api, "_names_client", lambda: ssm)
    yield s, t, clock, regions
    s.assert_no_pending_responses()


def answer(s, codes, known=NAMES):
    names = [PARAM.format(c) for c in sorted(codes)]
    resp = {"Parameters": [{"Name": PARAM.format(c), "Type": "String", "Value": known[c]} for c in sorted(codes) if c in known]}
    invalid = [PARAM.format(c) for c in sorted(codes) if c not in known]
    if invalid:
        resp["InvalidParameters"] = invalid
    s.add_response("get_parameters", resp, {"Names": names})


def config():
    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": "/api/config", "queryStringParameters": None}, None)
    assert r["statusCode"] == 200
    return json.loads(r["body"])


def test_miss_asks_ssm_and_stores_one_item_for_a_day(env):
    s, t, _, regions = env
    answer(s, regions)
    body = config()
    assert body["regionNames"] == NAMES and body["regions"] == regions
    item = t.items["regionnames#v1"]
    assert json.loads(item["names"]) == NAMES and json.loads(item["asked"]) == sorted(regions)
    assert item["expires"] == NOW + 24 * 3600 and t.puts == ["regionnames#v1"]


def test_memory_then_item_then_expiry(env, monkeypatch):
    s, t, clock, regions = env
    answer(s, regions)
    config()
    # Within a minute: the container's copy, no table read, no SSM call.
    reads = len(t.gets)
    clock["mono"] += 59
    assert config()["regionNames"] == NAMES and len(t.gets) == reads
    # A new container: the shared item, no SSM call.
    monkeypatch.setattr(api, "_region_names_mem", {})
    assert config()["regionNames"] == NAMES and len(t.gets) == reads + 1
    # After 24 hours: SSM again.
    clock["epoch"] += 24 * 3600
    clock["mono"] += 61
    answer(s, regions, {**NAMES, "us-east-1": "US East (Virginia)"})
    assert config()["regionNames"]["us-east-1"] == "US East (Virginia)"


def test_names_only_for_listed_regions_and_a_new_region_is_added(env, monkeypatch):
    s, t, clock, regions = env
    t.items["regionnames#v1"] = {"cacheKey": "regionnames#v1", "expires": NOW + 600,
                                 "names": json.dumps({"us-east-1": NAMES["us-east-1"], "eu-west-1": "Europe (Ireland)",
                                                      "us-west-2": NAMES["us-west-2"]}),
                                 "asked": json.dumps(["eu-west-1", "us-east-1", "us-west-2"])}
    # Only the Region the item has not asked about yet goes to SSM; eu-west-1 is not listed.
    answer(s, ["ap-northeast-1"])
    assert config()["regionNames"] == NAMES
    stored = json.loads(t.items["regionnames#v1"]["names"])
    assert stored["ap-northeast-1"] == NAMES["ap-northeast-1"] and "eu-west-1" in stored
    assert t.items["regionnames#v1"]["expires"] == NOW + 600


def test_an_unknown_region_is_absent_and_not_asked_again(env, monkeypatch):
    s, t, _, regions = env
    regions.append("xx-new-1")
    answer(s, regions)
    assert "xx-new-1" not in config()["regionNames"]
    monkeypatch.setattr(api, "_region_names_mem", {})
    assert set(config()["regionNames"]) == set(NAMES)  # no SSM call queued: a call would fail


def test_more_than_ten_regions_take_several_calls(env, monkeypatch):
    s, _, _, _ = env
    many = [f"xx-r{i:02d}-1" for i in range(23)]
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": list(many), "fetchedAt": None})
    monkeypatch.setattr(api, "CACHE_WORKERS", 1)  # the Stubber answers in queue order
    known = {c: c.upper() for c in many}
    for i in range(0, 23, 10):
        answer(s, many[i:i + 10], known)
    assert config()["regionNames"] == known


@pytest.mark.parametrize("failure", ["ssm", "table", "both"])
def test_failures_never_fail_config_and_errors_are_not_cached(env, failure):
    s, t, _, regions = env
    if failure in ("ssm", "both"):
        s.add_client_error("get_parameters", "AccessDeniedException", http_status_code=400)
    else:
        answer(s, regions)
    if failure in ("table", "both"):
        t.fail_get = t.fail_put = True
    body = config()
    if failure == "table":
        assert body["regionNames"] == NAMES
    else:
        assert body["regionNames"] == {} and "regionnames#v1" not in t.items and api._region_names_mem == {}
    assert body["regions"] == regions


def test_a_failed_lookup_keeps_the_names_already_known(env):
    s, t, _, regions = env
    t.items["regionnames#v1"] = {"cacheKey": "regionnames#v1", "expires": NOW + 600,
                                 "names": json.dumps({"us-east-1": NAMES["us-east-1"]}), "asked": json.dumps(["us-east-1"])}
    s.add_client_error("get_parameters", "ThrottlingException", http_status_code=400)
    assert config()["regionNames"] == {"us-east-1": NAMES["us-east-1"]}
    assert json.loads(t.items["regionnames#v1"]["asked"]) == ["us-east-1"] and t.puts == []


def test_unexpected_errors_answer_without_names(env, monkeypatch):
    def boom(_codes):
        raise RuntimeError("no endpoint")
    monkeypatch.setattr(api, "_fetch_region_names", boom)
    assert config()["regionNames"] == {}


def test_broken_item_is_a_miss(env):
    s, t, _, regions = env
    t.items["regionnames#v1"] = {"cacheKey": "regionnames#v1", "expires": NOW + 600, "names": "not json", "asked": "[]"}
    answer(s, regions)
    assert config()["regionNames"] == NAMES


def test_the_lookup_uses_the_functions_own_region(monkeypatch):
    monkeypatch.setattr(api, "_clients", {})
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    c = api._names_client()
    assert c.meta.region_name == "ap-northeast-1" and c.meta.service_model.service_name == "ssm"
    assert api._names_client() is c


def test_default_tests_stub_the_lookup():
    """Outside the dedicated tests the lookup knows no names, so /api/config sends no SSM call."""
    assert config()["regionNames"] == {}
