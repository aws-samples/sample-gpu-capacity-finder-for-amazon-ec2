"""GET /api/on-demand-price: On-Demand list prices from the Price List Query API, through the
shared DynamoDB cache. The pricing client goes through a botocore Stubber, so the request shape and
filters are checked against the service model; the cache table is an in-memory fake."""
import json
import logging
import os

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

NOW = 2_000_000_000


class CacheTable:
    def __init__(self):
        self.items = {}
        self.puts = []

    def get_item(self, Key, **_):
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, **_):
        self.items[Item["cacheKey"]] = Item
        self.puts.append(Item["cacheKey"])


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(api, "REGIONS", ["us-east-1", "us-west-2"])
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", ["p5.*", "g6e.*", "g7*"])
    monkeypatch.setattr(api, "_clients", {})
    offered = {"us-east-1": ["g6e.2xlarge", "p5.48xlarge"], "us-west-2": ["g6e.2xlarge"]}
    monkeypatch.setattr(api, "region_offerings", lambda r: offered[r])
    clock = {"epoch": NOW, "mono": 1000.0}
    monkeypatch.setattr(api, "_epoch", lambda: clock["epoch"])
    monkeypatch.setattr(api, "_now", lambda: clock["mono"])
    t = CacheTable()
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    c = boto3.client("pricing", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    api._clients[("pricing", "us-east-1")] = c
    s = Stubber(c)
    s.activate()
    yield s, t, clock
    s.assert_no_pending_responses()


def call(q):
    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": "/api/on-demand-price",
                     "queryStringParameters": q}, None)
    return r["statusCode"], json.loads(r["body"])


def advance(clock, seconds):
    clock["epoch"] += seconds
    clock["mono"] += seconds


def filters(itype, region):
    pairs = [("instanceType", itype), ("regionCode", region), ("operatingSystem", "Linux"), ("tenancy", "Shared"),
             ("preInstalledSw", "NA"), ("capacitystatus", "Used"), ("termType", "OnDemand"), ("marketoption", "OnDemand")]
    return [{"Type": "TERM_MATCH", "Field": f, "Value": v} for f, v in pairs]


def product(usd, unit="Hrs"):
    """One Price List entry as the API returns it: a JSON string (shape from a real GetProducts answer)."""
    return json.dumps({
        "product": {"productFamily": "Compute Instance", "attributes": {"instanceType": "p5.48xlarge", "regionCode": "us-east-1"}},
        "serviceCode": "AmazonEC2",
        "terms": {"OnDemand": {"SKU.JRTCKXETXF": {"priceDimensions": {"SKU.JRTCKXETXF.6YS6EN2CT7": {
            "unit": unit, "pricePerUnit": {"USD": usd}, "description": f"${usd} per On Demand Linux p5.48xlarge Instance Hour"}}}}},
    })


def expect(s, itype, region, entries):
    s.add_response("get_products", {"FormatVersion": "aws_v1", "PriceList": entries},
                   {"ServiceCode": "AmazonEC2", "Filters": filters(itype, region), "FormatVersion": "aws_v1"})


Q = {"region": "us-east-1", "instanceType": "p5.48xlarge"}


def test_miss_asks_the_price_list_with_and_filters_and_caches_24_hours(env):
    s, t, _ = env
    expect(s, "p5.48xlarge", "us-east-1", [product("55.0400000000")])
    status, body = call(Q)
    assert status == 200
    assert body == {"region": "us-east-1", "instanceType": "p5.48xlarge", "usdPerHour": "55.04", "currency": "USD",
                    "kind": "list-price", "fetchedAt": body["fetchedAt"], "fromCache": False}
    assert body["fetchedAt"].endswith("Z")
    item = t.items["price-od#p5.48xlarge#us-east-1"]
    assert json.loads(item["data"]) == {"usdPerHour": "55.04"}
    assert item["expires"] == NOW + api.OD_PRICE_TTL_SECONDS == NOW + 86400 and item["fetchedAt"] == body["fetchedAt"]
    # Same container: the memory copy, with the stored fetchedAt.
    assert call(Q)[1] == {**body, "fromCache": True}


def test_a_new_container_reads_dynamodb_until_the_item_expires(env, monkeypatch):
    s, t, clock = env
    expect(s, "p5.48xlarge", "us-east-1", [product("55.04")])
    first = call(Q)[1]
    monkeypatch.setattr(api, "_price_cache", {})
    advance(clock, 86399)
    assert call(Q)[1] == {**first, "fromCache": True}
    # The memory copy never outlives the item.
    advance(clock, 1)
    expect(s, "p5.48xlarge", "us-east-1", [product("50.00")])
    again = call(Q)[1]
    assert again["usdPerHour"] == "50" and again["fromCache"] is False


def test_memory_copy_lasts_a_minute(env):
    s, t, clock = env
    expect(s, "p5.48xlarge", "us-east-1", [product("1.5")])
    call(Q)
    t.items.clear()
    advance(clock, 59)
    assert call(Q)[1]["fromCache"] is True
    advance(clock, 2)
    expect(s, "p5.48xlarge", "us-east-1", [product("1.5")])
    assert call(Q)[1]["fromCache"] is False


def test_no_match_is_not_found_and_cached_for_an_hour(env):
    s, t, clock = env
    expect(s, "g6e.2xlarge", "us-west-2", [])
    q = {"region": "us-west-2", "instanceType": "g6e.2xlarge"}
    status, body = call(q)
    assert status == 200 and body["usdPerHour"] is None and body["reason"] == "not-found"
    assert body["currency"] == "USD" and body["kind"] == "list-price"
    item = t.items["price-od#g6e.2xlarge#us-west-2"]
    assert json.loads(item["data"]) == {"usdPerHour": None} and item["expires"] == NOW + 3600
    assert call(q)[1] == {**body, "fromCache": True}
    advance(clock, 3600)
    expect(s, "g6e.2xlarge", "us-west-2", [product("2.24208")])
    again = call(q)[1]
    assert again["usdPerHour"] == "2.24208" and "reason" not in again


def test_several_matches_take_the_lowest_with_one_warning(env, caplog):
    s, _, _ = env
    expect(s, "p5.48xlarge", "us-east-1", [product("60.00"), product("55.04"), product("57.1")])
    with caplog.at_level(logging.WARNING):
        body = call(Q)[1]
    assert body["usdPerHour"] == "55.04"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "On-Demand list prices" in r.getMessage()]
    assert len(warnings) == 1 and "3" in warnings[0].getMessage()


def test_zero_and_non_hourly_dimensions_are_not_prices(env, caplog):
    """A zero price (the Capacity Block product's On-Demand term, should a filter ever let it in) or
    a non-hourly dimension is ignored."""
    s, _, _ = env
    expect(s, "p5.48xlarge", "us-east-1", [product("0.0000000000"), product("9.99", unit="Quantity"), "not json"])
    with caplog.at_level(logging.WARNING):
        body = call(Q)[1]
    assert body["usdPerHour"] is None and body["reason"] == "not-found"
    assert not [r for r in caplog.records if "On-Demand list prices" in r.getMessage()]


def test_price_list_errors_are_not_cached(env):
    s, t, _ = env
    s.add_client_error("get_products", "ThrottlingException", http_status_code=400)
    status, body = call(Q)
    assert (status, body["code"], body["params"]) == (502, "aws-error", {"awsCode": "ThrottlingException"})
    assert t.puts == [] and api._price_cache == {}
    expect(s, "p5.48xlarge", "us-east-1", [product("55.04")])
    assert call(Q)[1]["usdPerHour"] == "55.04"


@pytest.mark.parametrize("q,status,code", [
    ({"region": "eu-west-1", "instanceType": "p5.48xlarge"}, 400, "region-not-enabled"),
    ({"region": "us-east-1", "instanceType": "m5.large"}, 400, "type-not-enabled"),
    ({"region": "us-west-2", "instanceType": "p5.48xlarge"}, 400, "type-not-offered"),
    ({"region": "us-east-1"}, 400, "type-not-enabled"),
])
def test_validates_region_and_type_before_the_price_list(env, q, status, code):
    assert call(q)[0] == status and call(q)[1]["code"] == code


def test_refresh_is_not_offered(env):
    """No forced refresh for list prices: refresh=true is ignored and the cached answer stays."""
    s, t, _ = env
    expect(s, "p5.48xlarge", "us-east-1", [product("55.04")])
    call(Q)
    body = call({**Q, "refresh": "true"})[1]
    assert body["fromCache"] is True and "refreshed" not in body
    assert not [k for k in t.items if k.startswith("#lock#")]


def test_pricing_client_is_us_east_1():
    assert api.PRICING_REGION == "us-east-1"


def test_config_reports_the_price_cache_lifetimes():
    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": "/api/config", "queryStringParameters": None}, None)
    assert json.loads(r["body"])["priceCache"] == {"onDemandHours": 24, "spotMinutes": 30}


@pytest.mark.parametrize("text,expected", [("55.0400000000", "55.04"), ("10.000", "10"), ("0.4200", "0.42"), ("2.24208", "2.24208")])
def test_decimal_text(text, expected):
    assert api._decimal_text(api.Decimal(text)) == expected


def test_cache_item_is_a_valid_dynamodb_request(monkeypatch):
    res = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    tbl = res.Table("c")
    s = Stubber(tbl.meta.client)
    monkeypatch.setattr(api, "offering_cache_table", lambda: tbl)
    s.add_response("put_item", {}, {"TableName": "c", "Item": {
        "cacheKey": "price-od#p5.48xlarge#us-east-1", "data": '{"usdPerHour": "55.04"}', "fetchedAt": "t1", "expires": 100 + 86400}})
    s.add_response("get_item", {"Item": {"cacheKey": {"S": "price-od#p5.48xlarge#us-east-1"}, "data": {"S": '{"usdPerHour": "55.04"}'},
                                         "fetchedAt": {"S": "t1"}, "expires": {"N": "86500"}}},
                   {"TableName": "c", "Key": {"cacheKey": "price-od#p5.48xlarge#us-east-1"}})
    s.activate()
    api._write_shared("price-od#p5.48xlarge#us-east-1", {"usdPerHour": "55.04"}, "t1", 100, api.OD_PRICE_TTL_SECONDS)
    api._price_cache.clear()
    assert api._read_shared("price-od#p5.48xlarge#us-east-1", 100) == {"data": {"usdPerHour": "55.04"}, "fetchedAt": "t1",
                                                                      "expires": 86500, "freshUntil": 86500}
    s.assert_no_pending_responses()


def test_unreadable_cache_falls_back_to_the_price_list(env, monkeypatch):
    s, _, _ = env

    class Broken:
        def get_item(self, **_):
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")

        def put_item(self, **_):
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "PutItem")

    monkeypatch.setattr(api, "offering_cache_table", lambda: Broken())
    expect(s, "p5.48xlarge", "us-east-1", [product("55.04")])
    assert call(Q)[1]["usdPerHour"] == "55.04"
