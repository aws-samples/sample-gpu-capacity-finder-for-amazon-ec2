"""Latency work that must not change answers: parallel quota lists, parallel cache calls, the
Spot-score memory layer, client creation at init, and the request log line.
Fake clients and tables only; nothing reaches AWS."""
import json
import logging
import os
import threading
import time

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import cache_fakes  # noqa: E402
import families  # noqa: E402
import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from cache_fakes import by_chunk  # noqa: E402

NOW = 2_000_000_000
# conftest swaps offering_cache_table for an offline table in every test; the init test needs the real one.
REAL_OFFERING_CACHE_TABLE = api.offering_cache_table
REGIONS = ["us-east-1", "us-west-2"]
# The heatmap fixtures sell every type in us-east-1 only; each type's Spot score Region list is
# still the full fixed Region group (the configured Regions), never narrowed to that Region.
SOLD_IN = list(REGIONS)


def call(path, q=None, method="GET", claims=None):
    ev = {"requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": {"token_use": "id", **(claims or {})}}}},
          "rawPath": path, "queryStringParameters": q}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def quota(code, name, value, metric=None):
    item = {"QuotaCode": code, "QuotaName": name, "Value": value, "Unit": "None", "Adjustable": True}
    if metric:
        item["UsageMetric"] = metric
    return item


class Gate:
    """Counts callers that are inside at the same time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.inside = 0
        self.peak = 0

    def __enter__(self):
        with self.lock:
            self.inside += 1
            self.peak = max(self.peak, self.inside)

    def __exit__(self, *_):
        with self.lock:
            self.inside -= 1


# ------------------------------------------------------------ quota lists
class FakePaginator:
    def __init__(self, pages, delay, gate, error=None):
        self.pages, self.delay, self.gate, self.error = pages, delay, gate, error
        self.kwargs = None

    def paginate(self, **kwargs):
        self.kwargs = kwargs
        for page in self.pages:
            with self.gate:
                time.sleep(self.delay)
            yield {"Quotas": page}
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": "x"}}, "ListServiceQuotas")


class FakeQuotas:
    def __init__(self, defaults, applied, delay=0.0, errors=None):
        self.gate = Gate()
        errors = errors or {}
        self.paginators = {
            "list_aws_default_service_quotas": FakePaginator(defaults, delay, self.gate, errors.get("defaults")),
            "list_service_quotas": FakePaginator(applied, delay, self.gate, errors.get("applied")),
        }

    def get_paginator(self, op):
        return self.paginators[op]


METRIC = {"MetricNamespace": "AWS/Usage", "MetricName": "ResourceCount", "MetricDimensions": {"Class": "P/OnDemand"}}
DEFAULTS = [[quota("L-P", "Running On-Demand P instances", 0.0, METRIC),
             quota("L-X", "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances", 5.0)],
            [quota("L-S", "All P5 Spot Instance Requests", 0.0),
             quota("L-CB", "Concurrent P5 Capacity Blocks per account", 2.0)]]
APPLIED = [[quota("L-P", "Running On-Demand P instances", 192.0)],
           [quota("L-S", "All P5 Spot Instance Requests", 64.0, METRIC)]]


@pytest.fixture
def quotas(monkeypatch):
    monkeypatch.setattr(api, "QUOTA_LIST_WORKERS", 2)
    monkeypatch.setattr(api, "_usage", lambda region, rows: {i: 7.0 for i, r in enumerate(rows) if r["_metric"]})

    def make(**kwargs):
        sq = FakeQuotas(**kwargs)
        monkeypatch.setattr(api, "client", lambda service, region: sq)
        return sq
    return make


def test_quota_lists_run_in_parallel_and_merge_as_before(quotas):
    sq = quotas(defaults=DEFAULTS, applied=APPLIED, delay=0.05)
    started = time.monotonic()
    result = api._fetch_quota_rows("us-east-1")
    elapsed = time.monotonic() - started
    # Two pages per list at 50 ms each: one after the other takes 200 ms, side by side about 100 ms.
    assert sq.gate.peak == 2 and elapsed < 0.18
    rows = {r["code"]: r for r in result["quotas"]}
    # Applied values overwrite defaults; a row without UsageMetric keeps the default's metric.
    assert (rows["L-P"]["value"], rows["L-P"]["applied"], rows["L-P"]["used"]) == (192.0, True, 7.0)
    assert (rows["L-S"]["value"], rows["L-S"]["applied"], rows["L-S"]["used"]) == (64.0, True, 7.0)
    assert (rows["L-CB"]["applied"], rows["L-CB"]["used"]) == (False, None)
    assert "L-X" not in rows
    assert [r["category"] for r in result["quotas"]] == ["on-demand", "spot", "capacity-block"]
    for paginator in sq.paginators.values():
        assert paginator.kwargs == {"ServiceCode": "ec2", "PaginationConfig": {"PageSize": 100}}


def test_quota_merge_is_the_same_with_one_worker(quotas, monkeypatch):
    quotas(defaults=DEFAULTS, applied=APPLIED)
    parallel = api._fetch_quota_rows("us-east-1")
    monkeypatch.setattr(api, "QUOTA_LIST_WORKERS", 1)
    sequential = api._fetch_quota_rows("us-east-1")
    strip = lambda r: {k: v for k, v in r.items() if k != "fetchedAt"}
    assert strip(parallel) == strip(sequential)


@pytest.mark.parametrize("errors, expected", [
    ({"defaults": "AccessDeniedException", "applied": "TooManyRequestsException"}, "AccessDeniedException"),
    ({"applied": "TooManyRequestsException"}, "TooManyRequestsException"),
])
def test_quota_list_failure_raises_the_defaults_error_first(quotas, errors, expected):
    quotas(defaults=DEFAULTS, applied=APPLIED, errors=errors)
    with pytest.raises(ClientError) as info:
        api._fetch_quota_rows("us-east-1")
    assert info.value.response["Error"]["Code"] == expected


def test_quota_route_maps_a_list_failure_to_502_as_before(quotas):
    quotas(defaults=DEFAULTS, applied=APPLIED, errors={"applied": "TooManyRequestsException"})
    status, body = call("/api/quotas", {"region": "us-east-1"})
    assert status == 502 and body["code"] == "aws-error" and body["params"] == {"awsCode": "TooManyRequestsException"}


def test_region_fan_out_runs_on_bounded_region_workers(monkeypatch):
    regions = [f"r{i}" for i in range(17)]
    monkeypatch.setattr(api, "REGIONS", regions)
    gate = Gate()

    def fetch(region):
        with gate:
            time.sleep(0.03)
        return {"ok": True}
    assert [r["region"] for r in api.each_region(fetch)] == regions
    assert gate.peak == api.REGION_WORKERS == 8


# ------------------------------------------------------------ cache calls
class _Meta:
    def __init__(self, client):
        self.client = client


class SlowTable:
    """A cache table whose calls take `delay` seconds; records the peak of concurrent calls. Its
    `meta.client` answers BatchGetItem (recorded in `batches`); `batch_error` makes it fail."""
    name = "cache"

    def __init__(self, delay=0.03, batch_error=None):
        self.items, self.gets, self.puts, self.batches = {}, [], [], []
        self.delay, self.gate = delay, Gate()
        self.lock = threading.Lock()
        self.batch_error = batch_error
        self.meta = _Meta(self)

    def batch_get_item(self, RequestItems):
        with self.gate:
            time.sleep(self.delay)
        request = RequestItems[self.name]
        keys = [k["cacheKey"] for k in request["Keys"]]
        assert len(keys) <= 100
        with self.lock:
            self.batches.append(keys)
        if self.batch_error:
            raise ClientError({"Error": {"Code": self.batch_error}}, "BatchGetItem")
        return {"Responses": {self.name: [self.items[k] for k in keys if k in self.items]}, "UnprocessedKeys": {}}

    def get_item(self, Key, **_):
        with self.gate:
            time.sleep(self.delay)
        with self.lock:
            # The Region set item and the Spot score registry items are counted apart.
            if Key["cacheKey"] != "#sps-regions" and not cache_fakes.is_registry(Key["cacheKey"]):
                self.gets.append(Key["cacheKey"])
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        with self.gate:
            time.sleep(self.delay)
        with self.lock:
            cache_fakes.conditional_put(self.items, Item, ConditionExpression, ExpressionAttributeValues)
            if Item["cacheKey"] != "#sps-regions" and not cache_fakes.is_registry(Item["cacheKey"]):
                self.puts.append(Item["cacheKey"])


TYPES = [f"g6e.{n}xlarge" for n in range(1, 17)]
GPUS = {t: n for n, t in enumerate(TYPES, 1)}
# One score group per type here (g6e/1 .. g6e/16), each in both Regions (one configuration each).
GROUPS = {f"g6e/{n}": {"types": [t], "chunks": [list(REGIONS)], "added": {}} for t, n in GPUS.items()}


def sps_item(key, scores, expires=NOW + 600):
    return {"cacheKey": api._sps_cache_key(key, SOLD_IN), "scores": json.dumps(scores), "fetchedAt": "t0", "expires": expires}


@pytest.fixture
def heat(monkeypatch):
    import score_groups as sg
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": list(REGIONS), "fetchedAt": None})
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", ["g6e.*"])
    monkeypatch.setattr(api, "_epoch", lambda: NOW)
    t = SlowTable()
    cache_fakes.seeded(t.items)
    t.items[sg.STATE_KEY] = {"cacheKey": sg.STATE_KEY, "version": 1, "families": '["g6e.*"]',
                             "groups": json.dumps(GROUPS), "gpus": json.dumps(GPUS), "offered": "{}",
                             "dailyAt": NOW - 60, "updatedAt": NOW - 60}
    for _, cfg in sg.configs(GROUPS):
        cache_fakes.register(t.items, cfg, NOW - 600)
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    return t


def score_keys():
    return [api._sps_cache_key(f"g6e/{n}", SOLD_IN) for n in range(1, 17)]


def test_the_scores_are_read_in_one_batch(heat, monkeypatch):
    for n in range(1, 17):
        heat.items[api._sps_cache_key(f"g6e/{n}", SOLD_IN)] = sps_item(f"g6e/{n}", {"us-east-1": 5})
    monkeypatch.setattr(api, "_region_spot_scores", lambda cfg: pytest.fail("cached groups call no EC2"))
    started = time.monotonic()
    status, body = call("/api/spot-heatmap", {"types": ",".join(TYPES)})
    elapsed = time.monotonic() - started
    assert status == 200 and all(body["fromCache"].values()) and len(body["scores"]) == 16
    # The 16 answers in one BatchGetItem; the state item is the one GetItem (the registry items
    # and the governor item are counted apart).
    assert heat.gets == ["#sps-groups"] and elapsed < 0.6
    assert sorted(heat.batches[0]) == sorted(score_keys())


def test_a_failed_batch_falls_back_to_parallel_get_item(heat, monkeypatch):
    heat.batch_error = "InternalServerError"
    for n in range(1, 17):
        heat.items[api._sps_cache_key(f"g6e/{n}", SOLD_IN)] = sps_item(f"g6e/{n}", {"us-east-1": 5})
    monkeypatch.setattr(api, "_region_spot_scores", lambda cfg: pytest.fail("cached groups call no EC2"))
    status, body = call("/api/spot-heatmap", {"types": ",".join(TYPES)})
    assert status == 200 and all(body["fromCache"].values()) and len(body["scores"]) == 16
    # The same answer from GetItem reads on the fallback pool of 8.
    assert set(score_keys()) <= set(heat.gets) and heat.gate.peak == 8


def test_refresh_locks_and_writes_run_in_parallel_and_keep_their_answers(heat, monkeypatch):
    monkeypatch.setattr(api, "_region_spot_scores", lambda cfg: {"us-east-1": int(cfg.itype.split("/")[1])})
    # One group's window still runs: it is served from its item and says how long to wait.
    blocked = "g6e/4"
    heat.items[api._refresh_lock_key(api._sps_lock_item(blocked))] = {"cacheKey": "x", "lockedUntil": NOW + 20}
    heat.items[api._sps_cache_key(blocked, SOLD_IN)] = sps_item(blocked, {"us-east-1": 9})
    status, body = call("/api/spot-heatmap", {"types": ",".join(TYPES), "refresh": "true"})
    assert status == 200
    assert body["fromCache"]["g6e.4xlarge"] is True and body["retryAfterSeconds"] == {"g6e.4xlarge": 20}
    assert body["scores"]["g6e.4xlarge"] == {"us-east-1": 9}
    assert body["scores"]["g6e.12xlarge"] == {"us-east-1": 12} and body["refreshed"]["g6e.12xlarge"] is True
    written = [k for k in heat.puts if k.startswith("sps#")]
    assert sorted(written) == sorted(k for k in score_keys() if k != api._sps_cache_key(blocked, SOLD_IN))
    assert heat.gate.peak == 8


def test_cache_map_keeps_order_and_raises_in_item_order():
    assert api._cache_map(lambda x: x * 2, range(20)) == [x * 2 for x in range(20)]

    def fail(x):
        if x in (3, 7):
            raise ValueError(x)
        return x
    with pytest.raises(ValueError) as info:
        api._cache_map(fail, range(10))
    assert info.value.args == (3,)


def test_offering_cache_reads_are_one_batch(monkeypatch):
    regions = [f"r{i}" for i in range(8)]
    monkeypatch.setattr(api, "_epoch", lambda: NOW)
    t = SlowTable()
    for r in regions:
        t.items[f"offerings#{r}"] = {"cacheKey": f"offerings#{r}", "data": "{}", "zones": "{}", "fetchedAt": r,
                                     "patterns": families.cache_tag(), "expires": NOW + 60}
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    found = api._offering_entries(regions)
    assert list(found) == regions and all(found[r]["fetchedAt"] == r and found[r]["fromCache"] for r in regions)
    assert t.batches == [[f"offerings#{r}" for r in regions]] and t.gets == []


# ------------------------------------------------------- spot-score memory layer
@pytest.fixture
def mem(monkeypatch):
    monkeypatch.setattr(api, "REGIONS", list(REGIONS))
    clock = {"mono": 1000.0}
    monkeypatch.setattr(api, "_now", lambda: clock["mono"])
    t = SlowTable(delay=0)
    cache_fakes.seeded(t.items)
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    return t, clock


def read(key, now=NOW):
    return api._read_sps_entries({"x": (key, SOLD_IN)}, now).get("x")


def test_spot_score_items_stay_in_memory_for_a_minute(mem):
    t, clock = mem
    t.items[api._sps_cache_key("p5/8", SOLD_IN)] = sps_item("p5/8", {"us-east-1": 4})
    first = read("p5/8")
    clock["mono"] += 59
    assert read("p5/8") == first == {"scores": {"us-east-1": 4}, "fetchedAt": "t0"}
    assert len(t.gets) == 1
    clock["mono"] += 2
    read("p5/8")
    assert len(t.gets) == 2


def test_memory_copy_never_outlives_the_item(mem):
    t, _ = mem
    t.items[api._sps_cache_key("p5/8", SOLD_IN)] = sps_item("p5/8", {"us-east-1": 4}, expires=NOW + 10)
    assert read("p5/8") is not None
    assert read("p5/8", NOW + 10) is None
    assert len(t.gets) == 2


def test_a_write_updates_the_memory_copy(mem):
    t, _ = mem
    api._write_sps("p5/8", SOLD_IN, {"us-east-1": 3}, "t1", NOW)
    assert read("p5/8") == {"scores": {"us-east-1": 3}, "fetchedAt": "t1"}
    assert t.gets == []


def test_a_forced_refresh_drops_the_memory_copy(mem):
    t, _ = mem
    t.items[api._sps_cache_key("p5/8", SOLD_IN)] = sps_item("p5/8", {"us-east-1": 4})
    read("p5/8")
    api._forget_sps("p5/8")
    assert api._sps_cache_key("p5/8", SOLD_IN) not in api._sps_mem
    before = len(t.gets)
    assert read("p5/8")["scores"] == {"us-east-1": 4} and len(t.gets) == before + 1


def test_the_stored_groups_stay_in_memory_for_a_minute(mem):
    t, clock = mem
    import score_groups as sg
    t.items[sg.STATE_KEY] = {"cacheKey": sg.STATE_KEY, "version": 1, "groups": "{}"}
    api._groups_state()
    api._groups_state()
    assert t.gets.count(sg.STATE_KEY) == 1
    clock["mono"] += 61
    api._groups_state()
    assert t.gets.count(sg.STATE_KEY) == 2


# ------------------------------------------------------------ heatmap workers
def test_repeats_run_eight_ec2_calls_at_once_within_the_budget(heat, monkeypatch):
    gate = Gate()

    def scores(cfg):
        with gate:
            time.sleep(0.05)
        return {"us-east-1": 1}
    monkeypatch.setattr(api, "_region_spot_scores", scores)
    body = call("/api/spot-heatmap", {"types": ",".join(TYPES)})[1]
    assert len(body["scores"]) == 16 and not body["errors"]
    assert api.HEATMAP_WORKERS == 8 and gate.peak == 8 and api.HEATMAP_BUDGET_SECONDS == 22


# ------------------------------------------------------------ init phase
def test_prewarm_creates_handles_for_the_functions_own_region_and_survives_failures(monkeypatch):
    made = []

    class FakeBoto:
        @staticmethod
        def client(service, region_name=None, **_kwargs):
            if (service, region_name) == ("cloudwatch", "us-west-2"):
                raise RuntimeError("no endpoint")
            made.append((service, region_name))
            return object()

        @staticmethod
        def resource(service):
            class Res:
                def Table(self, name):
                    made.append(("table", name))
                    return name
            return Res()
    monkeypatch.setattr(api, "boto3", FakeBoto)
    monkeypatch.setattr(api, "HOME_REGION", "us-west-2")
    monkeypatch.setattr(api, "_clients", {})
    monkeypatch.setattr(api, "_table", None)
    monkeypatch.setattr(api, "_offering_cache_table", None)
    monkeypatch.setattr(api, "_subscriptions_table", None)
    monkeypatch.setattr(api, "offering_cache_table", REAL_OFFERING_CACHE_TABLE)
    monkeypatch.setattr(api, "TABLE_NAME", "probes")
    monkeypatch.setattr(api, "OFFERING_CACHE_TABLE_NAME", "cache")
    monkeypatch.setattr(api, "SUBSCRIPTIONS_TABLE_NAME", "")
    monkeypatch.setattr(api, "TOPIC_ARN", "")
    api._prewarm()
    assert ("table", "probes") in made and ("table", "cache") in made
    assert ("ec2", "us-west-2") in made and ("service-quotas", "us-west-2") in made
    assert ("pricing", "us-east-1") in made
    assert not any(s in ("sns", "ssm") for s, _ in made)
    assert ("cloudwatch", "us-west-2") not in api._clients and ("ec2", "us-west-2") in api._clients


# ------------------------------------------------------------ request log line
def request_lines(caplog):
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    return [line for line in lines if line.get("msg") == "request"]


def test_one_request_line_with_route_status_duration_and_cache_counts(heat, caplog, monkeypatch):
    monkeypatch.setattr(api, "_region_spot_scores", lambda cfg: {"us-east-1": 2})
    heat.items[api._sps_cache_key("g6e/1", SOLD_IN)] = sps_item("g6e/1", {"us-east-1": 5})
    with caplog.at_level(logging.INFO):
        call("/api/spot-heatmap", {"types": "g6e.1xlarge"})
    [line] = request_lines(caplog)
    assert line["route"] == "/api/spot-heatmap" and line["method"] == "GET" and line["status"] == 200
    assert line["cacheHits"] == 1 and line["cacheMisses"] == 0 and isinstance(line["durationMs"], int)
    assert "errorCode" not in line and "region" not in line
    # Again within the minute: a hit from the container's copy, no table read.
    reads = len(heat.gets)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        call("/api/spot-heatmap", {"types": "g6e.1xlarge,g6e.2xlarge"})
    [line] = request_lines(caplog)
    # g6e/2 has no item: one read and a miss (then one repeat of its granted configuration).
    assert (line["cacheHits"], line["cacheMisses"]) == (1, 1) and len(heat.gets) == reads + 1


def test_request_line_carries_the_error_code_and_only_safe_values(caplog, monkeypatch):
    monkeypatch.setattr(api, "REGIONS", list(REGIONS))
    claims = {"sub": "sub-alice", "email": "alice@example.com"}
    with caplog.at_level(logging.INFO):
        call("/api/spot-scores", {"region": "us-east-1", "instanceType": "m5.large", "token": "secret-value"}, claims=claims)
        call("/api/../etc/passwd", {"region": "xx-evil-1"}, method="PATCH")
    first, second = request_lines(caplog)
    assert first["status"] == 400 and first["errorCode"] and first["region"] == "us-east-1"
    assert first["instanceType"] == "m5.large"
    assert (second["route"], second["method"], second["status"], second["errorCode"]) == ("-", "-", 404, "not-found")
    assert "region" not in second
    text = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("alice@example.com", "sub-alice", "secret-value", "passwd", "xx-evil-1"):
        assert secret not in text


def test_request_line_names_the_aws_code_of_a_502(caplog, monkeypatch):
    def fail(q, claims):
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "GetItem")
    monkeypatch.setitem(api.ROUTES, ("GET", "/api/probes"), fail)
    with caplog.at_level(logging.INFO):
        status, _ = call("/api/probes")
    assert status == 502 and request_lines(caplog)[0]["errorCode"] == "aws-error:ThrottlingException"

