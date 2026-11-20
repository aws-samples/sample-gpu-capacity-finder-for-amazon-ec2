"""Batched cache reads (cache_reads.get_items): BatchGetItem chunks of 100 keys, a bounded retry of
UnprocessedKeys, the GetItem fallback, strongly consistent lock reads, and the callers that use it.
Fake tables and a botocore Stubber only; nothing reaches AWS."""
import os

os.environ.update(SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import json  # noqa: E402

import api  # noqa: E402
import boto3  # noqa: E402
import cache_fakes  # noqa: E402
import cache_reads  # noqa: E402
import pytest  # noqa: E402
import spot_cache  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

NOW = 2_000_000_000


class _Meta:
    def __init__(self, client):
        self.client = client


class BatchTable:
    """Items by cacheKey; `meta.client.batch_get_item` records each request. `unprocessed` keys come
    back unprocessed that many times in a row; `fail` makes every batch request fail."""
    name = "cache"

    def __init__(self, unprocessed=None, fail=None, get_fail=()):
        self.items, self.calls, self.gets = {}, [], []
        self.unprocessed = dict(unprocessed or {})
        self.fail, self.get_fail = fail, set(get_fail)
        self.meta = _Meta(self)

    def batch_get_item(self, RequestItems):
        assert list(RequestItems) == [self.name]
        req = RequestItems[self.name]
        keys = [k["cacheKey"] for k in req["Keys"]]
        self.calls.append((keys, req.get("ConsistentRead", False)))
        if self.fail:
            raise ClientError({"Error": {"Code": self.fail}}, "BatchGetItem")
        left = [k for k in keys if self.unprocessed.get(k, 0) > 0]
        for k in left:
            self.unprocessed[k] -= 1
        done = [self.items[k] for k in keys if k in self.items and k not in left]
        out = {"Responses": {self.name: done}, "UnprocessedKeys": {}}
        if left:
            out["UnprocessedKeys"] = {self.name: {"Keys": [{"cacheKey": k} for k in left]}}
        return out

    def get_item(self, Key, ConsistentRead=False):
        self.gets.append((Key["cacheKey"], ConsistentRead))
        if Key["cacheKey"] in self.get_fail:
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        cache_fakes.conditional_put(self.items, Item, ConditionExpression, ExpressionAttributeValues)


def fill(t, keys):
    for k in keys:
        t.items[k] = {"cacheKey": k, "v": k}


def no_sleep(_):
    pass


def test_keys_are_read_in_chunks_of_100():
    t = BatchTable()
    keys = [f"k{i:03d}" for i in range(250)]
    fill(t, keys[:-1])
    found = cache_reads.get_items(t, keys + keys[:5], sleep=no_sleep)
    assert [len(c[0]) for c in t.calls] == [100, 100, 50]
    assert set(found) == set(keys[:-1]) and found["k000"] == {"cacheKey": "k000", "v": "k000"}
    assert t.gets == [] and not any(c[1] for c in t.calls)


def test_one_key_is_one_get_item_and_no_keys_no_call():
    t = BatchTable()
    fill(t, ["a"])
    assert cache_reads.get_items(t, ["a"]) == {"a": {"cacheKey": "a", "v": "a"}}
    assert cache_reads.get_items(t, []) == {}
    assert t.calls == [] and t.gets == [("a", False)]


def test_unprocessed_keys_are_retried_a_bounded_number_of_times():
    sleeps = []
    t = BatchTable(unprocessed={"b": 1, "c": 99})
    fill(t, ["a", "b", "c"])
    found = cache_reads.get_items(t, ["a", "b", "c"], sleep=sleeps.append)
    # 3 requests in all; c stays unprocessed and is read with GetItem instead.
    assert [c[0] for c in t.calls] == [["a", "b", "c"], ["b", "c"], ["c"]]
    assert sleeps == [cache_reads.RETRY_DELAY_SECONDS, cache_reads.RETRY_DELAY_SECONDS * 2]
    assert t.gets == [("c", False)] and set(found) == {"a", "b", "c"}


def test_a_failed_batch_falls_back_to_get_item_and_a_failed_get_is_a_miss():
    t = BatchTable(fail="ProvisionedThroughputExceededException", get_fail={"b"})
    fill(t, ["a", "b", "c"])
    found = cache_reads.get_items(t, ["a", "b", "c"], sleep=no_sleep)
    assert len(t.calls) == 1 and sorted(g[0] for g in t.gets) == ["a", "b", "c"]
    assert set(found) == {"a", "c"}


def test_consistent_reads_are_asked_for_on_every_path():
    t = BatchTable(unprocessed={"b": 99})
    fill(t, ["a", "b"])
    cache_reads.get_items(t, ["a", "b"], consistent=True, sleep=no_sleep)
    assert all(c[1] for c in t.calls) and t.gets == [("b", True)]


def test_a_table_without_a_batch_client_reads_item_by_item():
    class Plain:
        def __init__(self):
            self.gets = []

        def get_item(self, Key, **_):
            self.gets.append(Key["cacheKey"])
            return {"Item": {"cacheKey": Key["cacheKey"]}}
    t = Plain()
    assert set(cache_reads.get_items(t, ["a", "b"])) == {"a", "b"} and sorted(t.gets) == ["a", "b"]


def test_the_real_request_shape(monkeypatch):
    res = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    tbl = res.Table("c")
    s = Stubber(tbl.meta.client)
    s.add_response("batch_get_item", {
        "Responses": {"c": [{"cacheKey": {"S": "a"}, "expires": {"N": "5"}}]},
        "UnprocessedKeys": {"c": {"Keys": [{"cacheKey": {"S": "b"}}], "ConsistentRead": True}}},
        {"RequestItems": {"c": {"Keys": [{"cacheKey": "a"}, {"cacheKey": "b"}], "ConsistentRead": True}}})
    s.add_response("batch_get_item", {"Responses": {"c": [{"cacheKey": {"S": "b"}}]}, "UnprocessedKeys": {}},
                   {"RequestItems": {"c": {"Keys": [{"cacheKey": "b"}], "ConsistentRead": True}}})
    s.activate()
    found = cache_reads.get_items(tbl, ["a", "b"], consistent=True, sleep=no_sleep)
    assert found == {"a": {"cacheKey": "a", "expires": 5}, "b": {"cacheKey": "b"}}
    s.assert_no_pending_responses()


# ------------------------------------------------------------- callers
def test_blocked_refresh_locks_are_read_in_one_consistent_batch(monkeypatch):
    t = BatchTable()
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    for item, until in (("sps#a", NOW + 20), ("sps#b", NOW + 45)):
        t.items[f"#lock#{item}"] = {"cacheKey": f"#lock#{item}", "lockedUntil": until}
    assert api._claim_refreshes(NOW, ["sps#a", "sps#b", "sps#c"]) == [20, 45, 0]
    assert t.calls == [(["#lock#sps#a", "#lock#sps#b"], True)] and t.gets == []
    # The same answers as one _claim_refresh per item.
    assert [api._claim_refresh(NOW, i) for i in ("sps#a", "sps#b")] == [20, 45]


def test_the_spot_score_tab_reads_the_cache_in_batches(monkeypatch):
    """30 score groups over 30 Regions on a new container: the 90 configuration answers in one
    batch, their 90 registry entries in one more, then the history, offering and price items; the
    stored groups and the governor item are single reads. No EC2 call."""
    import score_groups as sg
    regions = [f"xx-r{i:02d}-1" for i in range(30)]
    gpus = {f"g6e.{n}xlarge": n for n in range(1, 31)}
    groups = {f"g6e/{n}": {"types": [f"g6e.{n}xlarge"], "chunks": [regions[:10], regions[10:20], regions[20:]],
                           "added": {}} for n in range(1, 31)}
    t = BatchTable()
    cache_fakes.seeded(t.items)
    t.items[sg.STATE_KEY] = {"cacheKey": sg.STATE_KEY, "version": 1, "families": '["g6e.*"]',
                             "groups": json.dumps(groups), "gpus": json.dumps(gpus), "offered": "{}",
                             "dailyAt": NOW - 60, "updatedAt": NOW - 60}
    for _, cfg in sg.configs(groups):
        cache_fakes.register(t.items, cfg, NOW - 60)
        spot_cache.write(t, cfg.itype, list(cfg.regions), {r: 5 for r in cfg.regions}, "t0", NOW, 40)
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    monkeypatch.setattr(api, "_epoch", lambda: NOW)
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", ["g6e.*"])
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": list(regions), "fetchedAt": None})
    monkeypatch.setattr(api, "client", lambda *a: pytest.fail("no AWS call expected"))
    t.calls.clear()
    t.gets.clear()
    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}},
                     "rawPath": "/api/spot-scores", "queryStringParameters": None}, None)
    assert r["statusCode"] == 200 and len(json.loads(r["body"])["groups"]) == 30
    assert [len(c[0]) for c in t.calls] == [90, 90, 30, 30, 30]
    assert sorted(k for k, _ in t.gets) == ["#sps-budget", sg.STATE_KEY]


@pytest.mark.parametrize("keys", [["only"], ["a", "b"]])
def test_get_one_and_get_items_never_raise_on_client_errors(keys):
    t = BatchTable(fail="InternalServerError", get_fail=set(keys))
    assert cache_reads.get_items(t, keys, sleep=no_sleep) == {}
