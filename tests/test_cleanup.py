import os
from datetime import datetime, timedelta, timezone

import boto3  # noqa: E402
import cleanup  # noqa: E402
import pytest  # noqa: E402
from botocore.stub import Stubber  # noqa: E402


def ec2(region):
    return boto3.client("ec2", region_name=region, aws_access_key_id="x", aws_secret_access_key="x")


def inst(i, age):
    return {"InstanceId": i, "LaunchTime": datetime.now(timezone.utc) - age}


@pytest.fixture
def regions(monkeypatch):
    """One stubbed EC2 client per Region, returned by boto3.client(region_name=...)."""
    clients = {}

    def make(*names):
        monkeypatch.setattr(cleanup, "target_regions", lambda: list(names))
        for name in names:
            clients[name] = ec2(name)
        monkeypatch.setattr(cleanup.boto3, "client", lambda _svc, region_name, config=None: clients[region_name])
        return {name: Stubber(c) for name, c in clients.items()}
    return make


TAGGED = [{"Name": "tag:gpu-capacity-finder-probe", "Values": ["true"]}]


def tpl(i, age):
    return {"LaunchTemplateId": i, "CreateTime": datetime.now(timezone.utc) - age}


def no_templates(s):
    s.add_response("describe_launch_templates", {"LaunchTemplates": []}, {"Filters": TAGGED})


def test_terminates_only_old_probe_instances(regions):
    s = regions("us-east-1")["us-east-1"]
    s.add_response("describe_instances", {"Reservations": [{"Instances": [
        inst("i-old", timedelta(hours=1)), inst("i-new", timedelta(minutes=1))]}]})
    s.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-old"]})
    no_templates(s)
    with s:
        assert cleanup.handler({}, None) == {"terminated": {"us-east-1": ["i-old"]}, "deletedTemplates": {},
                                             "failedRegions": []}


def test_deletes_only_probe_launch_templates_older_than_an_hour(regions):
    s = regions("us-east-1")["us-east-1"]
    s.add_response("describe_instances", {"Reservations": []})
    s.add_response("describe_launch_templates", {"LaunchTemplates": [
        tpl("lt-old", timedelta(hours=1, minutes=1)), tpl("lt-new", timedelta(minutes=59)),
        tpl("lt-gone", timedelta(hours=3))]}, {"Filters": TAGGED})
    s.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-old"})
    # Deleted in between (by the test launch itself): already gone counts as done.
    s.add_client_error("delete_launch_template", "InvalidLaunchTemplateId.NotFound", http_status_code=400,
                       expected_params={"LaunchTemplateId": "lt-gone"})
    with s:
        assert cleanup.handler({}, None) == {"terminated": {}, "deletedTemplates": {"us-east-1": ["lt-old", "lt-gone"]},
                                             "failedRegions": []}


def test_a_template_that_cannot_be_deleted_fails_only_its_region(regions):
    stubs = regions("eu-west-1", "us-east-1")
    for name, s in stubs.items():
        s.add_response("describe_instances", {"Reservations": []})
    stubs["eu-west-1"].add_response("describe_launch_templates", {"LaunchTemplates": [tpl("lt-x", timedelta(hours=2))]})
    stubs["eu-west-1"].add_client_error("delete_launch_template", "UnauthorizedOperation", http_status_code=403)
    stubs["us-east-1"].add_response("describe_launch_templates", {"LaunchTemplates": [tpl("lt-y", timedelta(hours=2))]})
    stubs["us-east-1"].add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-y"})
    for s in stubs.values():
        s.activate()
    assert cleanup.handler({}, None) == {"terminated": {}, "deletedTemplates": {"us-east-1": ["lt-y"]},
                                         "failedRegions": ["eu-west-1"]}
    for s in stubs.values():
        s.assert_no_pending_responses()


def test_failed_region_does_not_stop_the_others(regions, caplog):
    stubs = regions("ap-east-1", "us-east-1")
    stubs["ap-east-1"].add_client_error("describe_instances", "AuthFailure", http_status_code=401)
    stubs["us-east-1"].add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-old", timedelta(hours=1))]}]})
    stubs["us-east-1"].add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-old"]})
    no_templates(stubs["us-east-1"])
    for s in stubs.values():
        s.activate()
    with caplog.at_level("WARNING"):
        result = cleanup.handler({}, None)
    assert result == {"terminated": {"us-east-1": ["i-old"]}, "deletedTemplates": {}, "failedRegions": ["ap-east-1"]}
    for s in stubs.values():
        s.assert_no_pending_responses()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "ap-east-1" in warnings[0].getMessage() and "AuthFailure" in warnings[0].getMessage()
    assert not warnings[0].exc_info


def test_every_region_failed_fails_the_invocation(regions):
    stubs = regions("ap-east-1", "us-east-1")
    stubs["ap-east-1"].add_client_error("describe_instances", "AuthFailure", http_status_code=401)
    stubs["us-east-1"].add_client_error("describe_instances", "RequestLimitExceeded", http_status_code=503)
    for s in stubs.values():
        s.activate()
    with pytest.raises(cleanup.CleanupFailed):
        cleanup.handler({}, None)


def test_the_region_list_falls_back_to_the_functions_own_region(monkeypatch):
    from botocore.exceptions import ClientError

    class Broken:
        def describe_regions(self, **_):
            raise ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeRegions")
    monkeypatch.setattr(cleanup, "HOME_REGION", "us-west-2")
    monkeypatch.setattr(cleanup.boto3, "client", lambda *a, **k: Broken())
    assert cleanup.target_regions() == ["us-west-2"]


def test_a_failed_region_lookup_logs_the_line_the_region_failure_alarm_counts(monkeypatch, caplog):
    """The fallback cleans the home Region only, so it must reach the probe-cleanup-region-failures
    alarm (its metric filter counts the literal "probe cleanup failed in")."""
    import pathlib
    import re
    from botocore.exceptions import ClientError

    class Broken:
        def describe_regions(self, **_):
            raise ClientError({"Error": {"Code": "RequestLimitExceeded"}}, "DescribeRegions")
    monkeypatch.setattr(cleanup, "HOME_REGION", "us-west-2")
    monkeypatch.setattr(cleanup.boto3, "client", lambda *a, **k: Broken())
    with caplog.at_level("WARNING"):
        assert cleanup.target_regions() == ["us-west-2"]
    stack = (pathlib.Path(__file__).parent.parent / "gpu_capacity_finder" / "stack.py").read_text()
    pattern = re.search(r"FilterPattern\.literal\('\"([^\"]+)\"'\)", stack).group(1)
    assert pattern == "probe cleanup failed in"
    lines = [r.getMessage() for r in caplog.records if pattern in r.getMessage()]
    assert len(lines) == 1 and "region-discovery" in lines[0] and "RequestLimitExceeded" in lines[0]


def test_a_failed_terminate_still_deletes_the_regions_templates(regions, caplog):
    stubs = regions("eu-west-1", "us-east-1")
    eu = stubs["eu-west-1"]
    eu.add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-old", timedelta(hours=1))]}]})
    eu.add_client_error("terminate_instances", "RequestLimitExceeded", http_status_code=503)
    eu.add_response("describe_launch_templates", {"LaunchTemplates": [tpl("lt-old", timedelta(hours=2))]},
                    {"Filters": TAGGED})
    eu.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-old"})
    stubs["us-east-1"].add_response("describe_instances", {"Reservations": []})
    no_templates(stubs["us-east-1"])
    for s in stubs.values():
        s.activate()
    with caplog.at_level("WARNING"):
        result = cleanup.handler({}, None)
    assert result == {"terminated": {}, "deletedTemplates": {"eu-west-1": ["lt-old"]}, "failedRegions": ["eu-west-1"]}
    for s in stubs.values():
        s.assert_no_pending_responses()
    # The alarm's metric filter counts this line.
    assert "probe cleanup failed in eu-west-1 (RequestLimitExceeded)" in caplog.text


def test_a_failed_terminate_in_every_region_fails_the_invocation(regions):
    s = regions("us-east-1")["us-east-1"]
    s.add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-old", timedelta(hours=1))]}]})
    s.add_client_error("terminate_instances", "UnauthorizedOperation", http_status_code=403)
    no_templates(s)
    with s, pytest.raises(cleanup.CleanupFailed):
        cleanup.handler({}, None)


def test_a_failed_template_delete_keeps_the_regions_terminate_result(regions, caplog):
    stubs = regions("eu-west-1", "us-east-1")
    s = stubs["eu-west-1"]
    s.add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-old", timedelta(hours=1))]}]})
    s.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-old"]})
    s.add_response("describe_launch_templates", {"LaunchTemplates": [tpl("lt-x", timedelta(hours=2)),
                                                                     tpl("lt-y", timedelta(hours=2))]})
    s.add_client_error("delete_launch_template", "RequestLimitExceeded", http_status_code=503,
                       expected_params={"LaunchTemplateId": "lt-x"})
    s.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-y"})
    stubs["us-east-1"].add_response("describe_instances", {"Reservations": []})
    no_templates(stubs["us-east-1"])
    for st in stubs.values():
        st.activate()
    with caplog.at_level("WARNING"):
        result = cleanup.handler({}, None)
    # One template failing does not stop the next one nor drop the terminated instances; the
    # Region still counts as failed (alarm).
    assert result == {"terminated": {"eu-west-1": ["i-old"]}, "deletedTemplates": {"eu-west-1": ["lt-y"]},
                      "failedRegions": ["eu-west-1"]}
    assert "lt-x" in caplog.text and "RequestLimitExceeded" in caplog.text
    for st in stubs.values():
        st.assert_no_pending_responses()


def test_a_failed_template_lookup_keeps_the_regions_terminate_result(regions):
    stubs = regions("eu-west-1", "us-east-1")
    eu = stubs["eu-west-1"]
    eu.add_response("describe_instances", {"Reservations": [{"Instances": [inst("i-old", timedelta(hours=1))]}]})
    eu.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": ["i-old"]})
    eu.add_client_error("describe_launch_templates", "RequestLimitExceeded", http_status_code=503)
    stubs["us-east-1"].add_response("describe_instances", {"Reservations": []})
    no_templates(stubs["us-east-1"])
    for s in stubs.values():
        s.activate()
    assert cleanup.handler({}, None) == {"terminated": {"eu-west-1": ["i-old"]}, "deletedTemplates": {},
                                         "failedRegions": ["eu-west-1"]}
    for s in stubs.values():
        s.assert_no_pending_responses()
