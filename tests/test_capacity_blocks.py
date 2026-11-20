"""Capacity Block search, error mapping, and the max-capacity route, against botocore Stubbers."""
import json
import os
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,p5en.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import capacity_blocks as cb  # noqa: E402
import families  # noqa: E402
import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import ANY, Stubber  # noqa: E402

UNSUPPORTED = ("The instance type 'g6e.12xlarge' is not supported for Capacity Blocks in 'us-east-1'. "
               "Specify a compatible instance type and try again.")


@pytest.fixture
def ec2(monkeypatch):
    """EC2 Stubber for us-east-1 (Capacity Block calls only)."""
    monkeypatch.setattr(api, "_clients", {})
    monkeypatch.setattr(api, "_offering_mem", {})
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", ["p5.*", "p5en.*", "g6e.*", "g7*"])
    c = boto3.client("ec2", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    api._clients[("ec2", "us-east-1")] = c
    s = Stubber(c)
    # Capacity Block routes validate the type with the Region's detected list (cb_types; None in the
    # tests: the capacity_blocks.FAMILIES fallback), not with the offering item.
    yield s
    s.assert_no_pending_responses()


def call(path, q=None):
    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": path, "queryStringParameters": q}, None)
    return r["statusCode"], json.loads(r["body"])


def offering(start, az="us-east-1a", count=1, hours=24):
    return {"CapacityBlockOfferingId": f"cb-{start:%d%H}", "InstanceType": "p5.48xlarge", "AvailabilityZone": az,
            "InstanceCount": count, "StartDate": start, "EndDate": start + timedelta(hours=hours),
            "CapacityBlockDurationHours": hours, "UpfrontFee": "0.00", "CurrencyCode": "USD", "Tenancy": "default"}


def base(count=1, hours=24, itype="p5.48xlarge"):
    return {"InstanceType": itype, "InstanceCount": count, "CapacityDurationHours": hours}


# ------------------------------------------------------------ pure helpers
def test_families_match_the_documented_list():
    assert cb.FAMILIES == ("p4d", "p4de", "p5", "p5e", "p5en", "p6-b200", "p6-b300", "p6e-gb200", "trn1", "trn2")
    assert cb.is_family("p5.48xlarge") and cb.is_family("p5en.48xlarge") and cb.is_family("p6e-gb200.36xlarge")
    assert not cb.is_family("g6e.12xlarge") and not cb.is_family("p5x.48xlarge") and not cb.is_family(None)


@pytest.mark.parametrize("text,expected", [
    ("Fri Nov 20 07:22:48 UTC 2026", datetime(2026, 11, 20, 7, 22, 48, tzinfo=timezone.utc)),
    ("2026-11-20T07:22:48Z", datetime(2026, 11, 20, 7, 22, 48, tzinfo=timezone.utc)),
    ("2026-11-20T07:22:48", datetime(2026, 11, 20, 7, 22, 48, tzinfo=timezone.utc)),
    ("next week", None),
])
def test_parse_date(text, expected):
    assert cb.parse_date(text) == expected


def test_sanitize_strips_request_ids_and_caps_length():
    text = cb.sanitize("Bad value\n for 123456789012. (Request ID: 0a1b-2c3d-4e5f)")
    assert text == "Bad value for <account>." and len(cb.sanitize("x" * 500)) == 200


@pytest.mark.parametrize("code,message,expected", [
    ("InvalidParameterValue", UNSUPPORTED, "cb-unsupported-type"),
    ("Unsupported", "The instance type is not supported in this zone.", "cb-unsupported-type"),
    ("UnsupportedOperation", "The operation is not supported.", "cb-unsupported"),
    ("InvalidParameterValue", "The duration is not valid. Specify a new duration and try again.", "cb-invalid-duration"),
    ("InvalidParameterValue", "The instance count is not valid. Refer to the user guide.", "cb-invalid-count"),
    ("InvalidParameterValue", "Something else.", "cb-invalid-request"),
    ("RequestLimitExceeded", "Slow down.", None),
])
def test_error_code(code, message, expected):
    assert cb.error_code(code, message) == expected


@pytest.mark.parametrize("code,message,expected", [
    ("RequestLimitExceeded", "Request limit exceeded.", True),
    ("Throttling", "", True),
    ("ThrottlingException", "", True),
    ("SomethingElse", "Rate exceeded", True),
    ("InvalidParameterValue", "The instance count is not valid.", False),
    ("InternalError", "An internal error has occurred.", False),
])
def test_is_throttle(code, message, expected):
    err = ClientError({"Error": {"Code": code, "Message": message}}, "DescribeCapacityBlockOfferings")
    assert cb.is_throttle(err) is expected
    assert cb.is_throttle(ValueError("Rate exceeded")) is False


# ------------------------------------------------------- /api/capacity-blocks
def test_search_sends_no_window_by_default(ec2):
    early, late = datetime(2030, 1, 3, tzinfo=timezone.utc), datetime(2030, 1, 9, tzinfo=timezone.utc)
    ec2.add_response("describe_capacity_block_offerings",
                     {"CapacityBlockOfferings": [offering(late, "us-east-1b")], "NextToken": "n"}, base(2))
    ec2.add_response("describe_capacity_block_offerings",
                     {"CapacityBlockOfferings": [offering(early)]}, {**base(2), "NextToken": "n"})
    ec2.activate()
    status, body = call("/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p5.48xlarge", "days": "1", "count": "2"})
    assert status == 200 and "note" not in body and "notes" not in body
    assert [o["AvailabilityZone"] for o in body["offerings"]] == ["us-east-1a", "us-east-1b"]


def test_start_date_is_sent_at_the_start_of_that_day_and_never_in_the_past(ec2, monkeypatch):
    day = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ec2.add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": []}, {**base(), "StartDateRange": day})
    ec2.add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": []}, {**base(), "StartDateRange": ANY})
    ec2.activate()
    q = {"region": "us-east-1", "instanceType": "p5.48xlarge", "startDate": day.strftime("%Y-%m-%d")}
    assert call("/api/capacity-blocks", q)[0] == 200
    seen = {}
    real = cb.search
    monkeypatch.setattr(cb, "search", lambda *a, **k: (seen.update(k), real(*a, **k))[1])
    before = datetime.now(timezone.utc)
    assert call("/api/capacity-blocks", {**q, "startDate": "2020-01-01"})[0] == 200
    assert seen["start"] >= before and seen["end"] is None


def test_end_date_over_the_ec2_limit_is_retried_once_at_the_limit(ec2):
    now = datetime.now(timezone.utc)
    limit = (now + timedelta(days=9)).replace(microsecond=0)
    end_day = (now + timedelta(days=30)).strftime("%Y-%m-%d")
    asked_end = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    ec2.add_client_error(
        "describe_capacity_block_offerings", "InvalidParameterValue", http_status_code=400,
        service_message=f"The end date is not valid. End date must not be later than {limit:%a %b %d %H:%M:%S} UTC "
                        f"{limit:%Y}. Specify a new end date and try again.",
        expected_params={**base(), "EndDateRange": asked_end})
    ec2.add_response("describe_capacity_block_offerings",
                     {"CapacityBlockOfferings": [offering(now + timedelta(days=2))]}, {**base(), "EndDateRange": limit})
    ec2.activate()
    status, body = call("/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p5.48xlarge", "endDate": end_day})
    assert status == 200 and len(body["offerings"]) == 1
    assert "limited" in body["note"] and limit.strftime("%Y-%m-%dT%H:%M:%SZ") in body["note"]
    assert body["notes"] == [{"code": "cb-window-end", "params": {"date": limit.strftime("%Y-%m-%dT%H:%M:%SZ")},
                              "message": body["note"]}]


def test_start_date_over_the_ec2_limit_is_clamped_and_filtered(ec2):
    now = datetime.now(timezone.utc)
    limit = (now + timedelta(days=2)).replace(microsecond=0)
    day = (now + timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0)
    ec2.add_client_error(
        "describe_capacity_block_offerings", "InvalidParameterValue", http_status_code=400,
        service_message=f"The start date is not valid. Start date {day:%a %b %d %H:%M:%S} UTC {day:%Y} must be before "
                        f"{limit:%a %b %d %H:%M:%S} UTC {limit:%Y}. Specify a new start date and try again.",
        expected_params={**base(), "StartDateRange": day})
    ec2.add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": [
        offering(now + timedelta(days=3)), offering(day + timedelta(hours=11), "us-east-1c")]},
        {**base(), "StartDateRange": limit - timedelta(minutes=1)})
    ec2.activate()
    status, body = call("/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p5.48xlarge", "startDate": day.strftime("%Y-%m-%d")})
    assert status == 200 and [o["AvailabilityZone"] for o in body["offerings"]] == ["us-east-1c"]
    assert "start date before" in body["note"]
    assert [n["code"] for n in body["notes"]] == ["cb-window-start"]
    assert body["notes"][0]["params"] == {"date": limit.strftime("%Y-%m-%dT%H:%M:%SZ")}


def test_unsupported_type_returns_400_with_the_ec2_message_and_a_code(ec2):
    ec2.add_client_error("describe_capacity_block_offerings", "InvalidParameterValue", http_status_code=400,
                         service_message=UNSUPPORTED + " (Request ID: 1234-abcd)")
    ec2.activate()
    # A type of the fallback families that EC2 itself refuses.
    status, body = call("/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p4de.24xlarge"})
    assert status == 400 and body["code"] == "cb-unsupported-type"
    assert body["error"] == UNSUPPORTED and "Request ID" not in body["error"]


def test_a_type_outside_the_capacity_block_families_is_refused_before_ec2(ec2):
    ec2.activate()
    status, body = call("/api/capacity-blocks", {"region": "us-east-1", "instanceType": "g6e.12xlarge"})
    assert status == 400 and body["code"] == "cb-unsupported-type" and "p5" in body["params"]["families"]


def test_invalid_duration_is_a_400_throttling_a_429_and_other_aws_errors_stay_502(ec2):
    ec2.add_client_error("describe_capacity_block_offerings", "InvalidParameterValue", http_status_code=400,
                         service_message="The duration is not valid. Specify a new duration and try again.")
    ec2.add_client_error("describe_capacity_block_offerings", "RequestLimitExceeded", http_status_code=503,
                         service_message="Request limit exceeded.")
    ec2.add_client_error("describe_capacity_block_offerings", "InternalError", http_status_code=500,
                         service_message="An internal error has occurred.")
    ec2.activate()
    q = {"region": "us-east-1", "instanceType": "p5.48xlarge", "days": "14"}
    status, body = call("/api/capacity-blocks", q)
    assert status == 400 and body["code"] == "cb-invalid-duration"
    status, body = call("/api/capacity-blocks", q)
    assert status == 429 and body == {"error": api.CB_THROTTLED_MESSAGE, "code": "throttled"}
    status, body = call("/api/capacity-blocks", q)
    assert status == 502 and body == {"error": "AWS call failed (InternalError)", "code": "aws-error",
                                      "params": {"awsCode": "InternalError"}}


@pytest.mark.parametrize("code,message", [
    ("RequestLimitExceeded", "Request limit exceeded."),
    ("Throttling", "Rate exceeded"),
    ("ThrottlingException", "Rate exceeded"),
    ("ServiceUnavailable", "Rate exceeded"),
])
def test_search_answers_429_throttled_for_every_ec2_throttle(ec2, code, message):
    ec2.add_client_error("describe_capacity_block_offerings", code, http_status_code=400, service_message=message)
    ec2.activate()
    status, body = call("/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p5.48xlarge"})
    assert (status, body["code"]) == (429, "throttled")


def test_ec2_clients_use_standard_retries_with_three_attempts(monkeypatch):
    monkeypatch.setattr(api, "_clients", {})
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    retries = api.client("ec2", "us-east-1").meta.config.retries
    assert retries["mode"] == "standard" and retries["total_max_attempts"] == 3
    # Other clients get their own bounded config with fewer attempts (see test_region_timeouts.py).
    assert api._client_options("cloudwatch")["config"].retries["total_max_attempts"] == 2


@pytest.mark.parametrize("q,needle", [
    ({"startDate": "2030-13-01"}, "YYYY-MM-DD"),
    ({"startDate": "tomorrow"}, "YYYY-MM-DD"),
    ({"endDate": "2020-01-01"}, "after"),
    ({"startDate": "2999-01-01"}, "within"),
    ({"count": "65"}, "between"),
])
def test_search_rejects_bad_input_before_ec2(ec2, q, needle):
    ec2.activate()
    status, body = call("/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p5.48xlarge", **q})
    # A request problem, not an EC2 answer: the code is a generic one, never a cb-* code.
    assert status == 400 and needle in body["error"] and not body["code"].startswith("cb-")


# ----------------------------------------------- instance types and config
def test_instance_types_can_keep_only_capacity_block_families(ec2):
    ec2.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [
        {"InstanceType": t, "LocationType": "region", "Location": "us-east-1"}
        for t in ("g6e.12xlarge", "p5.48xlarge", "p5en.48xlarge")]})
    ec2.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [
        {"InstanceType": t, "LocationType": "availability-zone", "Location": "us-east-1a"}
        for t in ("g6e.12xlarge", "p5.48xlarge", "p5en.48xlarge")]},
        {"LocationType": "availability-zone", "Filters": [{"Name": "instance-type", "Values": families.filter_values()}]})
    ec2.activate()
    assert call("/api/instance-types", {"region": "us-east-1"})[1]["instanceTypes"] == ["g6e.12xlarge", "p5.48xlarge", "p5en.48xlarge"]
    status, body = call("/api/instance-types", {"region": "us-east-1", "capacityBlocks": "true"})
    assert status == 200 and body["instanceTypes"] == ["p5.48xlarge", "p5en.48xlarge"]
    assert call("/api/instance-types", {"region": "us-east-1", "capacityBlocks": "yes"})[0] == 400


def test_config_lists_capacity_block_families_and_interval(monkeypatch):
    monkeypatch.setattr(api, "NOTIFY_INTERVAL_MINUTES", 30)
    _, body = call("/api/config")
    assert body["capacityBlockFamilies"] == list(cb.FAMILIES) and body["notifyIntervalMinutes"] == 30
    assert body["capacityBlockMaxCount"] == 64


# --------------------------------------------------- /api/capacity-blocks/max
def binary_reference(available, high=cb.MAX_COUNT):
    """The earlier sequential binary search, kept here as the reference the staged search must match."""
    low, top, best = 0, high + 1, []
    while top - low > 1:
        mid = (low + top) // 2
        found = available(mid)
        if found:
            low, best = mid, found
        else:
            top = mid
    return low, best


def count_rounds(monkeypatch):
    """Counts the parallel rounds of cb.largest_count (one wait per round)."""
    rounds = []
    real = cb.wait
    monkeypatch.setattr(cb, "wait", lambda fs, *a, **k: (rounds.append(len(fs)), real(fs, *a, **k))[1])
    return rounds


def gate_rounds(monkeypatch, f):
    """Counts the parallel rounds of cb.largest_count like count_rounds, and proves each round runs in
    parallel without a clock: the calls of a round stay in flight until all of them are (or 2 s pass),
    then the round goes on. Returns (rounds, in_flight): in_flight[i] == rounds[i] when round i ran all
    its calls at once; a sequential or narrower pool leaves fewer in flight."""
    rounds, in_flight = [], []
    real = cb.wait
    f.gate = threading.Event()

    def gated(fs, *a, **k):
        rounds.append(len(fs))
        with f.cond:
            f.cond.wait_for(lambda: f.active >= len(fs), timeout=2)
            in_flight.append(f.active)
        f.gate.set()
        try:
            return real(fs, *a, **k)
        finally:
            f.gate.clear()
    monkeypatch.setattr(cb, "wait", gated)
    return rounds, in_flight


class FakeEc2:
    """Thread-safe DescribeCapacityBlockOfferings fake: offerings up to `answer` instances, each call
    sleeping `delay` seconds. `fail` maps a count to a ClientError (code, message) for that count.
    When `gate` (an Event, see gate_rounds) is set up, each call stays in flight until it opens.
    It has no paginator, so a paginated call fails."""

    def __init__(self, answer, delay=0.0, fail=None, itype="p5.48xlarge"):
        self.answer, self.delay, self.fail, self.itype = answer, delay, fail or {}, itype
        self.calls, self.kwargs = [], []
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.gate = None

    def describe_capacity_block_offerings(self, **kwargs):
        count = kwargs["InstanceCount"]
        with self.lock:
            self.calls.append(count)
            self.kwargs.append(kwargs)
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.cond.notify_all()
        try:
            if self.gate is not None:
                self.gate.wait(timeout=5)
            if self.delay:
                time.sleep(self.delay)
            if count in self.fail:
                code, message = self.fail[count]
                raise ClientError({"Error": {"Code": code, "Message": message}}, "DescribeCapacityBlockOfferings")
            if count > self.answer:
                return {"CapacityBlockOfferings": []}
            t = datetime(2030, 1, 5, tzinfo=timezone.utc)
            # Two offerings per count, the later one first: the earliest one is the answer.
            hours = kwargs["CapacityDurationHours"]
            return {"CapacityBlockOfferings": [offering(t + timedelta(days=2), "us-east-1b", count, hours),
                                               offering(t + timedelta(minutes=count), "us-east-1c", count, hours)]}
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def fake(monkeypatch):
    """Installs a FakeEc2 as the us-east-1 EC2 client; region offerings come from a fixed list."""
    monkeypatch.setattr(api, "_clients", {})
    monkeypatch.setattr(api, "region_offerings", lambda region: ["g6e.12xlarge", "p5.48xlarge"])

    def install(*args, **kwargs):
        f = FakeEc2(*args, **kwargs)
        api._clients[("ec2", "us-east-1")] = f
        return f
    return install


Q = {"region": "us-east-1", "instanceType": "p5.48xlarge"}
FINAL_KEYS = {"region", "instanceType", "days", "maxCount", "earliest", "done"}
STAGE_KEYS = {"region", "instanceType", "days", "done", "lo", "hi", "probed", "callsUsed"}


def run_search(f, q=None, max_requests=4, rounds=None):
    """The page's loop against the handler: stage 1, then the lo and hi of each answer until done:
    true (at most `max_requests`). Returns one record per request: status, body, the counts the
    fake EC2 saw, the parallel rounds (when `rounds` counts them), and the wall time."""
    q = dict(q or Q)
    out, params = [], dict(q)
    for _ in range(max_requests):
        seen, waits = len(f.calls), len(rounds) if rounds is not None else 0
        started = time.monotonic()
        status, body = call("/api/capacity-blocks/max", params)
        out.append({"status": status, "body": body, "calls": f.calls[seen:], "seconds": time.monotonic() - started,
                    "rounds": rounds[waits:] if rounds is not None else None})
        if status != 200 or body["done"]:
            break
        params = {**q, "lo": str(body["lo"]), "hi": str(body["hi"])}
    return out


def reference_answer(answer, days=1):
    """The earlier sequential binary search through its own fake: (count, the summary it would show)."""
    ref = FakeEc2(answer)
    count, offerings = binary_reference(lambda c: cb.search(ref, "p5.48xlarge", c, days * 24, max_items=1000, paginate=False)[0])
    earliest = cb.summary(min(offerings, key=lambda o: o["StartDate"])) if count else None
    # As the route sends it (dates in ISO 8601).
    return count, json.loads(json.dumps(earliest, default=lambda d: d.isoformat()))


def expected_requests(answer):
    return 1 if answer in (0, 1, 64) else 2 if answer < 32 else 3


@pytest.mark.parametrize("answer", range(0, 65))
def test_staged_requests_match_the_binary_search_for_every_answer(fake, monkeypatch, answer):
    """The page's loop against a gated thread-safe fake: same answer and earliest offering as the old
    sequential binary search, at most 3 requests, 16 calls in all, 14 per request (7 in practice),
    1 parallel round in stage 1 and at most 2 in a follow-up, every call of a round in flight at once."""
    f = fake(answer)
    rounds, in_flight = gate_rounds(monkeypatch, f)
    out = run_search(f, rounds=rounds)
    final = out[-1]["body"]
    count, earliest = reference_answer(answer)
    assert all(r["status"] == 200 for r in out) and final["done"] is True
    assert final["maxCount"] == count == answer and final.get("earliest") == earliest
    assert set(final) == FINAL_KEYS - (set() if answer else {"earliest"})
    assert len(out) == expected_requests(answer) <= 3
    total = sum(len(r["calls"]) for r in out)
    assert total == len(f.calls) <= 16 and all(len(r["calls"]) <= 14 for r in out)
    assert sorted(out[0]["calls"]) == [1, 2, 4, 8, 16, 32, 64] and f.peak <= 7
    for i, r in enumerate(out):
        assert len(set(r["calls"])) == len(r["calls"]) and all(1 <= c <= 64 for c in r["calls"])
        assert len(r["rounds"]) <= (1 if i == 0 else 2) and all(n <= 7 for n in r["rounds"]) and sum(r["rounds"]) == len(r["calls"])
    # Parallel rounds: every call of a round was in flight at the same time (no clock involved).
    assert in_flight == rounds, (in_flight, rounds)
    for r in out[:-1]:
        b = r["body"]
        assert set(b) == STAGE_KEYS and b["done"] is False and 0 <= b["lo"] < b["hi"] <= 65
        assert b["lo"] <= answer < b["hi"] and b["probed"] == b["callsUsed"] == len(r["calls"])
    # One page per call, MaxResults as before, no window.
    assert all(k == {**base(k["InstanceCount"]), "MaxResults": 1000} for k in f.kwargs)


@pytest.mark.slow
def test_wall_time_per_request(fake, monkeypatch):
    """The one real-clock check (the round shape is pinned without a clock above). With 0.2 s per EC2
    call: stage 1 takes about one call, a follow-up at most about two, far below the calls one after
    another (stage 1 alone would be 1.4 s); at 5 s per call that is about 5 s and 10 s per request."""
    answer, delay = 45, 0.2  # 3 requests: stage 1 and two follow-ups
    f = fake(answer, delay=delay)
    rounds = count_rounds(monkeypatch)
    out = run_search(f, rounds=rounds)
    assert out[-1]["body"]["maxCount"] == answer and len(out) == 3
    for i, r in enumerate(out):
        assert len(r["rounds"]) <= (1 if i == 0 else 2)
        # One round of slack for a busy machine; a sequential round would take len(calls) * delay.
        assert r["seconds"] < (len(r["rounds"]) + 1) * delay, (i, r["seconds"], r["rounds"])
    sequential = len(f.calls) * delay
    print(f"answer {answer}: " + ", ".join(f"{r['seconds']:.3f}s/{len(r['calls'])} calls" for r in out) + f"; sequential {sequential:.1f}s")


def test_staged_search_requests_calls_and_rounds_over_all_answers(monkeypatch):
    """The table for the change description: requests, EC2 calls, and rounds per answer 0 to 64,
    driving cb.search_stage the way the page drives the route."""
    rounds = count_rounds(monkeypatch)
    rows = []
    for answer in range(65):
        lo = hi = None
        per = []
        for _ in range(4):
            rounds.clear()
            done, lo, hi, found = cb.search_stage(lambda c, a=answer: [c] if c <= a else [], lo, hi)
            per.append((len(found), len(rounds)))
            if done:
                break
        assert done and lo == answer
        rows.append((answer, len(per), sum(c for c, _ in per), sum(n for _, n in per), max(c for c, _ in per), max(n for _, n in per)))
    requests, calls, total_rounds = ([r[i] for r in rows] for i in (1, 2, 3))
    assert max(requests) == 3 and max(calls) == 15 <= cb.MAX_CALLS and max(total_rounds) == 4
    assert max(r[4] for r in rows) == 7 <= cb.MAX_REQUEST_CALLS and max(r[5] for r in rows) == 2
    assert min(calls) == 7 and [r[0] for r in rows if r[1] == 1] == [0, 1, 64]
    print("answer requests calls rounds per-request-calls(max) per-request-rounds(max)")
    for r in rows:
        print(*r)
    for name, v in (("requests", requests), ("calls", calls), ("rounds", total_rounds)):
        print(f"{name}: max={max(v)} mean={sum(v) / 65:.2f}")


class InlinePool:
    """A ThreadPoolExecutor stand-in that runs each call at submit (the bracket logic, not the threads,
    is under test; the gated fake above pins the parallel rounds)."""

    def __init__(self, max_workers=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as err:  # noqa: BLE001 - handed to the caller through the future, like a pool
            future.set_exception(err)
        return future


def test_any_valid_bracket_stays_within_the_per_request_cap(monkeypatch):
    """Whatever lo and hi a client sends (every 0 <= lo < hi <= 65), and whether or not they match
    EC2: at most 2 rounds, 14 calls, no count twice, only counts from 1 to 64."""
    monkeypatch.setattr(cb, "ThreadPoolExecutor", InlinePool)
    rounds = count_rounds(monkeypatch)
    worst = 0
    for lo in range(0, 65):
        for hi in range(lo + 1, 66):
            for answer in {0, lo, (lo + hi) // 2, hi - 1, 64}:
                rounds.clear()
                seen = []
                done, new_lo, new_hi, found = cb.search_stage(lambda c, a=answer: (seen.append(c), [c] if c <= a else [])[1], lo, hi)
                assert len(seen) == len(set(seen)) == len(found) <= cb.MAX_REQUEST_CALLS and len(rounds) <= 2
                assert all(1 <= c <= 64 for c in seen) and 0 <= new_lo < new_hi <= 65
                if lo <= answer < hi:
                    # A truthful bracket only narrows, and keeps the answer inside.
                    assert lo <= new_lo <= answer < new_hi <= hi and (not done or new_lo == answer)
                worst = max(worst, len(seen))
    assert worst == 7


def test_the_per_request_guard_caps_calls_and_never_repeats_a_count(monkeypatch):
    """The loop guard: rounds that propose counts already asked, or too many, still stop at 14 calls."""
    seen = []
    monkeypatch.setattr(cb, "_next_points", lambda lo, hi, found: [0, 1, 2, 2, *range(lo, hi + 2)])
    done, lo, hi, found = cb.search_stage(lambda c: (seen.append(c), [c] if c <= 40 else [])[1], 0, 65)
    assert len(seen) == 14 == len(set(seen)) and all(1 <= c <= 64 for c in seen) and not done


def test_a_follow_up_whose_lower_end_lost_its_offering_searches_below_it(fake):
    """lo had offerings in an earlier request, none now: lo becomes the upper end and the search goes on."""
    f = fake(39)
    status, body = call("/api/capacity-blocks/max", {**Q, "lo": "40", "hi": "42"})
    assert status == 200 and (body["done"], body["lo"], body["hi"]) == (False, 30, 40)
    assert sorted(f.calls) == [10, 20, 30, 40, 41] and body["callsUsed"] == 5
    out = run_search(f, max_requests=3)
    assert out[-1]["body"]["maxCount"] == 39


def test_a_count_above_a_lower_end_that_lost_its_offering_still_wins(fake):
    """Not monotonic for a moment (40 has none, 41 has one): the hit above lo is the answer."""
    f = fake(41)
    f.fail = {40: ("InvalidParameterValue", "The instance count is not valid.")}
    status, body = call("/api/capacity-blocks/max", {**Q, "lo": "40", "hi": "42"})
    assert status == 200 and body["done"] is True and body["maxCount"] == 41 and sorted(f.calls) == [40, 41]


@pytest.mark.parametrize("fail,status,mapped", [
    ({8: ("Throttling", "Rate exceeded"), 32: ("InternalError", "An internal error has occurred.")}, 429, "throttled"),
    ({8: ("InternalError", "An internal error has occurred."), 32: ("Throttling", "Rate exceeded")}, 502, "aws-error"),
])
def test_two_errors_in_one_round_report_the_smallest_counts(fake, fail, status, mapped):
    f = fake(40, fail=fail)
    got, body = call("/api/capacity-blocks/max", Q)
    assert (got, body["code"]) == (status, mapped) and len(f.calls) == 7


def test_lo_without_hi_names_the_rule(fake):
    fake(40)
    for params, name in (({"lo": "4"}, "hi"), ({"hi": "8"}, "lo")):
        status, body = call("/api/capacity-blocks/max", {**Q, **params})
        assert (status, body["params"]["name"]) == (400, name) and "both or neither" in body["error"]


@pytest.mark.parametrize("params,calls,body", [
    ({"lo": "0", "hi": "1"}, [], {"done": True, "maxCount": 0}),
    ({"lo": "64", "hi": "65"}, [64], {"done": True, "maxCount": 64}),
    ({"lo": "5", "hi": "6"}, [5], {"done": True, "maxCount": 5}),
    ({"lo": "4", "hi": "8"}, [4, 5, 6, 7], {"done": True, "maxCount": 7}),
    ({"lo": "0", "hi": "65"}, [16, 32, 48, 52, 56, 60], {"done": False, "lo": 60, "hi": 65}),
])
def test_follow_up_requests_at_the_edges(fake, params, calls, body):
    f = fake(64 if params["hi"] in ("65", "8", "6") else 0)
    if params == {"lo": "5", "hi": "6"}:
        f.answer = 5
    status, got = call("/api/capacity-blocks/max", {**Q, **params})
    assert status == 200 and sorted(f.calls) == calls
    assert {k: got[k] for k in body} == body


@pytest.mark.parametrize("params,name", [
    ({"lo": "a", "hi": "8"}, "lo"), ({"lo": "4", "hi": "b"}, "hi"), ({"lo": "1.5", "hi": "8"}, "lo"),
    ({"lo": " 4", "hi": "8"}, "lo"), ({"lo": "+4", "hi": "8"}, "lo"), ({"lo": "-1", "hi": "8"}, "lo"),
    ({"lo": "04", "hi": "8"}, "lo"), ({"lo": "4", "hi": "1_0"}, "hi"), ({"lo": "\u0664", "hi": "8"}, "lo"),
    ({"lo": "4", "hi": "0x10"}, "hi"), ({"lo": "4", "hi": "9" * 400}, "hi"), ({"lo": "4", "hi": "66"}, "hi"),
    ({"lo": "4", "hi": "100"}, "hi"), ({"lo": "4"}, "hi"), ({"hi": "8"}, "lo"), ({"lo": "4", "hi": ""}, "hi"),
    ({"lo": "8", "hi": "8"}, "lo"), ({"lo": "9", "hi": "8"}, "lo"), ({"lo": "65", "hi": "65"}, "lo"),
    ({"lo": "4", "hi": "8\n"}, "hi"),
])
def test_lo_and_hi_are_validated_strictly_before_any_ec2_call(fake, params, name):
    f = fake(40)
    status, body = call("/api/capacity-blocks/max", {**Q, **params})
    assert (status, body["code"], body["params"]) == (400, "bad-parameter", {"name": name})
    assert f.calls == []


def test_max_capacity_treats_a_rejected_count_as_none(fake):
    invalid = ("InvalidParameterValue", "The instance count is not valid. Refer to the Capacity Blocks user guide.")
    f = fake(16, fail={c: invalid for c in range(17, 65)})
    out = run_search(f)
    body = out[-1]["body"]
    assert body["done"] and body["maxCount"] == 16 and body["earliest"]["instanceCount"] == 16
    assert len(out) == 2 and len(f.calls) <= 16


def test_max_capacity_unsupported_type_is_a_400_after_the_first_round(fake):
    f = fake(64, fail={c: ("InvalidParameterValue", UNSUPPORTED) for c in range(1, 65)}, itype="p4de.24xlarge")
    status, body = call("/api/capacity-blocks/max", {"region": "us-east-1", "instanceType": "p4de.24xlarge"})
    assert status == 400 and body["code"] == "cb-unsupported-type" and body["error"] == UNSUPPORTED
    assert sorted(f.calls) == [1, 2, 4, 8, 16, 32, 64]


def test_an_offering_error_in_a_follow_up_stops_that_request_after_its_round(fake, monkeypatch):
    rounds = count_rounds(monkeypatch)
    f = fake(45, fail={48: ("InvalidParameterValue", UNSUPPORTED)})
    status, body = call("/api/capacity-blocks/max", {**Q, "lo": "32", "hi": "64"})
    assert status == 400 and body["code"] == "cb-unsupported-type" and sorted(f.calls) == [40, 48, 56] and rounds == [3]


@pytest.mark.parametrize("code,message,status,mapped", [
    ("RequestLimitExceeded", "Request limit exceeded.", 429, "throttled"),
    ("Throttling", "Rate exceeded", 429, "throttled"),
    ("ThrottlingException", "Rate exceeded", 429, "throttled"),
    ("InternalError", "An internal error has occurred.", 502, "aws-error"),
])
def test_max_capacity_ec2_error_in_one_probe_fails_the_request_without_another_round(fake, monkeypatch, code, message, status, mapped):
    rounds = count_rounds(monkeypatch)
    f = fake(40, fail={8: (code, message)})
    got, body = call("/api/capacity-blocks/max", Q)
    assert (got, body["code"]) == (status, mapped) and len(f.calls) == 7
    # The same in a follow-up: its first round finishes, no second round starts.
    rounds.clear()
    f = fake(45, fail={40: (code, message), 56: (code, message)})
    got, body = call("/api/capacity-blocks/max", {**Q, "lo": "32", "hi": "64"})
    assert (got, body["code"]) == (status, mapped) and sorted(f.calls) == [40, 48, 56] and rounds == [3]


def test_max_capacity_no_offering_at_all(fake):
    f = fake(0)
    status, body = call("/api/capacity-blocks/max", {**Q, "days": "7"})
    assert status == 200 and body == {"region": "us-east-1", "instanceType": "p5.48xlarge", "days": 7, "done": True, "maxCount": 0}
    assert sorted(f.calls) == [1, 2, 4, 8, 16, 32, 64]


@pytest.mark.parametrize("answer", [5, 47])
def test_follow_ups_keep_the_duration(fake, answer):
    f = fake(answer)
    out = run_search(f, {**Q, "days": "2"})
    e = out[-1]["body"]["earliest"]
    assert (e["zone"], e["instanceCount"], e["durationHours"], e["upfrontFee"]) == ("us-east-1c", answer, 48, "0.00")
    assert all(k["CapacityDurationHours"] == 48 for k in f.kwargs) and all(r["body"]["days"] == 2 for r in out)


@pytest.mark.parametrize("q", [
    {"region": "eu-west-1", "instanceType": "p5.48xlarge"},
    {"region": "us-east-1", "instanceType": "p5.48xlarge", "days": "15"},
    {"region": "us-east-1", "instanceType": "m5.large"},
])
def test_max_capacity_validates_like_the_other_routes(ec2, q):
    ec2.activate()
    assert call("/api/capacity-blocks/max", q)[0] == 400


def test_start_date_is_at_most_14_utc_days_ahead(ec2):
    """startDate is a UTC day: today + 14 days is accepted, today + 15 days is a 400 before any EC2 search."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    last = today + timedelta(days=14)
    ec2.add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": []}, {**base(), "StartDateRange": last})
    ec2.activate()
    q = {"region": "us-east-1", "instanceType": "p5.48xlarge"}
    assert call("/api/capacity-blocks", {**q, "startDate": last.strftime("%Y-%m-%d")})[0] == 200
    status, body = call("/api/capacity-blocks", {**q, "startDate": (last + timedelta(days=1)).strftime("%Y-%m-%d")})
    assert (status, body["code"], body["params"]) == (400, "bad-parameter", {"name": "startDate"})
    assert "14 days" in body["error"] and "UTC" in body["error"]
