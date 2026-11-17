"""One unreachable Region must not hold the whole Region fan-out, and the Regions that gave no
answer are shared through the cache table, so one container's timeout spares the others."""
import time

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

import api


@pytest.fixture(autouse=True)
def _reset():
    api._region_down.clear()
    yield
    api._region_down.clear()


class MarkerTable:
    """The cache table reduced to the no-answer markers: every other key is a miss."""

    def __init__(self, items=None, fail=False):
        self.items = dict(items or {})
        self.puts = []
        self.fail = fail

    def get_item(self, Key, **_):
        if self.fail:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, **_):
        if self.fail:
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "PutItem")
        self.items[Item["cacheKey"]] = Item
        self.puts.append(Item["cacheKey"])


@pytest.fixture
def markers(monkeypatch):
    t = MarkerTable()
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    return t


def test_ec2_clients_use_short_timeouts():
    cfg = api._client_options("ec2")["config"]
    assert cfg.connect_timeout == api.EC2_CONNECT_TIMEOUT == 3
    assert cfg.read_timeout == api.EC2_READ_TIMEOUT


@pytest.mark.parametrize("service", ["service-quotas", "cloudwatch", "pricing", "ssm", "sns"])
def test_other_clients_use_bounded_timeouts(service):
    cfg = api._client_options(service)["config"]
    assert cfg.connect_timeout == 3
    assert cfg.read_timeout == 10
    assert cfg.retries == {"mode": "standard", "total_max_attempts": 2}


def test_the_lambda_invoke_client_tries_once_with_a_short_read_timeout():
    cfg = api._client_options("lambda")["config"]
    assert cfg.connect_timeout == 3
    assert cfg.read_timeout == 5
    assert cfg.retries == {"mode": "standard", "total_max_attempts": 1}


def test_a_cloudwatch_timeout_marks_usage_unavailable_instead_of_failing(monkeypatch):
    from botocore.exceptions import ReadTimeoutError

    class Paginator:
        def __init__(self, op):
            self.op = op

        def paginate(self, **_):
            return [{"Quotas": [{"QuotaCode": "L-1", "QuotaName": "Running On-Demand G and VT instances",
                                 "Value": 64.0, "UsageMetric": {"MetricName": "ResourceCount"}}]}]

    class SQ:
        def get_paginator(self, op):
            return Paginator(op)

    def slow_usage(region, rows):
        raise ReadTimeoutError(endpoint_url="https://monitoring.example")

    monkeypatch.setattr(api, "client", lambda service, region: SQ())
    monkeypatch.setattr(api, "_usage", slow_usage)
    out = api._fetch_quota_rows("us-east-1")
    assert out["usageError"]
    assert out["usageErrorCode"] == "usage-unavailable"
    assert out["usageErrorParams"] == {"awsCode": "ReadTimeoutError"}
    assert out["quotas"][0]["used"] is None


def test_a_hanging_region_does_not_block_the_others():
    def fn(region):
        if region == "me-south-1":
            time.sleep(5)
        return {"families": {}}
    started = time.time()
    out = api.each_region(fn, ["us-east-1", "me-south-1", "us-west-2"], budget=0.3)
    assert time.time() - started < 2
    by = {e["region"]: e for e in out}
    assert [e["region"] for e in out] == ["us-east-1", "me-south-1", "us-west-2"]
    assert "error" not in by["us-east-1"] and "error" not in by["us-west-2"]
    assert by["me-south-1"]["errorCode"] == "no-answer"


def test_an_unreachable_region_is_skipped_for_a_while():
    calls = []

    def fn(region):
        calls.append(region)
        if region == "me-south-1":
            raise ConnectTimeoutError(endpoint_url="https://ec2.me-south-1.amazonaws.com/")
        return {}
    first = api.each_region(fn, ["us-east-1", "me-south-1"])
    assert {e["region"]: e.get("errorCode") for e in first} == {"us-east-1": None, "me-south-1": "no-answer"}
    api.each_region(fn, ["us-east-1", "me-south-1"])
    assert calls.count("me-south-1") == 1      # the second request did not try it again
    assert calls.count("us-east-1") == 2


def test_the_skip_expires():
    api._region_down["me-south-1"] = time.time() - 1
    seen = []
    api.each_region(lambda r: seen.append(r) or {}, ["me-south-1"])
    assert seen == ["me-south-1"]


def test_ordinary_errors_do_not_mark_a_region_down():
    def fn(region):
        raise RuntimeError("boom")
    out = api.each_region(fn, ["us-east-1"])
    assert out[0]["errorCode"] == "no-answer"
    assert "us-east-1" not in api._region_down


# ------------------------------------- the shared marker in the cache table
def test_a_region_that_gave_no_answer_is_written_to_the_shared_marker(markers):
    def fn(region):
        if region == "me-south-1":
            raise ConnectTimeoutError(endpoint_url="https://ec2.me-south-1.amazonaws.com/")
        return {}
    started = time.time()
    api.each_region(fn, ["us-east-1", "me-south-1"])
    assert markers.puts == ["#down#me-south-1"]
    marker = markers.items["#down#me-south-1"]
    # The marker expires with its own TTL, so it clears itself.
    assert marker["expires"] == marker["downUntil"]
    assert int(started) + api.REGION_DOWN_SECONDS <= marker["downUntil"] <= time.time() + api.REGION_DOWN_SECONDS


def test_another_container_skips_the_region_from_the_shared_marker(markers):
    """The marker of one container spares the others the wait: nothing is in this container's memory."""
    markers.items["#down#me-south-1"] = {"cacheKey": "#down#me-south-1", "downUntil": int(time.time()) + 60,
                                         "expires": int(time.time()) + 60}
    seen = []
    out = api.each_region(lambda r: seen.append(r) or {}, ["us-east-1", "me-south-1"])
    assert seen == ["us-east-1"]
    assert {e["region"]: e.get("errorCode") for e in out} == {"us-east-1": None, "me-south-1": "no-answer"}
    # It is kept here too, so the next request in this container needs no read.
    assert api._region_down["me-south-1"] == markers.items["#down#me-south-1"]["downUntil"]


def test_a_marker_that_has_run_out_or_cannot_be_read_skips_nothing(markers):
    markers.items["#down#me-south-1"] = {"cacheKey": "#down#me-south-1", "downUntil": int(time.time()) - 1}
    seen = []
    api.each_region(lambda r: seen.append(r) or {}, ["me-south-1"])
    assert seen == ["me-south-1"] and "me-south-1" not in api._region_down
    # A read that fails leaves the Region unknown, and an unknown Region is always tried.
    markers.fail = True
    api.each_region(lambda r: seen.append(r) or {}, ["me-south-1"])
    assert seen == ["me-south-1", "me-south-1"]


def test_throttling_and_other_aws_errors_write_no_marker(markers):
    """The endpoint answered, so the Region is not down; a marker would skip a Region that works."""
    def fn(region):
        raise ClientError({"Error": {"Code": "RequestLimitExceeded"}}, "DescribeInstanceTypeOfferings")
    out = api.each_region(fn, ["us-east-1"])
    assert out[0]["errorCode"] == "aws-error" and out[0]["errorParams"] == {"awsCode": "RequestLimitExceeded"}
    assert markers.puts == [] and api._region_down == {}


def test_a_failed_marker_write_still_skips_the_region_in_this_container(markers):
    markers.fail = True

    def fn(region):
        raise ConnectTimeoutError(endpoint_url="https://ec2.me-south-1.amazonaws.com/")
    api.each_region(fn, ["me-south-1"])
    assert "me-south-1" in api._region_down and markers.puts == []


def test_a_short_refresh_budget_marks_no_region(markers):
    """A Region that misses the small budget of a refresh of data the request already has is not
    marked: only a real no-answer is (mark_pending_down)."""
    out = api.each_region(lambda r: time.sleep(5) or {}, ["me-south-1"], budget=0.05, mark_pending_down=False)
    assert out[0]["errorCode"] == "no-answer"
    assert api._region_down == {} and markers.puts == []
