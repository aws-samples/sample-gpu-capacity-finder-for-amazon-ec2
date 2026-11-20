"""Test launch with an EC2 Fleet of type instant (fleet_launch.py and the probe routes of api.py).

Request shapes run against botocore Stubbers (the EC2 service model checks every parameter name and
type); the guards and counters run against the fake probe table of test_api. Nothing reaches AWS."""
import base64
import itertools
import json
import os
from datetime import datetime, timedelta, timezone

os.environ.update(SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t", ENABLE_PROBE="true")

import api  # noqa: E402
import boto3  # noqa: E402
import fleet_launch  # noqa: E402
import pytest  # noqa: E402
from botocore.exceptions import (ClientError, ConnectionClosedError, ConnectTimeoutError,  # noqa: E402
                                 EndpointConnectionError, NoCredentialsError, ParamValidationError, ReadTimeoutError,
                                 SSLError)
from botocore.stub import ANY, Stubber  # noqa: E402
from test_api import ProbeTable, event  # noqa: E402

REGION = "us-east-1"
G6E4 = ["g6e.12xlarge", "g6e.24xlarge"]
SUBNETS = [("us-east-1a", "subnet-a"), ("us-east-1b", "subnet-b"), ("us-east-1c", "subnet-c")]
OFFERS = {
    "us-east-1": {"families": {"g6e": ["g6e.xlarge", *G6E4], "p5": ["p5.48xlarge"]},
                  "zones": {t: ["us-east-1a", "us-east-1b"] for t in ["g6e.xlarge", *G6E4, "p5.48xlarge"]},
                  "fetchedAt": "2033-05-18T03:00:00.000000Z", "fromCache": True},
    "us-west-2": {"families": {"g6e": ["g6e.12xlarge"]}, "zones": {"g6e.12xlarge": ["us-west-2a"]},
                  "fetchedAt": "2033-05-18T03:00:00.000000Z", "fromCache": True},
}
STATE = {"version": 1, "families": [], "dailyAt": 0, "updatedAt": 0, "offered": {},
         "gpus": {"g6e.xlarge": 1, "g6e.12xlarge": 4, "g6e.24xlarge": 4, "p5.48xlarge": 8},
         "groups": {"g6e/4": {"types": ["g6e.24xlarge", "g6e.12xlarge", "g6e.48xlarge"], "chunks": [["us-east-1"]],
                              "added": {}},
                    "p5/8": {"types": ["p5.48xlarge"], "chunks": [["us-east-1"]], "added": {}}}}
VCPUS = {"g6e.xlarge": 4, "g6e.12xlarge": 48, "g6e.24xlarge": 96, "g6e.48xlarge": 192, "p5.48xlarge": 192}
TAG = [{"Key": "gpu-capacity-finder-probe", "Value": "true"}]


def quota_rows(od_p=192.0, od_g=256.0, spot_g=256.0, spot_p=None, spot_p5=None, used=0.0):
    rows = [{"name": "Running On-Demand P instances", "category": "on-demand", "family": "p", "value": od_p, "used": used},
            {"name": "Running On-Demand G and VT instances", "category": "on-demand", "family": "g", "value": od_g,
             "used": used},
            {"name": "All G and VT Spot Instance Requests", "category": "spot", "family": "g", "value": spot_g, "used": None}]
    if spot_p is not None:
        rows.append({"name": "All P Spot Instance Requests", "category": "spot", "family": "p", "value": spot_p, "used": 0.0})
    if spot_p5 is not None:
        rows.append({"name": "All P5 Spot Instance Requests", "category": "spot", "family": "p", "value": spot_p5, "used": 0.0})
    return {"quotas": rows}


@pytest.fixture
def world(monkeypatch):
    """Offerings, stored groups, quotas and a probe table; every AWS client is a Stubber made on demand."""
    t = ProbeTable()
    clock = [datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(api, "_offering_entry", lambda region: OFFERS[region])
    monkeypatch.setattr(api, "_groups_state", lambda: STATE)
    monkeypatch.setattr(api, "_quota_rows", lambda region, live=False: quota_rows())
    monkeypatch.setattr(api, "table", lambda: t)
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [])
    monkeypatch.setattr(api, "_utcnow", lambda: clock[0])
    for name in ("_clients", "_sg_cache", "_subnet_cache", "_arch_cache", "_ami_cache"):
        monkeypatch.setattr(api, name, {})
    monkeypatch.setattr(api, "MAX_PROBES_PER_USER_PER_DAY", 50)
    monkeypatch.setattr(api, "MAX_TEST_LAUNCHES_PER_DAY", 100)
    stubs = {}
    single = []
    # CreateFleet and the unknown-outcome calls go to the one-attempt client: here the same Stubber.
    monkeypatch.setattr(api, "_single_try_ec2", lambda region, connect, read: single.append((region, connect, read))
                        or api._clients[("ec2", region)])

    def stub(service, region=REGION):
        if (service, region) not in stubs:
            c = boto3.client(service, region_name=region, aws_access_key_id="x", aws_secret_access_key="x")
            api._clients[(service, region)] = c
            stubs[(service, region)] = Stubber(c)
            stubs[(service, region)].activate()
        return stubs[(service, region)]
    yield {"table": t, "clock": clock, "stub": stub, "single": single}
    for s in stubs.values():
        s.assert_no_pending_responses()


def no_ec2(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "client", lambda service, region: calls.append((service, region)) or pytest.fail(
        f"no {service} call expected"))
    return calls


def expect_reads(stub, types=G6E4, arch="x86_64", subnets=SUBNETS, sg="sg-probe", groups=None):
    e = stub("ec2")
    e.add_response("describe_instance_types", {"InstanceTypes": [
        {"InstanceType": t, "VCpuInfo": {"DefaultVCpus": VCPUS[t]}, "ProcessorInfo": {"SupportedArchitectures": [arch]}}
        for t in types]}, {"InstanceTypes": list(types)})
    e.add_response("describe_subnets", {"Subnets": [
        {"SubnetId": s, "VpcId": "vpc-1", "AvailabilityZone": az, "State": "available"} for az, s in subnets]},
        {"Filters": [{"Name": "default-for-az", "Values": ["true"]}]})
    stub("ssm").add_response("get_parameter", {"Parameter": {"Value": f"ami-{arch}"}},
                             {"Name": api.AMI_PARAMETERS[arch]})
    e.add_response("describe_security_groups", {"SecurityGroups": groups or [{"GroupId": sg}]})
    return e


def expect_launch(e, market, sizes, fleet_answer, subnets=SUBNETS, arch="x86_64", sg="sg-probe"):
    data = {"ImageId": f"ami-{arch}",
            "NetworkInterfaces": [{"DeviceIndex": 0, "AssociatePublicIpAddress": False, "Groups": [sg],
                                   "DeleteOnTermination": True}],
            "MetadataOptions": {"HttpTokens": "required"}, "InstanceInitiatedShutdownBehavior": "terminate",
            "UserData": base64.b64encode(fleet_launch.PROBE_USER_DATA.encode()).decode()}
    if market == "on-demand":
        data["CapacityReservationSpecification"] = {"CapacityReservationPreference": "none"}
    e.add_response("create_launch_template", {"LaunchTemplate": {"LaunchTemplateId": "lt-1", "LatestVersionNumber": 1}},
                   {"LaunchTemplateName": ANY, "LaunchTemplateData": data,
                    "TagSpecifications": [{"ResourceType": "launch-template", "Tags": TAG}]})
    params = {
        "Type": "instant", "ClientToken": ANY,
        "LaunchTemplateConfigs": [{"LaunchTemplateSpecification": {"LaunchTemplateId": "lt-1", "Version": "1"},
                                   "Overrides": [{"InstanceType": t, "SubnetId": s} for t in sizes for _, s in subnets]}],
        "TargetCapacitySpecification": {"TotalTargetCapacity": 1, "DefaultTargetCapacityType": market},
        "TagSpecifications": [{"ResourceType": "instance", "Tags": [*TAG, {"Key": fleet_launch.LAUNCH_TAG, "Value": ANY}]}]
        + [{"ResourceType": r, "Tags": TAG} for r in ("volume", "network-interface", "fleet")],
        **({"SpotOptions": {"AllocationStrategy": "price-capacity-optimized", "InstanceInterruptionBehavior": "terminate"}}
           if market == "spot" else {"OnDemandOptions": {"AllocationStrategy": "lowest-price"}}),
    }
    if isinstance(fleet_answer, str):
        e.add_client_error("create_fleet", fleet_answer, http_status_code=400, expected_params=params)
    else:
        e.add_response("create_fleet", fleet_answer, params)
    return params


def launched(az="us-east-1b", itype="g6e.12xlarge", subnet="subnet-b"):
    return {"FleetId": "fleet-1", "Errors": [], "Instances": [{
        "InstanceIds": ["i-1"], "InstanceType": itype,
        "LaunchTemplateAndOverrides": {"Overrides": {"InstanceType": itype, "SubnetId": subnet, "AvailabilityZone": az}}}]}


def post(body, sub="sub-alice"):
    ev = event("POST", "/api/probe", body=body)
    ev["requestContext"]["authorizer"]["jwt"]["claims"]["sub"] = sub
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


# ------------------------------------------------------------- the launch
@pytest.mark.parametrize("market", ["on-demand", "spot"])
def test_a_group_launch_sends_every_default_subnet_times_every_offered_size(world, market):
    """g6e/4 stores three sizes; us-east-1 sells two: 2 sizes x 3 default subnets = 6 overrides.
    On-Demand: lowest-price; Spot: price-capacity-optimized. Launched -> terminated, template deleted."""
    e = expect_reads(world["stub"])
    expect_launch(e, market, G6E4, launched())
    e.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-1"]})
    e.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    status, body = post({"region": REGION, "group": "g6e/4", "market": market})
    assert status == 200, body
    assert (body["result"], body["errorCode"], body["cached"]) == ("launched", None, False)
    assert body["obtained"] == {"az": "us-east-1b", "instanceType": "g6e.12xlarge"}
    assert (body["group"], body["label"], body["instanceType"], body["market"]) == ("g6e/4", "g6e · 4 GPUs", None, market)
    assert body["sizes"] == G6E4 and body["skipped"] == [] and body["scoreSource"] == "none"
    assert body["today"] == {"day": "2030-01-01", "user": 1, "userLimit": 50, "all": 1, "allLimit": 100,
                             "resetAt": "2030-01-02T00:00:00Z"}
    record = world["table"].records()[0]
    assert (record["target"], record["group"], record["az"], record["type"], record["mode"]) == (
        "g6e/4", "g6e/4", "us-east-1b", "g6e.12xlarge", market)
    assert record["sk"].endswith("#g6e/4#" + market) and record["sizes"] == G6E4


def test_one_size_of_a_group_tries_only_that_size_and_keeps_its_group(world):
    e = expect_reads(world["stub"], types=["g6e.24xlarge"])
    expect_launch(e, "on-demand", ["g6e.24xlarge"], launched(itype="g6e.24xlarge"))
    e.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-1"]})
    e.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    status, body = post({"region": REGION, "instanceType": "g6e.24xlarge", "market": "on-demand"})
    assert status == 200 and body["group"] == "g6e/4" and body["instanceType"] == "g6e.24xlarge"
    assert world["table"].records()[0]["target"] == "g6e/4:g6e.24xlarge"


def test_the_older_mode_field_still_names_the_market(world):
    e = expect_reads(world["stub"], types=["p5.48xlarge"])
    expect_launch(e, "spot", ["p5.48xlarge"], launched(itype="p5.48xlarge"))
    e.add_response("terminate_instances", {"TerminatingInstances": []})
    e.add_response("delete_launch_template", {})
    assert post({"region": REGION, "instanceType": "p5.48xlarge", "mode": "spot"})[1]["market"] == "spot"


def test_an_arm_only_family_launches_the_arm64_image(world, monkeypatch):
    monkeypatch.setitem(OFFERS["us-east-1"]["families"], "g5g", ["g5g.xlarge"])
    monkeypatch.setitem(VCPUS, "g5g.xlarge", 4)
    e = expect_reads(world["stub"], types=["g5g.xlarge"], arch="arm64")
    expect_launch(e, "on-demand", ["g5g.xlarge"], launched(itype="g5g.xlarge"), arch="arm64")
    e.add_response("terminate_instances", {"TerminatingInstances": []})
    e.add_response("delete_launch_template", {})
    assert post({"region": REGION, "instanceType": "g5g.xlarge", "market": "on-demand"})[0] == 200
    assert api._ami_cache == {(REGION, "arm64"): "ami-arm64"}


@pytest.mark.parametrize("answer,result,code,az", [
    ({"Errors": [{"ErrorCode": "InsufficientInstanceCapacity", "LaunchTemplateAndOverrides": {
        "Overrides": {"InstanceType": "g6e.12xlarge", "SubnetId": "subnet-a"}}}], "Instances": []},
     "no-capacity", "InsufficientInstanceCapacity", "us-east-1a"),
    ({"Errors": [{"ErrorCode": "VcpuLimitExceeded"}], "Instances": []}, "quota", "VcpuLimitExceeded", None),
    ("InvalidParameterValue", "error", "InvalidParameterValue", None),
])
def test_no_launch_maps_the_errors_and_still_deletes_the_template(world, answer, result, code, az):
    e = expect_reads(world["stub"])
    expect_launch(e, "on-demand", G6E4, answer)
    e.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "on-demand"})
    assert status == 200 and (body["result"], body["errorCode"], body["obtained"]) == (result, code, None)
    assert body["errors"][0]["code"] == code and body["errors"][0]["az"] == az
    record = world["table"].records()[0]
    assert record["errorCode"] == code and "az" not in record
    # EC2 answered: the launch counts.
    assert body["today"]["user"] == 1 and body["today"]["all"] == 1


def test_a_failed_template_delete_is_logged_and_the_result_stands(world, caplog):
    e = expect_reads(world["stub"])
    expect_launch(e, "spot", G6E4, launched())
    e.add_response("terminate_instances", {"TerminatingInstances": []})
    e.add_client_error("delete_launch_template", "RequestLimitExceeded", http_status_code=503)
    with caplog.at_level("WARNING"):
        status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and body["result"] == "launched"
    assert "could not delete the test launch template lt-1" in caplog.text


# --------------------------------------------------- quota and default VPC
def test_sizes_above_the_free_quota_are_left_out(world, monkeypatch):
    # 64 vCPUs free: g6e.12xlarge (48) fits, g6e.24xlarge (96) does not.
    monkeypatch.setattr(api, "_quota_rows", lambda region, live=False: quota_rows(od_g=100.0, used=36.0))
    e = expect_reads(world["stub"])
    expect_launch(e, "on-demand", ["g6e.12xlarge"], launched())
    e.add_response("terminate_instances", {"TerminatingInstances": []})
    e.add_response("delete_launch_template", {})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "on-demand"})
    assert status == 200 and body["sizes"] == ["g6e.12xlarge"]
    assert body["skipped"] == [{"instanceType": "g6e.24xlarge", "vcpus": 96}]


def test_quota_too_low_is_refused_without_a_launch_request_and_gives_the_counts_back(world, monkeypatch):
    monkeypatch.setattr(api, "_quota_rows", lambda region, live=False: quota_rows(od_g=32.0))
    e = world["stub"]("ec2")
    e.add_response("describe_instance_types", {"InstanceTypes": [
        {"InstanceType": t, "VCpuInfo": {"DefaultVCpus": VCPUS[t]}, "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]}}
        for t in G6E4]})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "on-demand"})
    assert status == 400 and body["code"] == "quota-too-low"
    assert body["params"] == {"needed": 48, "available": 32, "quota": "Running On-Demand G and VT instances",
                              "quotaCode": None, "market": "on-demand"}
    t = world["table"]
    assert t.count("sub-alice", "2030-01-01") == 0 and t.all_count("2030-01-01") == 0
    assert t.items[("us-east-1", "#lock#g6e/4#on-demand")]["lockedUntil"] == 0 and t.records() == []


def test_an_unknown_quota_lets_ec2_decide(world, monkeypatch):
    monkeypatch.setattr(api, "_quota_rows", lambda region, live=False: (_ for _ in ()).throw(
        ClientError({"Error": {"Code": "AccessDenied"}}, "ListServiceQuotas")))
    e = expect_reads(world["stub"])
    expect_launch(e, "spot", G6E4, launched())
    e.add_response("terminate_instances", {"TerminatingInstances": []})
    e.add_response("delete_launch_template", {})
    assert post({"region": REGION, "group": "g6e/4", "market": "spot"})[1]["sizes"] == G6E4


@pytest.mark.parametrize("family,market,rows,expected", [
    ("p5en", "spot", quota_rows(spot_p=0.0, spot_p5=192.0), "All P5 Spot Instance Requests"),
    ("p4d", "spot", quota_rows(spot_p=96.0, spot_p5=192.0), "All P Spot Instance Requests"),
    ("p5", "on-demand", quota_rows(), "Running On-Demand P instances"),
    ("g6e", "spot", quota_rows(), "All G and VT Spot Instance Requests"),
    ("p5", "spot", quota_rows(), None),
])
def test_the_quota_of_a_family_prefers_the_row_that_names_it(family, market, rows, expected):
    row = api._quota_for(rows["quotas"], family, market)
    assert (row or {}).get("name") == expected


def test_no_default_vpc_is_a_400_and_no_template_is_created(world):
    e = world["stub"]("ec2")
    e.add_response("describe_instance_types", {"InstanceTypes": [
        {"InstanceType": t, "VCpuInfo": {"DefaultVCpus": VCPUS[t]}, "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]}}
        for t in G6E4]})
    e.add_response("describe_subnets", {"Subnets": []})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "on-demand"})
    assert status == 400 and body["code"] == "no-default-vpc" and body["params"] == {"region": REGION}
    assert "No default VPC in this Region" in body["error"]
    assert world["table"].count("sub-alice", "2030-01-01") == 0


# ------------------------------------------------------------- the guards
def test_the_deployment_wide_cap_refuses_before_any_ec2_call(world, monkeypatch):
    calls = no_ec2(monkeypatch)
    monkeypatch.setattr(api, "MAX_TEST_LAUNCHES_PER_DAY", 2)
    t = world["table"]
    t.items[("#probes#2030-01-01", "count")] = {"region": "#probes#2030-01-01", "sk": "count", "count": 2}
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 429 and body["code"] == "daily-cap-all"
    assert body["params"] == {"limit": 2, "resetAt": "2030-01-02T00:00:00Z"}
    assert "maxTestLaunchesPerDay" in body["error"]
    # The user's count taken just before is given back; the slot is free again.
    assert t.count("sub-alice", "2030-01-01") == 0 and t.all_count("2030-01-01") == 2
    assert t.items[("us-east-1", "#lock#g6e/4#spot")]["lockedUntil"] == 0 and calls == []


def test_two_requests_race_for_the_last_deployment_slot_and_only_one_launches(world, monkeypatch):
    """The conditional UpdateItem decides: the first takes the last slot, the second is refused
    (ConditionalCheckFailed) and never reaches EC2."""
    monkeypatch.setattr(api, "MAX_TEST_LAUNCHES_PER_DAY", 1)
    runs = []
    monkeypatch.setattr(api, "run_probe", lambda *a: runs.append(a) or {
        "result": "no-capacity", "errorCode": "InsufficientInstanceCapacity", "obtained": None, "instanceIds": [],
        "errors": [], "sizes": G6E4, "skipped": []})
    assert post({"region": REGION, "group": "g6e/4", "market": "spot"})[0] == 200
    status, body = post({"region": "us-west-2", "instanceType": "g6e.12xlarge", "market": "spot"}, sub="sub-bob")
    assert (status, body["code"]) == (429, "daily-cap-all") and len(runs) == 1
    assert world["table"].all_count("2030-01-01") == 1 and world["table"].count("sub-bob", "2030-01-01") == 0


def test_the_deployment_wide_cap_zero_is_off(world, monkeypatch):
    monkeypatch.setattr(api, "MAX_TEST_LAUNCHES_PER_DAY", 0)
    monkeypatch.setattr(api, "run_probe", lambda *a: {"result": "launched", "errorCode": None, "instanceIds": ["i-1"],
                                                      "obtained": {"az": "us-east-1a", "instanceType": "p5.48xlarge"},
                                                      "errors": [], "sizes": ["p5.48xlarge"], "skipped": []})
    status, body = post({"region": REGION, "instanceType": "p5.48xlarge", "market": "spot"})
    assert status == 200 and body["today"]["all"] is None and body["today"]["allLimit"] == 0
    assert not [k for k in world["table"].items if k[0].startswith("#probes#")]


@pytest.mark.parametrize("refusal", ["user-cap", "all-cap", "cooldown", "running"])
def test_a_refused_request_makes_no_ec2_call(world, monkeypatch, refusal):
    calls = no_ec2(monkeypatch)
    monkeypatch.setattr(api, "run_probe", lambda *a: pytest.fail("a refused request must not launch"))
    t = world["table"]
    if refusal == "user-cap":
        monkeypatch.setattr(api, "MAX_PROBES_PER_USER_PER_DAY", 1)
        t.items[("#probe-count#sub-alice", "2030-01-01")] = {"count": 1}
    elif refusal == "all-cap":
        monkeypatch.setattr(api, "MAX_TEST_LAUNCHES_PER_DAY", 1)
        t.items[("#probes#2030-01-01", "count")] = {"count": 1}
    elif refusal == "cooldown":
        rec = {"sk": "2030-01-01T09:55:00.000000Z#g6e/4#spot", "target": "g6e/4", "mode": "spot", "result": "no-capacity",
               "errorCode": "InsufficientInstanceCapacity", "sizes": G6E4}
        monkeypatch.setattr(api, "_recent_probes", lambda *_: [rec])
    else:
        t.items[("us-east-1", "#lock#g6e/4#spot")] = {"lockedUntil": api._epoch() + 600}
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    if refusal in ("user-cap", "all-cap"):
        assert status == 429 and body["code"] == ("probe-daily-limit" if refusal == "user-cap" else "daily-cap-all")
    else:
        assert status == 200 and body["cached"] is True
        assert body["result"] == ("no-capacity" if refusal == "cooldown" else "in-progress")
    assert calls == []


def test_the_cooldown_is_per_group_and_region_and_market(world, monkeypatch):
    runs = []
    monkeypatch.setattr(api, "run_probe", lambda region, family, sizes, market: runs.append((region, sizes, market)) or {
        "result": "launched", "errorCode": None, "instanceIds": ["i-1"], "errors": [], "skipped": [], "sizes": sizes,
        "obtained": {"az": f"{region}a", "instanceType": sizes[0]}})
    t = world["table"]
    monkeypatch.setattr(api, "_recent_probes", lambda region, since: sorted(
        [v for (r, sk), v in t.items.items() if r == region and not sk.startswith("#") and sk >= api.iso(since)],
        key=lambda v: v["sk"]))
    assert post({"region": REGION, "group": "g6e/4", "market": "spot"})[1]["cached"] is False
    # Same group, Region and market within the cooldown: the stored result, no launch.
    again = post({"region": REGION, "group": "g6e/4", "market": "spot"})[1]
    assert again["cached"] is True and again["obtained"] == {"az": "us-east-1a", "instanceType": "g6e.12xlarge"}
    # Another Region, another market, another group, or one size: each has its own slot.
    assert post({"region": "us-west-2", "group": "g6e/4", "market": "spot"})[1]["cached"] is False
    assert post({"region": REGION, "group": "g6e/4", "market": "on-demand"})[1]["cached"] is False
    assert post({"region": REGION, "group": "p5/8", "market": "spot"})[1]["cached"] is False
    assert post({"region": REGION, "instanceType": "g6e.24xlarge", "market": "spot"})[1]["cached"] is False
    assert len(runs) == 5 and runs[1] == ("us-west-2", ["g6e.12xlarge"], "spot")
    # After the cooldown the group launches again.
    world["clock"][0] += timedelta(minutes=api.COOLDOWN_MINUTES + 1)
    for key in [k for k in t.items if k[1].startswith("#lock#")]:
        t.items[key]["lockedUntil"] = 0
    assert post({"region": REGION, "group": "g6e/4", "market": "spot"})[1]["cached"] is False


@pytest.mark.parametrize("body,code", [
    ({"region": REGION, "group": "g9/1", "market": "spot"}, "unknown-group"),
    ({"region": REGION, "group": "x6/1", "market": "spot"}, "type-not-enabled"),
    ({"region": REGION, "group": "G6E/4", "market": "spot"}, "bad-parameter"),
    ({"region": "us-west-2", "group": "p5/8", "market": "spot"}, "group-not-offered"),
    ({"region": REGION, "group": "gr6/1", "market": "spot"}, "type-not-enabled"),
    ({"region": REGION, "market": "spot"}, "bad-parameter"),
    ({"region": "me-south-1", "group": "g6e/4", "market": "spot"}, "region-not-enabled"),
])
def test_bad_targets_are_refused_before_any_ec2_call(world, monkeypatch, body, code):
    calls = no_ec2(monkeypatch)
    status, out = post(body)
    assert status == 400 and out["code"] == code and calls == []
    assert world["table"].items == {}


def test_spot_is_refused_when_the_deployment_turns_it_off(world, monkeypatch):
    calls = no_ec2(monkeypatch)
    monkeypatch.setattr(api, "ENABLE_SPOT_PROBE", False)
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert (status, body["code"], calls) == (403, "spot-probes-disabled", [])


# ---------------------------------------------------------- GET /api/probes
def rec(hours_ago, target, mode, result, user, az=None, itype=None, group=None):
    at = datetime(2030, 1, 1, 10, 0, tzinfo=timezone.utc) - timedelta(hours=hours_ago)
    item = {"sk": f"{api.iso(at)}#{target}#{mode}", "target": target, "mode": mode, "result": result, "user": user}
    item.update({k: v for k, v in (("az", az), ("type", itype), ("group", group)) if v})
    return item


def test_probes_returns_today_counts_and_the_observed_results_per_group(world, monkeypatch):
    t = world["table"]
    t.items[("#probe-count#sub-alice", "2030-01-01")] = {"count": 3}
    t.items[("#probes#2030-01-01", "count")] = {"count": 41}
    rows = [rec(30, "g6e/4", "spot", "launched", "alice", "us-east-1a", "g6e.12xlarge", "g6e/4"),  # older than 24 h
            rec(5, "g6e/4", "spot", "launched", "alice", "us-east-1a", "g6e.12xlarge", "g6e/4"),
            rec(4, "g6e/4", "spot", "launched", "bob", "us-east-1a", "g6e.12xlarge", "g6e/4"),
            rec(3, "g6e/4:g6e.24xlarge", "spot", "launched", "carol", "us-east-1b", "g6e.24xlarge", "g6e/4"),
            rec(2, "g6e/4", "spot", "no-capacity", "dave", group="g6e/4"),
            rec(2, "g6e/4", "on-demand", "quota", "dave", group="g6e/4"),
            # A record of an earlier version (one type and zone, no group): its group comes from the GPU counts.
            {"sk": f"{api.iso(datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc))}#us-east-1a#p5.48xlarge#spot",
             "az": "us-east-1a", "type": "p5.48xlarge", "mode": "spot", "result": "error", "user": "erin"},
            rec(1, "g5.xlarge", "on-demand", "launched", "frank", "us-east-1c", "g5.xlarge")]
    monkeypatch.setattr(api, "_recent_probes", lambda region, since: [r for r in rows if r["sk"] >= api.iso(since)])
    world["stub"]("ec2").add_response("describe_subnets", {"Subnets": [
        {"SubnetId": s, "VpcId": "vpc-1", "AvailabilityZone": az} for az, s in SUBNETS]})
    r = api.handler(event("GET", "/api/probes", {"region": REGION}), None)
    body = json.loads(r["body"])
    assert body["today"] == {"day": "2030-01-01", "user": 3, "userLimit": 50, "all": 41, "allLimit": 100,
                             "resetAt": "2030-01-02T00:00:00Z"}
    assert (body["defaultVpc"], body["defaultSubnetZones"]) == (True, ["us-east-1a", "us-east-1b", "us-east-1c"])
    rows_by = {(o["group"] or o["instanceType"], o["market"]): o for o in body["observed"]}
    spot = rows_by[("g6e/4", "spot")]
    assert (spot["launched"], spot["noCapacity"], spot["total"], spot["label"]) == (3, 1, 4, "g6e · 4 GPUs")
    assert spot["landed"] == [{"az": "us-east-1a", "instanceType": "g6e.12xlarge", "count": 2},
                              {"az": "us-east-1b", "instanceType": "g6e.24xlarge", "count": 1}]
    assert spot["last"]["result"] == "no-capacity"
    assert rows_by[("g6e/4", "on-demand")]["quota"] == 1
    assert rows_by[("p5/8", "spot")]["other"] == 1
    assert rows_by[("g5.xlarge", "on-demand")]["group"] is None
    for user in ("alice", "bob", "carol", "dave", "erin", "frank", "sub-alice"):
        assert user not in r["body"]


def test_probes_in_a_region_without_a_default_vpc(world, monkeypatch):
    world["stub"]("ec2").add_response("describe_subnets", {"Subnets": []})
    body = json.loads(api.handler(event("GET", "/api/probes", {"region": REGION}), None)["body"])
    assert (body["defaultVpc"], body["defaultSubnetZones"], body["observed"]) == (False, [], [])


def test_probes_with_the_caps_off_show_no_counts(world, monkeypatch):
    monkeypatch.setattr(api, "MAX_PROBES_PER_USER_PER_DAY", 0)
    monkeypatch.setattr(api, "MAX_TEST_LAUNCHES_PER_DAY", 0)
    world["stub"]("ec2").add_client_error("describe_subnets", "UnauthorizedOperation", http_status_code=403)
    body = json.loads(api.handler(event("GET", "/api/probes", {"region": REGION}), None)["body"])
    assert (body["today"]["user"], body["today"]["all"], body["defaultVpc"]) == (None, None, None)


# ---------------------------------------------------------- pure helpers
def test_overrides_are_every_size_times_every_default_subnet():
    subnets = {"us-east-1b": "subnet-b", "us-east-1a": "subnet-a"}
    assert fleet_launch.overrides(subnets, ["g6e.12xlarge", "g6e.24xlarge"]) == [
        {"InstanceType": "g6e.12xlarge", "SubnetId": "subnet-a"}, {"InstanceType": "g6e.12xlarge", "SubnetId": "subnet-b"},
        {"InstanceType": "g6e.24xlarge", "SubnetId": "subnet-a"}, {"InstanceType": "g6e.24xlarge", "SubnetId": "subnet-b"}]
    many = {f"z{i}": f"s{i}" for i in range(10)}
    assert len(fleet_launch.overrides(many, [f"t{i}.x" for i in range(40)])) == fleet_launch.MAX_OVERRIDES


@pytest.mark.parametrize("sizes,vcpus,available,fit,skipped", [
    (["a", "b"], {"a": 8, "b": 16}, 8, ["a"], [{"instanceType": "b", "vcpus": 16}]),
    (["a", "b"], {"a": 8, "b": 16}, 16, ["a", "b"], []),
    (["a", "b"], {"a": 8, "b": 16}, 7, [], [{"instanceType": "a", "vcpus": 8}, {"instanceType": "b", "vcpus": 16}]),
    (["a"], {"a": 8}, None, ["a"], []),
    (["a"], {}, 0, ["a"], []),
])
def test_fitting_keeps_sizes_within_the_free_quota(sizes, vcpus, available, fit, skipped):
    assert fleet_launch.fitting(sizes, vcpus, available) == (fit, skipped)


def test_default_subnets_one_per_zone_and_none_raises():
    class Ec2:
        def __init__(self, subnets):
            self.subnets = subnets

        def describe_subnets(self, **_):
            return {"Subnets": self.subnets}
    found = fleet_launch.default_subnets(Ec2([
        {"SubnetId": "s-b", "VpcId": "vpc-1", "AvailabilityZone": "us-east-1b"},
        {"SubnetId": "s-a", "VpcId": "vpc-1", "AvailabilityZone": "us-east-1a"},
        {"SubnetId": "s-x", "VpcId": "vpc-1", "AvailabilityZone": "us-east-1c", "State": "pending"}]))
    assert found == ({"us-east-1a": "s-a", "us-east-1b": "s-b"}, "vpc-1")
    with pytest.raises(fleet_launch.NoDefaultVpc):
        fleet_launch.default_subnets(Ec2([]))


@pytest.mark.parametrize("errors,result,code", [
    (["InsufficientInstanceCapacity", "VcpuLimitExceeded"], "no-capacity", "InsufficientInstanceCapacity"),
    (["Unsupported", "InsufficientHostCapacity"], "no-capacity", "InsufficientHostCapacity"),
    (["Unsupported", "MaxSpotInstanceCountExceeded"], "quota", "MaxSpotInstanceCountExceeded"),
    (["Unsupported", "Unsupported"], "unsupported", "Unsupported"),
    (["Unsupported", "InvalidFleetConfiguration"], "error", "Unsupported"),
    ([], "error", "NoInstanceLaunched"),
])
def test_fleet_errors_map_to_the_result_codes(errors, result, code):
    resp = {"Errors": [{"ErrorCode": c} for c in errors], "Instances": []}
    out = fleet_launch.parse_fleet(resp, {})
    assert (out["result"], out["errorCode"], out["obtained"], out["instanceIds"]) == (result, code, None, [])
    assert [e["code"] for e in out["errors"]] == errors


def test_a_launched_instance_reports_its_zone_from_the_subnet_when_ec2_leaves_it_out():
    resp = {"Errors": [{"ErrorCode": "InsufficientInstanceCapacity"}], "Instances": [{
        "InstanceIds": ["i-9"], "InstanceType": "p5.48xlarge",
        "LaunchTemplateAndOverrides": {"Overrides": {"SubnetId": "subnet-c"}}}]}
    out = fleet_launch.parse_fleet(resp, {"subnet-c": "us-east-1c"})
    assert out["result"] == "launched" and out["obtained"] == {"az": "us-east-1c", "instanceType": "p5.48xlarge"}
    assert out["errors"] == [{"code": "InsufficientInstanceCapacity", "message": "", "az": None, "instanceType": None}]


@pytest.mark.parametrize("market,strategy_key,strategy", [
    ("spot", "SpotOptions", "price-capacity-optimized"), ("on-demand", "OnDemandOptions", "lowest-price")])
def test_fleet_params_strategy_per_market(market, strategy_key, strategy):
    p = fleet_launch.fleet_params("lt-1", 1, [], market, token="t")
    assert p[strategy_key]["AllocationStrategy"] == strategy
    assert ({"SpotOptions", "OnDemandOptions"} - {strategy_key}).isdisjoint(p)
    assert p["Type"] == "instant" and p["TargetCapacitySpecification"] == {
        "TotalTargetCapacity": 1, "DefaultTargetCapacityType": market}


def test_template_data_has_the_self_shutdown_and_no_public_address():
    od = fleet_launch.template_data("ami-1", "sg-1", "on-demand")
    assert base64.b64decode(od["UserData"]).decode() == "#!/bin/bash\nshutdown -h +5\n"
    assert od["NetworkInterfaces"][0]["AssociatePublicIpAddress"] is False
    assert od["InstanceInitiatedShutdownBehavior"] == "terminate" and od["MetadataOptions"] == {"HttpTokens": "required"}
    assert od["CapacityReservationSpecification"] == {"CapacityReservationPreference": "none"}
    assert "CapacityReservationSpecification" not in fleet_launch.template_data("ami-1", "sg-1", "spot")
    assert "InstanceMarketOptions" not in fleet_launch.template_data("ami-1", "sg-1", "spot")


class FakeEc2:
    def __init__(self, fleet):
        self.fleet, self.calls = fleet, []

    def create_launch_template(self, **kw):
        self.calls.append("create_launch_template")
        return {"LaunchTemplate": {"LaunchTemplateId": "lt-9", "LatestVersionNumber": 1}}

    def create_fleet(self, **kw):
        self.calls.append("create_fleet")
        if isinstance(self.fleet, Exception):
            raise self.fleet
        return self.fleet

    def terminate_instances(self, **kw):
        self.calls.append("terminate_instances")
        raise ClientError({"Error": {"Code": "RequestLimitExceeded"}}, "TerminateInstances")

    def delete_launch_template(self, **kw):
        self.calls.append(("delete_launch_template", kw["LaunchTemplateId"]))


def launch_with(ec2):
    return fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                               sizes=["p5.48xlarge"], market="spot")


def test_the_template_is_deleted_even_when_the_fleet_call_breaks():
    ec2 = FakeEc2(RuntimeError("connection reset"))
    with pytest.raises(RuntimeError):
        launch_with(ec2)
    assert ec2.calls == ["create_launch_template", "create_fleet", ("delete_launch_template", "lt-9")]


def test_a_failed_terminate_is_logged_and_the_template_still_goes(caplog):
    ec2 = FakeEc2(launched(az="us-east-1a", itype="p5.48xlarge", subnet="subnet-a"))
    out = launch_with(ec2)
    assert out["result"] == "launched" and out["instanceIds"] == ["i-1"]
    assert ec2.calls[-2:] == ["terminate_instances", ("delete_launch_template", "lt-9")]
    assert "terminate failed" in caplog.text


def test_a_fleet_client_error_is_a_result():
    ec2 = FakeEc2(ClientError({"Error": {"Code": "InsufficientInstanceCapacity"}}, "CreateFleet"))
    out = launch_with(ec2)
    assert (out["result"], out["errorCode"]) == ("no-capacity", "InsufficientInstanceCapacity")
    assert ec2.calls[-1] == ("delete_launch_template", "lt-9")


def test_no_delete_fleets_call():
    """An instant fleet is deleted by EC2 itself once its instances are terminated (or none
    launched); the function never calls DeleteFleets and has no permission for it."""
    import inspect
    assert "delete_fleets" not in inspect.getsource(fleet_launch) + inspect.getsource(api)


# ------------------------------------------- unknown outcome, deadline, every instance
class OutcomeEc2:
    """A fake EC2 client for launch(): CreateFleet raises `fleet_error` (or answers `fleet`), and
    a resend answers `resent` when given (else the same as the first call); DescribeInstances
    answers `found` (or raises it)."""

    def __init__(self, fleet_error=None, fleet=None, found=(), terminate_error=None, resent=None):
        self.fleet_error, self.fleet, self.found, self.terminate_error = fleet_error, fleet, found, terminate_error
        self.resent = resent
        self.calls, self.params, self.fleets = [], {}, []

    def create_launch_template(self, **kw):
        self.calls.append("create_launch_template")
        return {"LaunchTemplate": {"LaunchTemplateId": "lt-9", "LatestVersionNumber": 1}}

    def create_fleet(self, **kw):
        self.calls.append("create_fleet")
        self.params.setdefault("create_fleet", kw)
        self.fleets.append(kw)
        if len(self.fleets) > 1 and self.resent is not None:
            if isinstance(self.resent, Exception):
                raise self.resent
            return self.resent
        if self.fleet_error is not None:
            raise self.fleet_error
        return self.fleet

    def describe_instances(self, **kw):
        self.calls.append("describe_instances")
        self.params["describe_instances"] = kw
        if isinstance(self.found, Exception):
            raise self.found
        return {"Reservations": [{"Instances": [{"InstanceId": i} for i in self.found]}]}

    def terminate_instances(self, **kw):
        self.calls.append(("terminate_instances", tuple(kw["InstanceIds"])))
        if self.terminate_error is not None:
            raise self.terminate_error

    def delete_launch_template(self, **kw):
        self.calls.append(("delete_launch_template", kw["LaunchTemplateId"]))


def read_timeout():
    return ReadTimeoutError(endpoint_url="https://ec2.us-east-1.amazonaws.com/")


def server_error():
    return ClientError({"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "CreateFleet")


def test_the_instance_carries_a_per_launch_tag_with_the_client_token():
    p = fleet_launch.fleet_params("lt-1", 1, [], "spot", token="tok-1")
    tags = {t["ResourceType"]: t["Tags"] for t in p["TagSpecifications"]}
    assert tags["instance"] == [{"Key": fleet_launch.PROBE_TAG, "Value": "true"},
                                {"Key": fleet_launch.LAUNCH_TAG, "Value": "tok-1"}]
    assert all(tags[r] == [{"Key": fleet_launch.PROBE_TAG, "Value": "true"}] for r in ("volume", "network-interface", "fleet"))
    assert p["ClientToken"] == "tok-1"


@pytest.mark.parametrize("error", [read_timeout(), server_error(), ConnectionClosedError(endpoint_url="https://x"),
                                   SSLError(endpoint_url="https://x", error="eof"),
                                   ClientError({"Error": {"Code": "Unavailable"},
                                                "ResponseMetadata": {"HTTPStatusCode": 503}}, "CreateFleet")])
def test_an_unknown_outcome_looks_up_the_launch_tag_and_terminates_what_it_finds(error, caplog):
    ec2 = OutcomeEc2(fleet_error=error, found=["i-7"])
    out = launch_with(ec2)
    assert (out["result"], out["errorCode"], out["obtained"], out["instanceIds"]) == ("error", "unknown-outcome", None, ["i-7"])
    assert "refund" not in out
    assert out["errors"][0]["code"] in ("ReadTimeoutError", "InternalError", "ConnectionClosedError", "SSLError",
                                        "Unavailable")
    token = ec2.params["create_fleet"]["ClientToken"]
    instance_tags = next(t["Tags"] for t in ec2.params["create_fleet"]["TagSpecifications"] if t["ResourceType"] == "instance")
    assert {"Key": fleet_launch.LAUNCH_TAG, "Value": token} in instance_tags
    filters = ec2.params["describe_instances"]["Filters"]
    assert filters[0] == {"Name": f"tag:{fleet_launch.LAUNCH_TAG}", "Values": [token]}
    assert ec2.params["describe_instances"]["MaxResults"] == 50
    # The resend (same request) failed too, then the lookup.
    assert ec2.fleets[1] == ec2.fleets[0]
    assert ec2.calls == ["create_launch_template", "create_fleet", "create_fleet", "describe_instances",
                         ("terminate_instances", ("i-7",)), ("delete_launch_template", "lt-9")]


@pytest.mark.parametrize("error,code", [
    (ConnectTimeoutError(endpoint_url="https://x"), "ConnectTimeoutError"),
    (EndpointConnectionError(endpoint_url="https://x"), "EndpointConnectionError"),
    (ParamValidationError(report="bad"), "ParamValidationError"),
    (NoCredentialsError(), "NoCredentialsError"),
    (ClientError({"Error": {"Code": "RequestLimitExceeded"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "CreateFleet"),
     "RequestLimitExceeded"),
    (ClientError({"Error": {"Code": "Throttling"}, "ResponseMetadata": {"HTTPStatusCode": 400}}, "CreateFleet"),
     "Throttling"),
])
def test_a_request_that_was_not_sent_or_was_throttled_launched_nothing_and_asks_for_a_refund(error, code):
    ec2 = OutcomeEc2(fleet_error=error, found=["i-7"])
    out = launch_with(ec2)
    assert (out["result"], out["errorCode"], out["instanceIds"], out["refund"]) == ("error", code, [], True)
    assert out["errors"] == [{"code": code, "az": None, "instanceType": None}]
    assert ec2.calls == ["create_launch_template", "create_fleet", ("delete_launch_template", "lt-9")]


@pytest.mark.parametrize("code,result", [("InsufficientInstanceCapacity", "no-capacity"), ("VcpuLimitExceeded", "quota")])
def test_a_5xx_with_a_capacity_or_quota_code_is_a_known_outcome(code, result):
    ec2 = OutcomeEc2(fleet_error=ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 500}},
                                             "CreateFleet"))
    out = launch_with(ec2)
    assert (out["result"], out["errorCode"], out["instanceIds"]) == (result, code, [])
    assert "refund" not in out and ec2.calls == ["create_launch_template", "create_fleet", ("delete_launch_template", "lt-9")]


def test_an_unknown_outcome_resends_the_same_request_and_uses_its_answer():
    ec2 = OutcomeEc2(fleet_error=read_timeout(), found=["i-7"],
                     resent=launched(az="us-east-1a", itype="p5.48xlarge", subnet="subnet-a"))
    out = launch_with(ec2)
    assert (out["result"], out["errorCode"], out["instanceIds"]) == ("launched", None, ["i-1"])
    assert out["obtained"] == {"az": "us-east-1a", "instanceType": "p5.48xlarge"}
    # Same ClientToken and same parameters: EC2 answers the first request's outcome.
    assert len(ec2.fleets) == 2 and ec2.fleets[1] == ec2.fleets[0]
    assert ec2.calls == ["create_launch_template", "create_fleet", "create_fleet", ("terminate_instances", ("i-1",)),
                         ("delete_launch_template", "lt-9")]


def test_a_resent_no_capacity_answer_is_the_result_without_a_lookup():
    ec2 = OutcomeEc2(fleet_error=server_error(), found=["i-7"],
                     resent={"Errors": [{"ErrorCode": "InsufficientInstanceCapacity"}], "Instances": []})
    out = launch_with(ec2)
    assert (out["result"], out["errorCode"]) == ("no-capacity", "InsufficientInstanceCapacity")
    assert "describe_instances" not in ec2.calls


def test_too_little_time_for_the_resend_goes_to_the_lookup():
    made = []
    ec2 = OutcomeEc2(fleet_error=read_timeout(), found=["i-7"], resent=launched())
    left = iter([20.0, 8.9, 8.0, 7.0])
    out = fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                              sizes=["p5.48xlarge"], market="spot", time_left=lambda: next(left),
                              single_try=lambda c, r: made.append((c, r)) or ec2)
    assert out["errorCode"] == "unknown-outcome" and out["instanceIds"] == ["i-7"]
    assert ec2.calls.count("create_fleet") == 1 and made == [(2, 10), (1, 2), (1, 2)]


def test_a_late_read_timeout_skips_the_resend_to_keep_the_answer_reserve():
    # CreateFleet at 12.9 s left (read 2 s) times out with 9.8 s left: a resend (1 + 2 s) plus the
    # lookup and terminate (3 s each) would leave less than ANSWER_RESERVE_SECONDS for the answer.
    made = []
    ec2 = OutcomeEc2(fleet_error=read_timeout(), found=["i-7"], resent=launched())
    left = iter([12.9, 9.8, 9.8, 6.8])
    out = fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                              sizes=["p5.48xlarge"], market="spot", time_left=lambda: next(left),
                              single_try=lambda c, r: made.append((c, r)) or ec2)
    assert out["errorCode"] == "unknown-outcome" and out["instanceIds"] == ["i-7"]
    assert ec2.calls.count("create_fleet") == 1 and made[:2] == [(2, 2), (1, 2)]


@pytest.mark.parametrize("left,expected", [(None, 15), (10.9, None), (11.0, 2), (14.0, 5), (40.0, 15)])
def test_the_resend_read_timeout_keeps_the_lookup_and_terminate_steps(left, expected):
    assert fleet_launch.resend_read_timeout(left) == expected


@pytest.mark.parametrize("left,expected", [(None, 4), (20.0, 4), (5.0, 3), (2.5, 1), (0.5, 1)])
def test_the_terminate_read_timeout_fits_the_time_left(left, expected):
    assert fleet_launch.terminate_read_timeout(left) == expected


def test_a_launch_terminates_with_the_one_attempt_client_and_skips_the_template_delete_when_late(caplog):
    made = []
    ec2 = OutcomeEc2(fleet=launched(az="us-east-1a", itype="p5.48xlarge", subnet="subnet-a"))
    left = iter([12.0, 5.0, 2.9])
    with caplog.at_level("WARNING"):
        out = fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                                  sizes=["p5.48xlarge"], market="spot", time_left=lambda: next(left),
                                  single_try=lambda c, r: made.append((c, r)) or ec2)
    assert out["result"] == "launched"
    # CreateFleet (2, 2), then terminate with connect 1 s and read 5 - 1 - 1 = 3 s; no template delete.
    assert made == [(2, 2), (1, 3)]
    assert ec2.calls == ["create_launch_template", "create_fleet", ("terminate_instances", ("i-1",))]
    assert "left for the scheduled cleanup" in caplog.text


@pytest.mark.parametrize("after_terminate,deleted", [(4.9, False), (5.0, True)])
def test_the_template_delete_leaves_the_answer_reserve(after_terminate, deleted, caplog):
    """A launch answered near its read timeout: the terminate may take up to 5 seconds, so the
    template delete (up to 3 seconds) runs only when it still leaves ANSWER_RESERVE_SECONDS for the
    record and the answer."""
    ec2 = OutcomeEc2(fleet=launched(az="us-east-1a", itype="p5.48xlarge", subnet="subnet-a"))
    left = iter([21.0, 9.0, after_terminate])
    with caplog.at_level("WARNING"):
        out = fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                                  sizes=["p5.48xlarge"], market="spot", time_left=lambda: next(left))
    assert out["result"] == "launched" and ("terminate_instances", ("i-1",)) in ec2.calls
    assert (("delete_launch_template", "lt-9") in ec2.calls) is deleted
    assert ("left for the scheduled cleanup" in caplog.text) is not deleted
    assert fleet_launch.TEMPLATE_DELETE_MIN_SECONDS == fleet_launch.AFTER_STEP_SECONDS + fleet_launch.ANSWER_RESERVE_SECONDS


def test_an_unknown_outcome_terminates_what_it_found_even_when_late_and_skips_the_template_delete():
    ec2 = OutcomeEc2(fleet_error=read_timeout(), found=["i-7"])
    left = iter([20.0, 3.0, 3.0, 1.0])
    out = fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                              sizes=["p5.48xlarge"], market="spot", time_left=lambda: next(left))
    assert out["instanceIds"] == ["i-7"]
    assert ec2.calls == ["create_launch_template", "create_fleet", "describe_instances", ("terminate_instances", ("i-7",))]


def test_an_unknown_outcome_without_an_instance_terminates_nothing():
    ec2 = OutcomeEc2(fleet_error=read_timeout(), found=[])
    out = launch_with(ec2)
    assert out["errorCode"] == "unknown-outcome" and out["instanceIds"] == []
    assert not [c for c in ec2.calls if isinstance(c, tuple) and c[0] == "terminate_instances"]


@pytest.mark.parametrize("found,terminate_error", [
    (ClientError({"Error": {"Code": "RequestLimitExceeded"}}, "DescribeInstances"), None),
    (["i-7"], read_timeout()),
])
def test_a_failed_lookup_or_terminate_after_an_unknown_outcome_is_logged(found, terminate_error, caplog):
    ec2 = OutcomeEc2(fleet_error=read_timeout(), found=found, terminate_error=terminate_error)
    with caplog.at_level("WARNING"):
        out = launch_with(ec2)
    assert out["errorCode"] == "unknown-outcome" and ec2.calls[-1] == ("delete_launch_template", "lt-9")
    assert "could not look up" in caplog.text or "terminate failed" in caplog.text


def test_a_4xx_fleet_error_is_a_known_outcome_without_a_lookup():
    ec2 = OutcomeEc2(fleet_error=ClientError({"Error": {"Code": "InvalidParameterValue"},
                                              "ResponseMetadata": {"HTTPStatusCode": 400}}, "CreateFleet"))
    out = launch_with(ec2)
    assert (out["result"], out["errorCode"]) == ("error", "InvalidParameterValue")
    assert "describe_instances" not in ec2.calls


def test_the_unknown_outcome_calls_use_the_short_single_try_client():
    made = []
    ec2 = OutcomeEc2(fleet_error=read_timeout(), found=["i-7"])
    fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                        sizes=["p5.48xlarge"], market="spot", time_left=lambda: 20.0,
                        single_try=lambda c, r: made.append((c, r)) or ec2)
    # CreateFleet: connect 2 s, read 20 - 8 - 2 = 10 s; the resend: connect 1 s, read 20 - 1 - 6 - 2 = 11 s
    # (the lookup, the terminate and the caller's answer stay free); then the lookup (and
    # terminate) client and the template delete client: connect 1 s, read 2 s.
    assert made == [(2, 10), (1, 11), (1, 2), (1, 2)]


def test_no_time_left_for_the_lookup_skips_it(caplog):
    ec2 = OutcomeEc2(fleet_error=read_timeout(), found=["i-7"])
    left = iter([20.0, 2.0, 2.0, 2.0])
    with caplog.at_level("WARNING"):
        out = fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                                  sizes=["p5.48xlarge"], market="spot", time_left=lambda: next(left))
    assert out["errorCode"] == "unknown-outcome" and out["instanceIds"] == []
    assert ec2.calls == ["create_launch_template", "create_fleet"]
    assert "no time left to look up" in caplog.text and "left for the scheduled cleanup" in caplog.text


@pytest.mark.parametrize("left", [11.9, 0.5])
def test_too_little_time_left_sends_no_create_fleet(left):
    ec2 = OutcomeEc2(fleet=launched())
    with pytest.raises(fleet_launch.NoTimeLeft):
        fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                            sizes=["p5.48xlarge"], market="spot", time_left=lambda: left)
    # Less than one step left: the template stays for the scheduled cleanup.
    assert ec2.calls == ["create_launch_template"] + ([("delete_launch_template", "lt-9")] if left >= 5 else [])


def test_twelve_seconds_left_still_sends_create_fleet():
    ec2 = OutcomeEc2(fleet=launched(az="us-east-1a", itype="p5.48xlarge", subnet="subnet-a"))
    out = fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                              sizes=["p5.48xlarge"], market="spot", time_left=lambda: 12.0)
    assert out["result"] == "launched" and "create_fleet" in ec2.calls


@pytest.mark.parametrize("left,expected", [(None, 15), (12.0, 2), (20.0, 10), (20.9, 10), (60.0, 15), (5.0, 1)])
def test_the_fleet_read_timeout_leaves_the_cleanup_reserve(left, expected):
    assert fleet_launch.fleet_read_timeout(left) == expected


def test_the_no_resend_unknown_outcome_path_keeps_the_answer_reserve():
    """With MIN_SECONDS_FOR_FLEET left at send, a CreateFleet that runs to its connect and read
    timeouts leaves too little for a resend, yet still enough for the lookup and the terminate (one
    AFTER_STEP_SECONDS each) plus ANSWER_RESERVE_SECONDS for the caller."""
    f = fleet_launch
    assert f.CLEANUP_RESERVE_SECONDS == 2 * f.AFTER_STEP_SECONDS + f.ANSWER_RESERVE_SECONDS
    left = f.MIN_SECONDS_FOR_FLEET - f.FLEET_CONNECT_TIMEOUT - f.fleet_read_timeout(f.MIN_SECONDS_FOR_FLEET)
    assert f.resend_read_timeout(left) is None
    assert left - 2 * f.AFTER_STEP_SECONDS >= f.ANSWER_RESERVE_SECONDS


def test_the_single_try_client_has_one_attempt_and_the_given_timeouts(monkeypatch):
    monkeypatch.setattr(api, "_clients", {})
    c = api._single_try_ec2("us-east-1", 2, 9)
    assert c.meta.config.retries["total_max_attempts"] == 1
    assert (c.meta.config.connect_timeout, c.meta.config.read_timeout) == (2, 9)
    assert api._single_try_ec2("us-east-1", 2, 9) is c and api._single_try_ec2("us-east-1", 2, 8) is not c


def test_every_instance_of_every_instances_entry_is_collected_and_terminated():
    resp = {"Errors": [], "Instances": [
        {"InstanceIds": ["i-1"], "InstanceType": "g6e.12xlarge",
         "LaunchTemplateAndOverrides": {"Overrides": {"SubnetId": "subnet-a"}}},
        {"InstanceIds": ["i-2", "i-3"], "InstanceType": "g6e.24xlarge",
         "LaunchTemplateAndOverrides": {"Overrides": {"SubnetId": "subnet-b"}}},
        {"InstanceIds": [], "InstanceType": "g6e.48xlarge"}]}
    out = fleet_launch.parse_fleet(resp, {"subnet-a": "us-east-1a", "subnet-b": "us-east-1b"})
    assert out["instanceIds"] == ["i-1", "i-2", "i-3"]
    assert out["obtained"] == {"az": "us-east-1a", "instanceType": "g6e.12xlarge"}
    ec2 = OutcomeEc2(fleet=resp)
    fleet_launch.launch(ec2, image_id="ami-1", security_group="sg-1", subnets={"us-east-1a": "subnet-a"},
                        sizes=["g6e.12xlarge"], market="spot")
    assert ("terminate_instances", ("i-1", "i-2", "i-3")) in ec2.calls


def test_a_later_entry_with_instances_still_counts_as_launched():
    resp = {"Errors": [], "Instances": [{"InstanceIds": [], "InstanceType": "g6e.12xlarge"},
                                        {"InstanceIds": ["i-9"], "InstanceType": "g6e.24xlarge",
                                         "LaunchTemplateAndOverrides": {"Overrides": {"SubnetId": "subnet-b"}}}]}
    out = fleet_launch.parse_fleet(resp, {"subnet-b": "us-east-1b"})
    assert out["result"] == "launched" and out["obtained"] == {"az": "us-east-1b", "instanceType": "g6e.24xlarge"}


# ------------------------------------------- the API around the unknown outcome and the deadline
class LostAnswer:
    """The one-attempt client of the API test: CreateFleet loses its answer, every other call goes
    to the Stubber-backed client."""

    def __init__(self, inner):
        self.inner = inner

    def create_fleet(self, **kw):
        self.token = kw["ClientToken"]
        raise read_timeout()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_an_unknown_outcome_keeps_the_counts_and_the_cooldown_and_terminates_the_instance(world, monkeypatch):
    e = expect_reads(world["stub"])
    e.add_response("create_launch_template", {"LaunchTemplate": {"LaunchTemplateId": "lt-1", "LatestVersionNumber": 1}})
    proxy = LostAnswer(api._clients[("ec2", REGION)])
    monkeypatch.setattr(api, "_single_try_ec2", lambda region, connect, read: proxy)
    e.add_response("describe_instances", {"Reservations": [{"Instances": [{"InstanceId": "i-5"}]}]})
    e.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-5"]})
    e.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and (body["result"], body["errorCode"], body["cached"]) == ("error", "unknown-outcome", False)
    assert body["errors"] == [{"code": "ReadTimeoutError", "az": None, "instanceType": None}]
    # The request may have reached EC2: both counts stay, the cooldown slot stays taken.
    assert body["today"]["user"] == 1 and body["today"]["all"] == 1
    t = world["table"]
    assert t.items[("us-east-1", "#lock#g6e/4#spot")]["lockedUntil"] > 0
    assert t.records()[0]["errorCode"] == "unknown-outcome"


def test_a_late_request_sends_nothing_and_gives_the_counts_back(world, monkeypatch):
    monkeypatch.setattr(api, "_time_left", lambda: 8.0)
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert (status, body["code"], body["params"]) == (503, "internal", {"reason": "no-time-left"})
    t = world["table"]
    assert t.count("sub-alice", "2030-01-01") == 0 and t.all_count("2030-01-01") == 0
    assert t.items[("us-east-1", "#lock#g6e/4#spot")]["lockedUntil"] == 0 and t.records() == []
    # Stopped before the first EC2 call: no lookup, no template, no CreateFleet client.
    assert world["single"] == []


@pytest.mark.parametrize("calls_in_time", range(6))
def test_every_ec2_step_before_create_fleet_checks_the_time_left(world, monkeypatch, calls_in_time):
    """A slow lookup before CreateFleet stops the request at the next step with the 503: the counts
    and the cooldown slot are given back. Steps: instance types, quota, subnets, image, security
    group, launch template."""
    e = expect_reads(world["stub"])
    # Keep only the responses of the steps that still run (the quota rows are not an EC2 call).
    runs = {"DescribeInstanceTypes": 1, "DescribeSubnets": 3, "DescribeSecurityGroups": 5}
    e._queue = type(e._queue)(r for r in e._queue if runs[r["operation_name"]] <= calls_in_time)
    ssm = world["stub"]("ssm")
    if calls_in_time < 4:
        ssm._queue.clear()
    left = itertools.chain([20.0] * calls_in_time, itertools.repeat(11.9))
    monkeypatch.setattr(api, "_time_left", lambda: next(left))
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert (status, body["params"]) == (503, {"reason": "no-time-left"})
    t = world["table"]
    assert t.count("sub-alice", "2030-01-01") == 0 and t.all_count("2030-01-01") == 0 and t.records() == []
    assert world["single"] == []


def test_the_api_passes_the_invocation_time_left_to_the_fleet_read_timeout(world, monkeypatch):
    e = expect_reads(world["stub"])
    expect_launch(e, "spot", G6E4, launched())
    e.add_response("terminate_instances", {"TerminatingInstances": []})
    e.add_response("delete_launch_template", {})
    monkeypatch.setattr(api, "_time_left", lambda: 21.0)
    assert post({"region": REGION, "group": "g6e/4", "market": "spot"})[0] == 200
    # CreateFleet, then the terminate and the template delete on one-attempt clients.
    assert world["single"] == [(REGION, 2, 11), (REGION, 1, 4), (REGION, 1, 2)]


def test_a_throttled_fleet_request_gives_the_counts_and_the_cooldown_back(world):
    e = expect_reads(world["stub"])
    params = expect_launch(e, "spot", G6E4, {})
    e._queue.pop()
    e.add_client_error("create_fleet", "RequestLimitExceeded", http_status_code=503, expected_params=params)
    e.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and (body["result"], body["errorCode"], body["cached"]) == ("error", "RequestLimitExceeded", False)
    assert "refund" not in body
    t = world["table"]
    assert t.count("sub-alice", "2030-01-01") == 0 and t.all_count("2030-01-01") == 0
    assert t.items[("us-east-1", "#lock#g6e/4#spot")]["lockedUntil"] == 0 and t.records() == []
    assert body["today"]["user"] == 0


# ------------------------------------------- a stale security group id
def sg_gone_answer():
    return {"Errors": [{"ErrorCode": "InvalidGroup.NotFound"}], "Instances": []}


@pytest.mark.parametrize("first", ["InvalidGroup.NotFound", "errors"])
def test_a_deleted_security_group_is_looked_up_again_and_the_launch_tried_once_more(world, first):
    e = expect_reads(world["stub"])
    expect_launch(e, "spot", G6E4, "InvalidGroup.NotFound" if first != "errors" else sg_gone_answer())
    e.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    e.add_response("describe_security_groups", {"SecurityGroups": []})
    e.add_response("create_security_group", {"GroupId": "sg-new"})
    expect_launch(e, "spot", G6E4, launched(), sg="sg-new")
    e.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-1"]})
    e.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and body["result"] == "launched"
    assert api._sg_cache[(REGION, "vpc-1")] == "sg-new"
    assert body["today"]["user"] == 1


def test_a_security_group_refused_by_the_template_is_looked_up_again(world):
    e = expect_reads(world["stub"])
    e.add_client_error("create_launch_template", "InvalidGroup.NotFound", http_status_code=400)
    e.add_response("describe_security_groups", {"SecurityGroups": [{"GroupId": "sg-new"}]})
    expect_launch(e, "spot", G6E4, launched(), sg="sg-new")
    e.add_response("terminate_instances", {"TerminatingInstances": []})
    e.add_response("delete_launch_template", {})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and body["result"] == "launched"


OPEN_RULE = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]


def test_a_tagged_security_group_with_inbound_rules_is_refused_before_any_launch(world):
    expect_reads(world["stub"], groups=[{"GroupId": "sg-open", "IpPermissions": OPEN_RULE}])
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 409 and body["code"] == "probe-sg-open"
    assert body["params"]["group"] == "sg-open"
    assert (REGION, "vpc-1") not in api._sg_cache


def test_the_tagged_group_without_inbound_rules_is_picked(world):
    world["stub"]("ec2").add_response("describe_security_groups", {"SecurityGroups": [
        {"GroupId": "sg-open", "IpPermissions": OPEN_RULE}, {"GroupId": "sg-closed", "IpPermissions": []}]})
    assert api._probe_security_group(REGION, "vpc-1") == "sg-closed"


def test_a_concurrently_created_group_is_also_checked_for_inbound_rules(world):
    e = world["stub"]("ec2")
    e.add_response("describe_security_groups", {"SecurityGroups": []})
    e.add_client_error("create_security_group", "InvalidGroup.Duplicate", http_status_code=400)
    e.add_response("describe_security_groups", {"SecurityGroups": [{"GroupId": "sg-open", "IpPermissions": OPEN_RULE}]})
    with pytest.raises(api.HttpError) as err:
        api._probe_security_group(REGION, "vpc-1")
    assert err.value.code == "probe-sg-open"


def test_the_name_conflict_message_does_not_ask_to_tag_the_group(world):
    e = world["stub"]("ec2")
    e.add_response("describe_security_groups", {"SecurityGroups": []})
    e.add_client_error("create_security_group", "InvalidGroup.Duplicate", http_status_code=400)
    e.add_response("describe_security_groups", {"SecurityGroups": []})
    with pytest.raises(api.HttpError) as err:
        api._probe_security_group(REGION, "vpc-1")
    assert err.value.code == "probe-sg-conflict" and "tag it" not in str(err.value)


def test_the_security_group_retry_happens_once(world):
    e = expect_reads(world["stub"])
    expect_launch(e, "spot", G6E4, sg_gone_answer())
    e.add_response("delete_launch_template", {})
    e.add_response("describe_security_groups", {"SecurityGroups": [{"GroupId": "sg-probe"}]})
    expect_launch(e, "spot", G6E4, sg_gone_answer())
    e.add_response("delete_launch_template", {})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and (body["result"], body["errorCode"]) == ("error", "InvalidGroup.NotFound")


def test_a_late_retry_keeps_the_first_answer(world, monkeypatch):
    e = expect_reads(world["stub"])
    expect_launch(e, "spot", G6E4, sg_gone_answer())
    e.add_response("delete_launch_template", {})
    # The first attempt reads the time left 8 times (4 lookups, the security group, the template,
    # CreateFleet and the template delete); the retry finds too little before its security group
    # lookup and sends nothing.
    left = itertools.chain([20.0] * 8, itertools.repeat(5.0))
    monkeypatch.setattr(api, "_time_left", lambda: next(left))
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and body["errorCode"] == "InvalidGroup.NotFound" and body["today"]["user"] == 1


def test_a_failed_security_group_retry_keeps_the_first_answer_that_reached_ec2(world):
    e = expect_reads(world["stub"])
    expect_launch(e, "spot", G6E4, sg_gone_answer())
    e.add_response("delete_launch_template", {})
    e.add_client_error("describe_security_groups", "UnauthorizedOperation", http_status_code=403)
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and (body["result"], body["errorCode"]) == ("error", "InvalidGroup.NotFound")
    t = world["table"]
    assert body["today"]["user"] == 1 and t.records()[0]["errorCode"] == "InvalidGroup.NotFound"


def test_a_throttled_security_group_retry_keeps_the_first_answer(world):
    e = expect_reads(world["stub"])
    expect_launch(e, "spot", G6E4, sg_gone_answer())
    e.add_response("delete_launch_template", {})
    e.add_response("describe_security_groups", {"SecurityGroups": [{"GroupId": "sg-new"}]})
    params = expect_launch(e, "spot", G6E4, {}, sg="sg-new")
    e._queue.pop()
    e.add_client_error("create_fleet", "RequestLimitExceeded", http_status_code=503, expected_params=params)
    e.add_response("delete_launch_template", {})
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status == 200 and body["errorCode"] == "InvalidGroup.NotFound" and body["today"]["user"] == 1
    assert world["table"].records()[0]["errorCode"] == "InvalidGroup.NotFound"


def test_a_security_group_retry_that_raises_after_a_refused_template_still_refunds(world):
    e = expect_reads(world["stub"])
    e.add_client_error("create_launch_template", "InvalidGroup.NotFound", http_status_code=400)
    e.add_client_error("describe_security_groups", "UnauthorizedOperation", http_status_code=403)
    status, body = post({"region": REGION, "group": "g6e/4", "market": "spot"})
    assert status >= 400
    t = world["table"]
    assert t.count("sub-alice", "2030-01-01") == 0 and t.records() == []


def test_unfulfillable_capacity_is_no_capacity_with_the_ec2_reason():
    resp = {"Errors": [{"ErrorCode": "UnfulfillableCapacity",
                        "ErrorMessage": "There is no Spot capacity available that matches your request.",
                        "LaunchTemplateAndOverrides": {"Overrides": {"InstanceType": "g6e.xlarge", "SubnetId": "s-1"}}}],
            "Instances": []}
    out = fleet_launch.parse_fleet(resp, {"s-1": "us-east-2a"})
    assert out["result"] == "no-capacity" and out["errorCode"] == "UnfulfillableCapacity"
    assert out["errorMessage"] == "There is no Spot capacity available that matches your request."


def test_an_other_error_keeps_the_ec2_message():
    resp = {"Errors": [{"ErrorCode": "InvalidParameterCombination", "ErrorMessage": "Some reason.",
                        "LaunchTemplateAndOverrides": {"Overrides": {"InstanceType": "g6e.xlarge", "SubnetId": "s-1"}}}]}
    out = fleet_launch.parse_fleet(resp, {"s-1": "us-east-1a"})
    assert out["result"] == "error" and out["errorCode"] == "InvalidParameterCombination" and out["errorMessage"] == "Some reason."
