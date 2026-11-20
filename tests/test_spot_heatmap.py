"""Repeats of granted Spot placement score configurations from the API (the heatmap and the Spot
score routes share them): the error mapping, one failing group that does not fail the others, the
time budget, the worker bound, and the cache item shapes. Fake tables; nothing reaches AWS."""
import json
import os
import threading
import time

os.environ.update(SPOT_SCORE_FAMILIES="g6e.*,p5.*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import cache_fakes  # noqa: E402
import pytest  # noqa: E402
import score_groups as sg  # noqa: E402
import spot_cache  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402
from score_fakes import NOW, Table, state_item  # noqa: E402

REGIONS = ["us-east-1", "us-west-2"]
GPUS = {f"g6e.{n}xlarge": n for n in range(1, 31)}
GPUS.update({"p5.48xlarge": 8})
# 31 groups: g6e/1 .. g6e/30 and p5/8, each in both Regions.
GROUPS = {k: {"types": [t for t in GPUS if sg.group_key(t, GPUS) == k], "chunks": [list(REGIONS)], "added": {}}
          for k in {sg.group_key(t, GPUS) for t in GPUS}}


@pytest.fixture
def env(monkeypatch):
    t = Table()
    clock = {"epoch": NOW, "mono": 1000.0}
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    monkeypatch.setattr(api, "_epoch", lambda: clock["epoch"])
    monkeypatch.setattr(api, "_now", lambda: clock["mono"])
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": list(REGIONS), "fetchedAt": None})
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", ["g6e.*", "p5.*"])
    cache_fakes.seeded(t.items)
    t.items[sg.STATE_KEY] = state_item(GROUPS, GPUS, ["g6e.*", "p5.*"])
    for key, cfg in sg.configs(GROUPS):
        cache_fakes.register(t.items, cfg, NOW - 600)
    return t


def call(q):
    ev = {"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": "/api/spot-heatmap", "queryStringParameters": q}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def by_key(fn):
    return lambda cfg: fn(cfg.itype)


@pytest.mark.parametrize("code,message,expected", [
    ("MaxConfigLimitExceeded", "You have exceeded the number of new request configurations", "sps-limited"),
    ("InvalidParameterValue", "Request configuration limit reached for the 24-hour period", "sps-limited"),
    ("SomeLimitCode", "You exceeded the 24-hour limit", "sps-limited"),
    ("RequestLimitExceeded", "Request limit exceeded.", "throttled"),
    ("Throttling", "Rate exceeded", "throttled"),
    ("InvalidParameterValue", "The instance type is not valid", "sps-unsupported"),
    ("Unsupported", "The requested configuration is not supported", "sps-unsupported"),
    ("UnauthorizedOperation", "You are not authorized to perform this operation.", "sps-error"),
    ("MaxConfigLimitExceeded", "Something EC2 says", "sps-limited"),
])
def test_error_mapping(code, message, expected):
    err = api.sps_error(ClientError({"Error": {"Code": code, "Message": message}}, "GetSpotPlacementScores"))
    assert err["code"] == expected and set(err) == {"code", "message"}
    if expected == "sps-error":
        assert err["message"] == f"AWS call failed ({code})"
    # The text never names a number.
    assert not any(ch.isdigit() for ch in api.SPS_MESSAGES["sps-limited"])


def test_one_failing_group_does_not_fail_the_others(env, monkeypatch):
    t = env

    def fake(key):
        if key == "p5/8":
            raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "x"}}, "GetSpotPlacementScores")
        return {"us-east-1": 3}
    monkeypatch.setattr(api, "_region_spot_scores", by_key(fake))
    status, body = call({"types": "p5.48xlarge,g6e.2xlarge"})
    assert status == 200 and body["scores"] == {"g6e.2xlarge": {"us-east-1": 3}, "p5.48xlarge": {}}
    assert body["errors"] == {"p5.48xlarge": {"code": "sps-error", "message": "AWS call failed (UnauthorizedOperation)"}}
    assert body["scoreState"] == {"g6e.2xlarge": "scored", "p5.48xlarge": "none"}
    # Only the answer is cached, never the error.
    assert [k for k in t.puts if k.startswith("sps#")] == [sg.config_for(GROUPS, "g6e/2", "us-east-1").scores_key]


def test_groups_past_the_time_budget_get_a_timeout(env, monkeypatch):
    t = env
    release = threading.Event()
    monkeypatch.setattr(api, "HEATMAP_WORKERS", 4)
    monkeypatch.setattr(api, "HEATMAP_BUDGET_SECONDS", 0.3)

    def fake(key):
        if key == "p5/8":
            release.wait(5)
        return {"us-east-1": 5}
    monkeypatch.setattr(api, "_region_spot_scores", by_key(fake))
    started = time.monotonic()
    try:
        body = call({"types": "p5.48xlarge,g6e.2xlarge"})[1]
    finally:
        release.set()
    assert time.monotonic() - started < 2
    assert body["errors"] == {"p5.48xlarge": {"code": "sps-timeout", "message": api.SPS_TIMEOUT["message"]}}
    assert body["scores"] == {"g6e.2xlarge": {"us-east-1": 5}, "p5.48xlarge": {}}
    # A timeout says nothing about the configuration: its registry entry stays warm.
    entry = t.items["spscfg#" + sg.config_for(GROUPS, "p5/8", "us-east-1").fp]
    assert entry["lastResult"] == "accepted" and "blockedUntil" not in entry


def test_calls_run_on_the_bounded_workers(env, monkeypatch):
    monkeypatch.setattr(api, "HEATMAP_WORKERS", 4)
    lock, running, peak = threading.Lock(), [0], [0]

    def fake(key):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.02)
        with lock:
            running[0] -= 1
        return {"us-west-2": 1}
    monkeypatch.setattr(api, "_region_spot_scores", by_key(fake))
    types = [f"g6e.{n}xlarge" for n in range(1, 31)]
    status, body = call({"types": ",".join(types)})
    assert status == 200 and len(body["scores"]) == 30 and body["errors"] == {}
    assert 1 < peak[0] <= 4


def test_types_come_back_in_natural_order(env, monkeypatch):
    monkeypatch.setattr(api, "_region_spot_scores", lambda cfg: {"us-east-1": 1})
    body = call({"types": "p5.48xlarge,g6e.12xlarge,g6e.2xlarge"})[1]
    assert list(body["scoreGroups"]) == ["g6e.2xlarge", "g6e.12xlarge", "p5.48xlarge"]


def test_cache_items_are_valid_dynamodb_requests(monkeypatch):
    res = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    tbl = res.Table("c")
    s = Stubber(tbl.meta.client)
    monkeypatch.setattr(api, "offering_cache_table", lambda: tbl)
    monkeypatch.setattr(api, "SPOT_SCORE_CACHE_MINUTES", 30)
    key = spot_cache.cache_key("p5/8", REGIONS)
    s.add_response("put_item", {}, {"TableName": "c", "Item": {
        "cacheKey": key, "scores": json.dumps({"us-east-1": 7}), "fetchedAt": "t1", "expires": 100 + 40 * 60}})
    s.add_response("get_item", {"Item": {"cacheKey": {"S": key}, "scores": {"S": json.dumps({"us-east-1": 7})},
                                         "fetchedAt": {"S": "t1"}, "expires": {"N": "2500"}}},
                   {"TableName": "c", "Key": {"cacheKey": key}})
    s.activate()
    api._write_sps("p5/8", REGIONS, {"us-east-1": 7}, "t1", 100)
    monkeypatch.setattr(api, "_sps_mem", {})
    assert api._read_sps_entries({"x": ("p5/8", REGIONS)}, 100) == {"x": {"scores": {"us-east-1": 7}, "fetchedAt": "t1"}}
    s.assert_no_pending_responses()
