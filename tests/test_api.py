import json
import os

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t", ENABLE_PROBE="true")

import api  # noqa: E402
import pytest  # noqa: E402


OFFERS = {
    "us-east-1": {"families": {"p5": ["p5.48xlarge"]}, "zones": {"p5.48xlarge": ["us-east-1a", "us-east-1b"]},
                  "fetchedAt": "2033-05-18T03:00:00.000000Z", "fromCache": True},
    "us-west-2": {"families": {}, "zones": {}, "fetchedAt": "2033-05-18T03:00:00.000000Z", "fromCache": True},
}


@pytest.fixture(autouse=True)
def offered(monkeypatch):
    """Pretend the offering cache says p5.48xlarge in us-east-1 (zones a and b only) and nothing in us-west-2."""
    monkeypatch.setattr(api, "_offering_entry", lambda region: OFFERS[region])


def event(method, path, query=None, body=None):
    return {
        "requestContext": {"http": {"method": method, "path": path},
                           "authorizer": {"jwt": {"claims": {"cognito:username": "alice", "sub": "sub-alice",
                                                                 "token_use": "id"}}}},
        "rawPath": path, "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.mark.parametrize("code,expected", [
    ("InsufficientInstanceCapacity", "no-capacity"), ("InsufficientHostCapacity", "no-capacity"),
    ("VcpuLimitExceeded", "quota"), ("MaxSpotInstanceCountExceeded", "quota"),
    ("Unsupported", "unsupported"), ("UnauthorizedOperation", "error"),
])
def test_classify_error(code, expected):
    assert api.classify_error(code) == expected


@pytest.mark.parametrize("body", [
    {"region": "us-east-1", "instanceType": "p5.48xlarge", "mode": "reserved"},
    {"region": "us-east-1", "instanceType": "p5.48xlarge", "market": "capacity-block"},
    {"region": "us-east-1", "instanceType": "g5.xlarge", "mode": "spot"},
    {"region": "us-west-2", "instanceType": "p5.48xlarge", "mode": "spot"},
    {"region": "eu-west-1", "instanceType": "p5.48xlarge", "mode": "spot"},
    {"region": "us-east-1", "mode": "spot"},
    {"region": "us-east-1", "group": "p5", "mode": "spot"},
    {"region": "us-east-1", "group": "p5/8", "mode": "spot"},
])
def test_probe_rejects_bad_input(body):
    assert api.handler(event("POST", "/api/probe", body=body), None)["statusCode"] == 400


@pytest.mark.parametrize("field,itype", [("instanceType", "gr6.4xlarge"), ("instanceType", "trn2.48xlarge"),
                                         ("instanceType", "inf2.xlarge"), ("instanceType", "dl1.24xlarge"),
                                         ("group", "gr6/1"), ("group", "trn2/16")])
def test_a_probe_for_an_untracked_family_is_refused_before_any_ec2_call(monkeypatch, field, itype):
    """The probe IAM condition can only hold coarse wildcards (g*, p*), so the function itself is
    what keeps a launch inside the tracked-family rule. Nothing reaches EC2."""
    def never(*args, **kwargs):
        pytest.fail(f"a refused probe must not call AWS: {args} {kwargs}")
    monkeypatch.setattr(api, "client", never)
    monkeypatch.setattr(api, "run_probe", never)
    monkeypatch.setattr(api, "table", never)
    monkeypatch.setattr(api, "_groups_state", never)
    monkeypatch.setattr(api, "_offering_entry", never)
    body = {"region": "us-east-1", field: itype, "mode": "spot"}
    status, error = body_of(api.handler(event("POST", "/api/probe", body=body), None))
    assert status == 400 and error["code"] == "type-not-enabled"
    assert error["params"]["families"] == "g and p (GPU) instance families"


def test_instance_type_must_match_family_and_be_offered():
    q = {"region": "us-east-1", "instanceType": "g6e.12xlarge"}  # allowed family, not offered
    r = api.handler(event("GET", "/api/instance-type-zones", q), None)
    assert r["statusCode"] == 400 and "not offered" in json.loads(r["body"])["error"]
    q = {"region": "us-east-1", "instanceType": "gr6.4xlarge"}  # not an allowed family
    r = api.handler(event("GET", "/api/spot-prices", q), None)
    assert r["statusCode"] == 400 and "enabled family" in json.loads(r["body"])["error"]


@pytest.mark.parametrize("name,ok", [
    # The rule tracks every G and P family by the naming rule (a digit after the letter).
    ("p5.48xlarge", True), ("g7e.2xlarge", True), ("g7.xlarge", True), ("g6e.xlarge", True),
    ("p4d.24xlarge", True), ("g6.xlarge", True), ("g5.xlarge", True), ("p3dn.24xlarge", True),
    ("gr6.4xlarge", False), ("trn2.48xlarge", False),
    # A name that is not one instance type is never a match, whatever the family.
    ("p5", False), ("p5.*", False), ("", False), (None, False), ("p5.48xlarge,g5.xlarge", False),
])
def test_matches_families(name, ok):
    assert api.matches_families(name) is ok


def test_zone_scores_are_removed():
    status, body = body_of(api.handler(event("GET", "/api/spot-scores", {"scope": "az", "region": "us-east-1",
                                                                       "instanceType": "p5.48xlarge"}), None))
    assert status == 400 and body["code"] == "sps-zone-removed"


def test_instance_types_route():
    r = api.handler(event("GET", "/api/instance-types", {"region": "us-east-1"}), None)
    assert json.loads(r["body"]) == {"region": "us-east-1", "instanceTypes": ["p5.48xlarge"],
                                     "fetchedAt": "2033-05-18T03:00:00.000000Z", "fromCache": True}
    assert api.handler(event("GET", "/api/instance-types", {"region": "eu-west-1"}), None)["statusCode"] == 400


def test_instance_type_zones_route():
    r = api.handler(event("GET", "/api/instance-type-zones", {"region": "us-east-1", "instanceType": "p5.48xlarge"}), None)
    body = json.loads(r["body"])
    assert body["zones"] == ["us-east-1a", "us-east-1b"]
    assert (body["fetchedAt"], body["fromCache"]) == ("2033-05-18T03:00:00.000000Z", True)
    r = api.handler(event("GET", "/api/instance-type-zones", {"region": "us-east-1", "instanceType": "g6e.2xlarge"}), None)
    assert r["statusCode"] == 400 and json.loads(r["body"])["code"] == "type-not-offered"


class NoTable:
    def __getattr__(self, name):
        pytest.fail(f"a removed route must not call DynamoDB ({name})")


HOLD = {"region": "us-east-1", "instanceType": "p5.48xlarge", "az": "us-east-1a"}  # the removed hold route's body


@pytest.mark.parametrize("method,path,query,body,signed_in", [
    pytest.param("GET", "/api/nope", None, None, True, id="unknown"),
    # The instance-type list lives in the browser (no server-side Watchlist), for every method.
    pytest.param("GET", "/api/watchlist", None, None, True, id="watchlist-get"),
    pytest.param("PUT", "/api/watchlist", None, {"types": ["p5.48xlarge"]}, True, id="watchlist-put"),
    pytest.param("POST", "/api/watchlist", None, {}, True, id="watchlist-post"),
    pytest.param("DELETE", "/api/watchlist", None, None, True, id="watchlist-delete"),
    pytest.param("GET", "/api/watchlist", None, None, False, id="watchlist-signed-out"),
    # The Quotas tab shows one Region at a time: no all-Region overview.
    pytest.param("GET", "/api/quota-overview", None, None, True, id="quota-overview"),
    pytest.param("GET", "/api/quota-overview", {"refresh": "true"}, None, True, id="quota-overview-refresh"),
    # The capacity-hold feature is gone ("All Regions" listing included).
    pytest.param("GET", "/api/reservations", {"region": "all"}, None, True, id="holds-list"),
    pytest.param("POST", "/api/reservations", {"region": "all"}, HOLD, True, id="holds-create"),
    pytest.param("POST", "/api/reservations/cancel", {"region": "all"}, {"region": "us-east-1", "id": "cr-0123456789abcdef0"},
                 True, id="holds-cancel"),
])
def test_removed_routes_return_404_without_touching_aws(monkeypatch, method, path, query, body, signed_in):
    """Unknown and removed routes answer the plain 404 before any AWS client, table, or cache is used
    (the conftest guard also fails any request that would reach AWS)."""
    monkeypatch.setattr(api.boto3, "resource", lambda *a, **k: NoTable())
    monkeypatch.setattr(api, "client", lambda *a, **k: pytest.fail("a removed route must not create an AWS client"))
    monkeypatch.setattr(api, "_fetch_quota_rows", lambda r: pytest.fail("the removed overview must not ask AWS"))
    monkeypatch.setattr(api, "_cached_quota_rows", lambda r, now: pytest.fail("the removed overview must not read the cache"))
    ev = event(method, path, query=query, body=body)
    if not signed_in:
        del ev["requestContext"]["authorizer"]
    r = api.handler(ev, None)
    if not signed_in:
        # No ID token claims: refused before the route lookup (the JWT authorizer stops this first).
        assert (r["statusCode"], json.loads(r["body"])["code"]) == (401, "no-identity")
        return
    assert (r["statusCode"], json.loads(r["body"])) == (404, {"error": "not found", "code": "not-found"})


def test_server_side_instance_type_list_code_is_gone():
    assert not [k for k in vars(api) if "watchlist" in k.lower()]
    assert not [path for _, path in (*api.ROUTES, *api.BODY_ROUTES) if "watchlist" in path]


def test_all_region_quota_overview_code_is_gone():
    assert not [k for k in vars(api) if "overview" in k.lower()]
    assert not [path for _, path in (*api.ROUTES, *api.BODY_ROUTES) if "overview" in path]


def test_config_has_no_removed_fields():
    body = json.loads(api.handler(event("GET", "/api/config"), None)["body"])
    assert not {"watchlistMax", "apiBurstLimit", "reservationEnabled", "reservationHours", "maxReservationsPerUser",
                "spotScoreRegionGroups", "newConfigReservePerDay", "maxNewConfigsPerDay", "maxSpotScoreConfigs"} & set(body)
    assert body["apiRateLimit"] == api.API_RATE_LIMIT


class ProbeTable:
    """Probe table with the cooldown lock condition the API uses."""
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        key = (Item["region"], Item["sk"])
        cur = self.items.get(key)
        if ConditionExpression:
            assert ConditionExpression == "attribute_not_exists(sk) OR lockedUntil < :now"
            if cur is not None and not cur["lockedUntil"] < ExpressionAttributeValues[":now"]:
                from botocore.exceptions import ClientError
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = Item

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        """The daily probe counter: take (ADD 1 below the limit) and refund (ADD -1 above zero)."""
        from botocore.exceptions import ClientError
        assert ExpressionAttributeNames == {"#c": "count"}
        key = (Key["region"], Key["sk"])
        cur = self.items.get(key)
        count = None if cur is None else cur.get("count")
        vals = ExpressionAttributeValues
        if UpdateExpression == "ADD #c :one SET expires = :exp":
            assert ConditionExpression == "attribute_not_exists(#c) OR #c < :limit"
            ok = count is None or count < vals[":limit"]
            new = {"count": (count or 0) + vals[":one"], "expires": vals[":exp"]}
        else:
            assert (UpdateExpression, ConditionExpression) == ("ADD #c :neg", "#c > :zero")
            ok = count is not None and count > vals[":zero"]
            new = {"count": (count or 0) + vals[":neg"]}
        if not ok:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.items[key] = {**(cur or {"region": Key["region"], "sk": Key["sk"]}), **new}

    def query(self, KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, Limit=None, **_):
        """The counter read (one key); history reads are faked per test (_recent_probes)."""
        assert KeyConditionExpression == "#r = :r AND sk = :s"
        item = self.items.get((ExpressionAttributeValues[":r"], ExpressionAttributeValues[":s"]))
        return {"Items": [item] if item else []}

    def records(self):
        return [v for (r, sk), v in self.items.items() if not sk.startswith("#") and not r.startswith("#")]

    def count(self, user, day):
        return self.items.get((f"#probe-count#{user}", day), {}).get("count")

    def all_count(self, day):
        return self.items.get((f"#probes#{day}", "count"), {}).get("count")


PROBE = {"region": "us-east-1", "instanceType": "p5.48xlarge", "mode": "spot"}
LOCK = ("us-east-1", "#lock#p5.48xlarge#spot")


def outcome(result="no-capacity", code="InsufficientInstanceCapacity", az=None, itype=None):
    """What run_probe returns for one test launch."""
    obtained = {"az": az, "instanceType": itype} if result == "launched" else None
    return {"result": result, "errorCode": code, "obtained": obtained, "instanceIds": ["i-1"] if obtained else [],
            "errors": [] if obtained else [{"code": code, "az": "us-east-1a", "instanceType": "p5.48xlarge"}],
            "sizes": ["p5.48xlarge"], "skipped": []}


def test_probe_uses_cooldown_and_records(monkeypatch):
    t = ProbeTable()
    monkeypatch.setattr(api, "table", lambda: t)
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [])
    monkeypatch.setattr(api, "run_probe", lambda *a: outcome())
    r = json.loads(api.handler(event("POST", "/api/probe", body=PROBE), None)["body"])
    assert r["result"] == "no-capacity" and not r["cached"]
    saved = t.records()
    assert saved[0]["user"] == "alice" and saved[0]["errorCode"] == "InsufficientInstanceCapacity"
    assert (saved[0]["target"], saved[0]["mode"], saved[0]["type"]) == ("p5.48xlarge", "spot", "p5.48xlarge")
    assert "az" not in saved[0]
    lock = t.items[LOCK]
    assert lock["lockedUntil"] > api._epoch() + 14 * 60

    monkeypatch.setattr(api, "_recent_probes", lambda *_: [saved[0]])
    monkeypatch.setattr(api, "run_probe", lambda *a: pytest.fail("must not launch during cooldown"))
    r = json.loads(api.handler(event("POST", "/api/probe", body=PROBE), None)["body"])
    assert r["cached"] is True


def test_parallel_probes_launch_once(monkeypatch):
    # The history read misses for every request (none has written yet); the lock lets one launch.
    t = ProbeTable()
    launches = []
    monkeypatch.setattr(api, "table", lambda: t)
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [])
    monkeypatch.setattr(api, "run_probe", lambda *a: launches.append(a) or outcome("launched", None, "us-east-1a", "p5.48xlarge"))
    bodies = [json.loads(api.handler(event("POST", "/api/probe", body=PROBE), None)["body"]) for _ in range(10)]
    assert len(launches) == 1
    assert [b["result"] for b in bodies].count("in-progress") == 9
    # The other market has its own slot.
    api.handler(event("POST", "/api/probe", body={**PROBE, "mode": "on-demand"}), None)
    assert len(launches) == 2


def test_expired_lock_and_failed_probe_free_the_slot(monkeypatch):
    t = ProbeTable()
    monkeypatch.setattr(api, "table", lambda: t)
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [])
    t.items[LOCK] = {"lockedUntil": api._epoch() - 1}

    def boom(*_):
        raise api.HttpError(400, "no default VPC", "no-default-vpc")
    monkeypatch.setattr(api, "run_probe", boom)
    assert api.handler(event("POST", "/api/probe", body=PROBE), None)["statusCode"] == 400
    assert t.items[LOCK]["lockedUntil"] == 0
    monkeypatch.setattr(api, "run_probe", lambda *a: outcome("launched", None, "us-east-1a", "p5.48xlarge"))
    assert json.loads(api.handler(event("POST", "/api/probe", body=PROBE), None)["body"])["result"] == "launched"


@pytest.mark.parametrize("days,status", [(1, 200), (14, 200), (21, 200), (182, 200), (15, 400), (20, 400), (27, 400), (183, 400)])
def test_capacity_block_days_follow_ec2_steps(monkeypatch, days, status):
    class Pager:
        def paginate(self, **kw):
            return [{"CapacityBlockOfferings": []}]

    class C:
        def get_paginator(self, name):
            return Pager()

    monkeypatch.setattr(api, "client", lambda *_: C())
    q = {"region": "us-east-1", "instanceType": "p5.48xlarge", "days": str(days)}
    assert api.handler(event("GET", "/api/capacity-blocks", q), None)["statusCode"] == status


def test_iso_is_fixed_width_and_ordered():
    from datetime import datetime, timezone
    a = api.iso(datetime(2030, 1, 1, 10, 0, 0, tzinfo=timezone.utc))
    b = api.iso(datetime(2030, 1, 1, 10, 0, 0, 500, tzinfo=timezone.utc))
    assert len(a) == len(b) and a < b and a.endswith("Z")


def test_spot_probe_refused_when_disabled(monkeypatch):
    monkeypatch.setattr(api, "ENABLE_SPOT_PROBE", False)
    monkeypatch.setattr(api, "run_probe", lambda *a: pytest.fail("a refused Spot launch must not run"))
    body = {"region": "us-east-1", "instanceType": "p5.48xlarge", "mode": "spot"}
    r = api.handler(event("POST", "/api/probe", body=body), None)
    assert r["statusCode"] == 403


def test_config_reports_spot_probe_switch(monkeypatch):
    monkeypatch.setattr(api, "ENABLE_SPOT_PROBE", False)
    r = api.handler(event("GET", "/api/config"), None)
    assert json.loads(r["body"])["spotProbeEnabled"] is False


# ------------------------------------------------------------- error codes
def body_of(r):
    return r["statusCode"], json.loads(r["body"])


@pytest.mark.parametrize("ev,status,code,params", [
    (event("GET", "/api/nope"), 404, "not-found", None),
    (event("GET", "/api/quotas", {"region": "eu-west-1"}), 400, "region-not-enabled", None),
    (event("GET", "/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p5.48xlarge", "days": "x"}), 400, "bad-parameter", {"name": "days"}),
    (event("GET", "/api/capacity-blocks", {"region": "us-east-1", "instanceType": "p5.48xlarge", "count": "99"}), 400, "out-of-range",
     {"name": "count", "low": 1, "high": 64}),
    (event("GET", "/api/instance-type-zones", {"region": "us-east-1", "instanceType": "gr6.xlarge"}), 400, "type-not-enabled", None),
    (event("GET", "/api/instance-type-zones", {"region": "us-west-2", "instanceType": "p5.48xlarge"}), 400, "type-not-offered",
     {"type": "p5.48xlarge", "region": "us-west-2"}),
    (event("POST", "/api/probe", body={"region": "us-east-1", "instanceType": "p5.48xlarge", "market": "x"}), 400,
     "bad-parameter", {"name": "market"}),
    (event("POST", "/api/probe", body={"region": "us-east-1", "group": "p5/8", "market": "spot"}), 400,
     "unknown-group", {"group": "p5/8"}),
])
def test_common_errors_carry_a_code_and_keep_the_message(ev, status, code, params):
    got_status, body = body_of(api.handler(ev, None))
    assert got_status == status and body["code"] == code and body["error"]
    if params is not None:
        assert body["params"] == params
    elif code != "type-not-enabled":
        assert "params" not in body


PROBE_BODY_FOR_TOKEN = {"region": "us-east-1", "instanceType": "p5.48xlarge", "mode": "spot"}


@pytest.mark.parametrize("method,path,body", [("GET", "/api/config", None), ("POST", "/api/probe", PROBE_BODY_FOR_TOKEN),
                                              ("POST", "/api/subscriptions", {}), ("DELETE", "/api/subscriptions", None)])
def test_an_access_token_is_refused_before_any_route(monkeypatch, method, path, body):
    """The JWT authorizer also lets an access token of the app client through; it carries no email
    or user name, so the API answers 401 before a route runs."""
    called = []
    monkeypatch.setattr(api, "ROUTES", {k: (lambda q, c, k=k: called.append(k)) for k in api.ROUTES})
    monkeypatch.setattr(api, "BODY_ROUTES", {k: (lambda q, c, b, k=k: called.append(k)) for k in api.BODY_ROUTES})
    ev = event(method, path, body=body)
    ev["requestContext"]["authorizer"]["jwt"]["claims"]["token_use"] = "access"
    status, answer = body_of(api.handler(ev, None))
    assert (status, answer["code"]) == (401, "no-identity") and called == []
    ev["requestContext"]["authorizer"]["jwt"]["claims"]["token_use"] = "id"
    assert api.handler(ev, None)["statusCode"] == 200 and len(called) == 1


def test_a_token_without_token_use_is_refused():
    """Fail closed: a token that does not say it is an ID token is not treated as one."""
    ev = event("GET", "/api/config")
    del ev["requestContext"]["authorizer"]["jwt"]["claims"]["token_use"]
    status, answer = body_of(api.handler(ev, None))
    assert (status, answer["code"]) == (401, "no-identity")


def test_bad_json_body_has_a_code():
    ev = event("POST", "/api/probe")
    ev["body"] = "{"
    assert body_of(api.handler(ev, None)) == (400, {"error": "body must be JSON", "code": "bad-body"})


def test_unexpected_failure_has_a_code(monkeypatch):
    monkeypatch.setattr(api, "ROUTES", {("GET", "/api/config"): lambda q, c: 1 / 0})
    assert body_of(api.handler(event("GET", "/api/config"), None)) == (500, {"error": "internal error", "code": "internal"})


def test_disabled_probe_has_a_code(monkeypatch):
    monkeypatch.setattr(api, "ENABLE_PROBE", False)
    body = {"region": "us-east-1", "instanceType": "p5.48xlarge", "mode": "spot"}
    assert body_of(api.handler(event("POST", "/api/probe", body=body), None))[1]["code"] == "probes-disabled"


# ------------------------------------------------------- daily probe cap
from datetime import datetime, timedelta, timezone  # noqa: E402


def probe_event(sub="sub-alice", **body):
    ev = event("POST", "/api/probe", body={**PROBE, **body})
    ev["requestContext"]["authorizer"]["jwt"]["claims"]["sub"] = sub
    return ev


@pytest.fixture
def capped(monkeypatch):
    """A probe table, no history, a fixed clock, and a launch that always reaches EC2."""
    t = ProbeTable()
    clock = [datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)]
    launches = []
    monkeypatch.setattr(api, "table", lambda: t)
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [])
    monkeypatch.setattr(api, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(api, "MAX_PROBES_PER_USER_PER_DAY", 2)
    monkeypatch.setattr(api, "COOLDOWN_MINUTES", 15)
    monkeypatch.setattr(api, "run_probe", lambda *a: launches.append(a) or outcome())

    def free_slots():
        # Drop the cooldown locks between calls.
        for key in [k for k in t.items if k[1].startswith("#lock#")]:
            t.items[key]["lockedUntil"] = 0
    return t, clock, launches, free_slots


def body_of_response(r):
    return r["statusCode"], json.loads(r["body"])


def test_daily_cap_counts_real_launches_then_refuses(capped):
    t, _, launches, free = capped
    assert body_of_response(api.handler(probe_event(), None))[0] == 200
    assert t.count("sub-alice", "2030-01-01") == 1
    item = t.items[("#probe-count#sub-alice", "2030-01-01")]
    # The end of 2030-01-01 UTC plus two days.
    assert item["expires"] == int(datetime(2030, 1, 4, tzinfo=timezone.utc).timestamp())
    free()
    assert body_of_response(api.handler(probe_event(mode="on-demand"), None))[0] == 200
    free()
    status, body = body_of_response(api.handler(probe_event(), None))
    assert status == 429 and body["code"] == "probe-daily-limit"
    # The next 00:00 UTC after the request: the counter is keyed by the UTC calendar day.
    assert body["params"] == {"limit": 2, "resetAt": "2030-01-02T00:00:00Z"}
    assert body["error"].startswith("You reached your limit of 2 test launches for today:")
    assert "00:00 UTC" in body["error"] and "maxTestLaunchesPerUserPerDay" in body["error"]
    assert len(launches) == 2 and t.count("sub-alice", "2030-01-01") == 2
    # The deployment-wide counter counts the two real launches only (the refused one never took it).
    assert t.all_count("2030-01-01") == 2
    # The refused request frees its cooldown slot, so the target is not blocked for 15 minutes.
    assert t.items[LOCK]["lockedUntil"] == 0


def test_cached_results_and_refused_requests_do_not_count(capped, monkeypatch):
    t, _, launches, _ = capped
    saved = {"sk": "2030-01-01T09:59:00.000000Z#p5.48xlarge#spot", "az": "us-east-1a", "target": "p5.48xlarge",
             "type": "p5.48xlarge", "mode": "spot", "result": "launched", "sizes": ["p5.48xlarge"]}
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [saved])
    body = body_of_response(api.handler(probe_event(), None))[1]
    assert body["cached"] is True and body["obtained"] == {"az": "us-east-1a", "instanceType": "p5.48xlarge"}
    # Refused before any launch: a market that does not exist.
    assert api.handler(probe_event(mode="dedicated"), None)["statusCode"] == 400
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [])
    # Another request holds the cooldown slot: "in-progress", not counted.
    t.items[LOCK] = {"lockedUntil": api._epoch() + 600}
    assert body_of_response(api.handler(probe_event(), None))[1]["result"] == "in-progress"
    assert launches == [] and t.count("sub-alice", "2030-01-01") is None and t.all_count("2030-01-01") is None


def test_daily_cap_zero_turns_it_off(capped, monkeypatch):
    t, _, launches, free = capped
    monkeypatch.setattr(api, "MAX_PROBES_PER_USER_PER_DAY", 0)
    for _ in range(5):
        free()
        assert api.handler(probe_event(), None)["statusCode"] == 200
    assert len(launches) == 5
    assert not [k for k in t.items if k[0].startswith("#probe-count#")]


def test_failed_probe_before_launch_refunds_the_count(capped, monkeypatch):
    t, _, _, free = capped

    def no_group(*_):
        raise api.HttpError(409, "conflict", "probe-sg-conflict")
    monkeypatch.setattr(api, "run_probe", no_group)
    status, body = body_of_response(api.handler(probe_event(), None))
    assert status == 409 and body["code"] == "probe-sg-conflict"
    assert t.count("sub-alice", "2030-01-01") == 0 and t.all_count("2030-01-01") == 0
    assert t.items[LOCK]["lockedUntil"] == 0

    def aws_down(*_):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "RequestLimitExceeded"}}, "DescribeSubnets")
    monkeypatch.setattr(api, "run_probe", aws_down)
    free()
    assert api.handler(probe_event(), None)["statusCode"] == 502
    assert t.count("sub-alice", "2030-01-01") == 0 and t.all_count("2030-01-01") == 0
    # A launch EC2 answered (even with an error) counts.
    monkeypatch.setattr(api, "run_probe", lambda *a: outcome())
    free()
    assert api.handler(probe_event(), None)["statusCode"] == 200
    assert t.count("sub-alice", "2030-01-01") == 1 and t.all_count("2030-01-01") == 1


def test_daily_cap_is_per_user(capped, monkeypatch):
    t, _, launches, free = capped
    monkeypatch.setattr(api, "MAX_PROBES_PER_USER_PER_DAY", 1)
    assert api.handler(probe_event(), None)["statusCode"] == 200
    free()
    assert api.handler(probe_event(), None)["statusCode"] == 429
    free()
    assert api.handler(probe_event(sub="sub-bob"), None)["statusCode"] == 200
    assert (t.count("sub-alice", "2030-01-01"), t.count("sub-bob", "2030-01-01")) == (1, 1)
    # The identity comes from the token; without one the probe is refused before any write.
    free()
    assert api.handler(probe_event(sub=""), None)["statusCode"] == 403
    assert len(launches) == 2


def test_daily_cap_rolls_over_at_utc_midnight(capped, monkeypatch):
    t, clock, _, free = capped
    monkeypatch.setattr(api, "MAX_PROBES_PER_USER_PER_DAY", 1)
    clock[0] = datetime(2030, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
    assert api.handler(probe_event(), None)["statusCode"] == 200
    free()
    assert api.handler(probe_event(), None)["statusCode"] == 429
    assert body_of_response(api.handler(probe_event(), None))[1]["params"]["resetAt"] == "2030-01-02T00:00:00Z"
    clock[0] = datetime(2030, 1, 2, 0, 0, 1, tzinfo=timezone.utc)
    free()
    assert api.handler(probe_event(), None)["statusCode"] == 200
    assert (t.count("sub-alice", "2030-01-01"), t.count("sub-alice", "2030-01-02")) == (1, 1)


def test_config_reports_the_daily_probe_cap(monkeypatch):
    monkeypatch.setattr(api, "MAX_PROBES_PER_USER_PER_DAY", 7)
    assert json.loads(api.handler(event("GET", "/api/config"), None)["body"])["maxProbesPerUserPerDay"] == 7


@pytest.mark.parametrize("now,reset", [
    (datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc), "2030-01-02T00:00:00Z"),
    (datetime(2030, 12, 31, 23, 59, 59, tzinfo=timezone.utc), "2031-01-01T00:00:00Z"),
    # A clock in another zone still resets at 00:00 UTC: 2030-01-01 20:00 at UTC-5 is 01:00 UTC on 01-02.
    (datetime(2030, 1, 1, 20, 0, tzinfo=timezone(timedelta(hours=-5))), "2030-01-03T00:00:00Z"),
])
def test_daily_reset_is_the_next_utc_midnight(now, reset):
    assert api._daily_reset(now) == reset
    day = api._probe_day(now)[0]  # the counter's day key and the reset time agree
    assert (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat() == reset[:10]
