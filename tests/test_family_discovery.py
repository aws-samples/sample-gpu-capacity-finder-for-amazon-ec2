"""The tracked-family rule (families.py: every G and P family by the naming rule), the Spot score
set (spotScoreFamilies), accelerator family discovery in the offering refresh, and per-Region
Capacity Block type detection, against fakes and botocore Stubbers."""
import fnmatch
import json
import os

os.environ.update(SPOT_SCORE_FAMILIES="g5.*", TABLE_NAME="t")

import alerts  # noqa: E402
import api  # noqa: E402
import boto3  # noqa: E402
import capacity_blocks as cb  # noqa: E402
import families  # noqa: E402
import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

# The deployment in this module scores one family.
SCORED = ["g5.*"]


class Table:
    """A dict-backed cache table: get_item, put_item (optionally failing), and the batched reads'
    one-by-one fallback (no meta.client)."""

    def __init__(self, items=None, fail_put=False):
        self.items = dict(items or {})
        self.puts = []
        self.fail_put = fail_put

    def get_item(self, Key, **_kw):
        item = self.items.get(Key["cacheKey"])
        return {"Item": item} if item else {}

    def put_item(self, Item, **_kw):
        if self.fail_put:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")
        self.items[Item["cacheKey"]] = Item
        self.puts.append(Item["cacheKey"])


def ec2_stub(region="us-east-1"):
    c = boto3.client("ec2", region_name=region, aws_access_key_id="x", aws_secret_access_key="x")
    return c, Stubber(c)


# --------------------------------------------------------- the tracked rule
# Every family whose letters are "g" or "p" followed by the generation digit. A name whose letters
# are another group (gr6, trn2, inf2, vt1, dl1) never qualifies.
TRACKED = ["g4dn", "g5", "g5g", "g6", "g6e", "g6f", "g7", "g7e", "g10", "p3dn", "p4d", "p4de", "p5", "p5en",
           "p6-b200", "p6e-gb200", "p7"]
UNTRACKED = ["gr6", "trn2", "inf2", "vt1", "dl1", "m5", "pz1", "g", "p"]


@pytest.mark.parametrize("family", TRACKED)
def test_the_rule_tracks_every_g_and_p_family(family):
    assert families.tracked(family) is True
    assert families.tracked(f"{family}.xlarge") is True


@pytest.mark.parametrize("family", UNTRACKED)
def test_the_rule_leaves_other_letter_groups_out(family):
    assert families.tracked(family) is False
    assert families.tracked(f"{family}.xlarge") is False


@pytest.mark.parametrize("itype,expected", [
    # Only the listed families are scored, and only tracked types.
    ("g5.xlarge", True), ("g5g.xlarge", False), ("g6e.xlarge", False), ("gr6.4xlarge", False),
])
def test_the_spot_score_set_is_the_tracked_types_of_the_patterns(itype, expected):
    assert families.scored(itype, SCORED) is expected


def test_the_default_spot_score_set():
    patterns = families.score_patterns_or_default([""])
    assert families.names(patterns) == ["g6e", "g7", "g7e", "p5", "p5e", "p5en", "p6-b200", "p6-b300"]
    assert families.scored("g7.xlarge", patterns) and not families.scored("g7e.xlarge", ["g7.*"])


@pytest.mark.parametrize("itype,pattern,expected", [
    ("g5.xlarge", "g5.*", True), ("g5g.xlarge", "g5*", True), ("p5en.48xlarge", "p5.*", False),
    ("g7e.xlarge", "g7.*", False), ("g7e.xlarge", "g7*", True), ("p6-b200.48xlarge", "p6-b200.*", True),
])
def test_patterns_match_like_fnmatch(itype, pattern, expected):
    assert families.pattern_matches(itype, pattern) is expected


def test_the_filter_and_iam_values_cover_the_rule():
    # An EC2 filter and an IAM StringLike condition cannot express "a digit follows", so both get
    # the coarse wildcards; every caller narrows the answer with the rule itself.
    assert families.WILDCARDS == ("g*", "p*")
    assert families.filter_values() == ["g*", "p*"]
    for family in TRACKED:
        assert any(fnmatch.fnmatchcase(f"{family}.xlarge", p) for p in families.filter_values()), family


def test_api_and_alerts_use_the_one_rule(monkeypatch):
    monkeypatch.setattr(api, "SPOT_SCORE_FAMILIES", SCORED)
    for itype in ("p3dn.24xlarge", "p4de.24xlarge", "p5en.48xlarge", "p7.48xlarge", "g4dn.xlarge", "g6e.xlarge",
                  "g7e.8xlarge", "g5.xlarge"):
        assert api.matches_families(itype), itype
    for itype in ("trn2.48xlarge", "inf2.xlarge", "pz1.large", "gr6.4xlarge"):
        assert not api.matches_families(itype), itype
    assert api.scored("g5.xlarge") and not api.scored("g6e.xlarge")
    # A Spot score alert needs a scored type; the page set alone is not enough.
    item = {"region": "us-east-1", "signal": "spot-score", "threshold": 5}
    assert alerts.is_valid({**item, "instanceType": "g5.xlarge"}, ["us-east-1"], SCORED)
    assert not alerts.is_valid({**item, "instanceType": "p4de.24xlarge"}, ["us-east-1"], SCORED)


def test_an_old_cache_item_built_for_other_patterns_is_not_read(monkeypatch):
    # The deploy that introduced the rule changes the tag, so every stored item is fetched again.
    assert api.tracked_tag() == "gpu:g*,p*"
    old = {"cacheKey": "offerings#us-east-1", "data": "{}", "zones": "{}", "patterns": "g6+,p4+", "expires": 2_000_000_100}
    assert api._parse_offering_item(old, 2_000_000_000) is None


# -------------------------------------------------- accelerator family discovery
def test_accelerator_families_follow_the_naming_rule():
    types = ["g6e.xlarge", "gr6.4xlarge", "p5.48xlarge", "p5.4xlarge", "trn1n.32xlarge", "inf2.xlarge", "m5.large", "vt1.3xlarge"]
    assert api.accelerator_families(types) == ["g6e", "inf2", "p5", "trn1n"]


def test_one_region_call_serves_the_tracked_types_and_the_accelerator_families(monkeypatch):
    monkeypatch.setattr(api, "_clients", {})
    c, s = ec2_stub()
    api._clients[("ec2", "us-east-1")] = c
    # The Region-level call also asks for Trn and Inf, which the rule leaves out: the quota tips
    # name those groups' families too.
    wide = ["p*", "g*", "trn*", "inf*"]
    assert api.region_offering_patterns() == wide
    s.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [
        {"InstanceType": t, "LocationType": "region", "Location": "us-east-1"}
        for t in ("p5.48xlarge", "p4de.24xlarge", "p3dn.24xlarge", "g5.xlarge", "gr6.4xlarge", "g7e.2xlarge",
                  "trn1.32xlarge", "inf2.xlarge")]},
        {"LocationType": "region", "Filters": [{"Name": "instance-type", "Values": wide}]})
    # The zone call asks for the rule's wildcards, then the rule narrows the answer.
    s.add_response("describe_instance_type_offerings", {"InstanceTypeOfferings": [
        {"InstanceType": "p5.48xlarge", "LocationType": "availability-zone", "Location": "us-east-1a"},
        {"InstanceType": "p3dn.24xlarge", "LocationType": "availability-zone", "Location": "us-east-1a"},
        {"InstanceType": "g5.xlarge", "LocationType": "availability-zone", "Location": "us-east-1a"}]},
        {"LocationType": "availability-zone", "Filters": [{"Name": "instance-type", "Values": ["g*", "p*"]}]})
    s.activate()
    got = api._fetch_region_offerings("us-east-1")
    s.assert_no_pending_responses()
    assert got["families"] == {"g5": ["g5.xlarge"], "g7e": ["g7e.2xlarge"], "p3dn": ["p3dn.24xlarge"],
                               "p4de": ["p4de.24xlarge"], "p5": ["p5.48xlarge"]}
    assert got["zones"] == {"g5.xlarge": ["us-east-1a"], "p3dn.24xlarge": ["us-east-1a"], "p5.48xlarge": ["us-east-1a"]}
    assert got["accelerators"] == ["g5", "g7e", "inf2", "p3dn", "p4de", "p5", "trn1"]


def item(region, offered, accelerators=None):
    it = {"cacheKey": f"offerings#{region}", "data": json.dumps(offered), "zones": "{}", "fetchedAt": "t0",
          "patterns": families.cache_tag(), "expires": 2_000_000_100}
    if accelerators is not None:
        it["accelerators"] = accelerators if isinstance(accelerators, str) else json.dumps(accelerators)
    return it


@pytest.mark.parametrize("raw,expected", [
    (None, None), ('["g5", "p5"]', ["g5", "p5"]), ("not json", None), ('{"g5": 1}', None), ("[1]", None), (5, None),
])
def test_stored_accelerators_parse_or_are_unknown(raw, expected):
    assert api._parse_accelerators(raw) == expected


def test_matrix_lists_the_accelerator_families_and_old_items_add_none(monkeypatch):
    monkeypatch.setattr(api, "_epoch", lambda: 2_000_000_000)
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": ["us-east-1", "us-west-2", "ap-east-1"], "fetchedAt": None})
    t = Table({"offerings#us-east-1": item("us-east-1", {"p5": ["p5.48xlarge"]}, ["g5", "g6e", "p5", "trn1"]),
               # Written before the field existed: still served, adds no accelerator family.
               "offerings#us-west-2": item("us-west-2", {"p4de": ["p4de.24xlarge"]}),
               "offerings#ap-east-1": item("ap-east-1", {}, ["g4dn", "g5", "inf1"])})
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": "/api/offering-matrix",
                     "queryStringParameters": None}, None)
    body = json.loads(r["body"])
    assert r["statusCode"] == 200 and body["fromCache"] is True and t.puts == []
    assert body["families"] == ["p4de", "p5"]
    assert body["acceleratorFamilies"] == ["g4dn", "g5", "g6e", "inf1", "p5", "trn1"]


def test_stored_item_keeps_the_accelerators_and_a_live_entry_without_them_writes_none(monkeypatch):
    t = Table()
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    api._write_cached("us-east-1", {"families": {}, "zones": {}, "accelerators": ["g5", "p5"], "fetchedAt": "t1"}, 100)
    assert json.loads(t.items["offerings#us-east-1"]["accelerators"]) == ["g5", "p5"]
    api._write_cached("us-west-2", {"families": {}, "zones": {}, "fetchedAt": "t1"}, 100)
    assert "accelerators" not in t.items["offerings#us-west-2"]


# ------------------------------------------------ Capacity Block type detection
def test_is_supported_uses_the_detected_types_else_the_family_fallback():
    assert "p4de" in cb.FAMILIES
    assert cb.is_supported("trn1.2xlarge", None) and cb.is_supported("p4de.24xlarge", None)
    assert not cb.is_supported("g6e.xlarge", None)
    # Detected lists are per type and per Region: trn1.2xlarge and trn2 in us-east-1 are not supported.
    detected = ["p5.48xlarge", "trn1.32xlarge"]
    assert cb.is_supported("trn1.32xlarge", detected) and not cb.is_supported("trn1.2xlarge", detected)
    assert not cb.is_supported("trn2.48xlarge", detected) and not cb.is_supported(None, detected)
    assert not cb.is_supported("p5.48xlarge", [])


@pytest.mark.parametrize("stored,now,expected", [
    (None, 100, (None, False)),
    ({"types": '["p5.48xlarge"]', "refreshAfter": 200, "expires": 900}, 100, (["p5.48xlarge"], True)),
    ({"types": '["p5.48xlarge"]', "refreshAfter": 200, "expires": 900}, 200, (["p5.48xlarge"], False)),
    ({"types": '["p5.48xlarge"]', "refreshAfter": 200, "expires": 900}, 900, (None, False)),
    ({"types": "[]", "refreshAfter": 200, "expires": 900}, 100, ([], True)),
    ({"types": "nope", "refreshAfter": 200, "expires": 900}, 100, (None, False)),
    ({"types": '{"a": 1}', "refreshAfter": 200, "expires": 900}, 100, (None, False)),
    ({"refreshAfter": 200, "expires": 900}, 100, (None, False)),
])
def test_parse_support(stored, now, expected):
    assert cb.parse_support(stored, now) == expected


def test_detection_paginates_with_the_usage_class_filter():
    c, s = ec2_stub("us-east-2")
    params = {"Filters": [{"Name": "supported-usage-class", "Values": ["capacity-block"]}]}
    s.add_response("describe_instance_types", {"InstanceTypes": [{"InstanceType": "trn2.48xlarge"}], "NextToken": "n"}, params)
    s.add_response("describe_instance_types", {"InstanceTypes": [{"InstanceType": "p5.48xlarge"}]}, {**params, "NextToken": "n"})
    s.activate()
    assert cb.detect_types(c) == ["p5.48xlarge", "trn2.48xlarge"]
    s.assert_no_pending_responses()


def _detecting(types=None, error=None):
    calls = []

    def ec2_for(region):
        calls.append(region)

        class Fake:
            def get_paginator(self, _op):
                class P:
                    def paginate(self, Filters):
                        assert Filters == cb.SUPPORT_FILTER
                        if error:
                            raise error
                        return [{"InstanceTypes": [{"InstanceType": t} for t in types]}]
                return P()
        return Fake()
    return ec2_for, calls


def test_fresh_item_answers_without_an_ec2_call():
    t = Table({"cbtypes#us-east-1": {"cacheKey": "cbtypes#us-east-1", "types": '["p5.48xlarge"]', "refreshAfter": 200,
                                     "expires": 900}})
    ec2_for, calls = _detecting(["x"])
    assert cb.supported_types(t, ec2_for, "us-east-1", 100, "t1") == ["p5.48xlarge"] and calls == [] and t.puts == []


def test_missing_or_due_item_is_detected_once_and_stored_for_a_day():
    t = Table()
    ec2_for, calls = _detecting(["trn2.48xlarge", "p5.48xlarge"])
    assert cb.supported_types(t, ec2_for, "us-east-2", 1000, "t1") == ["p5.48xlarge", "trn2.48xlarge"]
    assert calls == ["us-east-2"]
    stored = t.items["cbtypes#us-east-2"]
    assert stored == {"cacheKey": "cbtypes#us-east-2", "types": '["p5.48xlarge", "trn2.48xlarge"]', "fetchedAt": "t1",
                      "refreshAfter": 1000 + 24 * 3600, "expires": 1000 + 7 * 24 * 3600}
    # The stored answer serves the rest of the day.
    assert cb.supported_types(t, ec2_for, "us-east-2", 2000, "t2") == ["p5.48xlarge", "trn2.48xlarge"] and calls == ["us-east-2"]
    # A Region without any Capacity Block type stores an empty list, which is an answer, not unknown.
    ec2_for, _ = _detecting([])
    assert cb.supported_types(t, ec2_for, "ap-east-1", 1000, "t1") == []
    assert cb.parse_support(t.items["cbtypes#ap-east-1"], 1001) == ([], True)


def test_failed_detection_keeps_the_stale_list_or_falls_back():
    err = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeInstanceTypes")
    ec2_for, _ = _detecting(error=err)
    t = Table({"cbtypes#us-east-1": {"cacheKey": "cbtypes#us-east-1", "types": '["p5.48xlarge"]', "refreshAfter": 200,
                                     "expires": 900}})
    assert cb.supported_types(t, ec2_for, "us-east-1", 500, "t1") == ["p5.48xlarge"] and t.puts == []
    assert cb.supported_types(Table(), ec2_for, "us-east-1", 500, "t1") is None


def test_a_failed_cache_write_still_answers():
    ec2_for, _ = _detecting(["p5.48xlarge"])
    assert cb.supported_types(Table(fail_put=True), ec2_for, "us-east-1", 500, "t1") == ["p5.48xlarge"]


def test_api_detection_is_kept_in_memory_for_a_minute(live_cb_types, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(api, "_now", lambda: clock[0])
    asked = []
    monkeypatch.setattr(cb, "supported_types", lambda table, ec2_for, region, now, stamp: asked.append(region) or ["p5.48xlarge"])
    assert api.cb_types("us-east-1") == ["p5.48xlarge"] and api.cb_types("us-east-1") == ["p5.48xlarge"]
    assert asked == ["us-east-1"]
    clock[0] += api.MEMORY_TTL_SECONDS
    api.cb_types("us-east-1")
    assert asked == ["us-east-1", "us-east-1"]


def test_api_detection_reads_the_cache_item_and_calls_ec2_in_that_region(live_cb_types, monkeypatch):
    t = Table()
    monkeypatch.setattr(api, "offering_cache_table", lambda: t)
    monkeypatch.setattr(api, "_clients", {})
    c, s = ec2_stub("us-east-2")
    api._clients[("ec2", "us-east-2")] = c
    s.add_response("describe_instance_types", {"InstanceTypes": [{"InstanceType": "trn2.48xlarge"}]},
                   {"Filters": [{"Name": "supported-usage-class", "Values": ["capacity-block"]}]})
    s.activate()
    assert api.cb_types("us-east-2") == ["trn2.48xlarge"] and "cbtypes#us-east-2" in t.items
    s.assert_no_pending_responses()


def test_capacity_block_type_list_follows_the_region(monkeypatch):
    offered = {"families": {"p5": ["p5.48xlarge", "p5.4xlarge"]}, "zones": {}, "fetchedAt": "t0", "fromCache": True,
               # The offered types of the Capacity Block fallback families, whatever the page tracks.
               "blockTypes": ["p5.4xlarge", "p5.48xlarge", "trn1.2xlarge", "trn1.32xlarge", "trn2.48xlarge"]}
    monkeypatch.setattr(api, "_offering_entry", lambda region: offered)
    detected = {"us-east-1": ["p5.48xlarge", "trn1.32xlarge"], "us-west-2": ["p5.48xlarge", "trn1.32xlarge", "trn2.48xlarge"],
                "ap-east-1": None}
    monkeypatch.setattr(api, "cb_types", lambda region: detected[region])
    monkeypatch.setattr(api, "_region_info", lambda: {"regions": ["us-east-1", "us-west-2", "ap-east-1"], "fetchedAt": None})

    def types(region):
        r = api.handler({"requestContext": {"http": {"method": "GET"}, "authorizer": {"jwt": {"claims": {"token_use": "id"}}}}, "rawPath": "/api/instance-types",
                         "queryStringParameters": {"region": region, "capacityBlocks": "true"}}, None)
        return json.loads(r["body"])["instanceTypes"]
    assert types("us-east-1") == ["p5.48xlarge", "trn1.32xlarge"]
    assert types("us-west-2") == ["p5.48xlarge", "trn1.32xlarge", "trn2.48xlarge"]
    # Unknown: the family fallback (every p5 and trn size).
    assert types("ap-east-1") == ["p5.4xlarge", "p5.48xlarge", "trn1.2xlarge", "trn1.32xlarge", "trn2.48xlarge"]
