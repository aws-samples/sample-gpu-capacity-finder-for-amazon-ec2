"""The stack-delete custom resource (lambda/teardown.py): removes probe launch templates and
security groups in every enabled Region, tolerates missing ones and failing Regions, and always
answers SUCCESS within a bounded time. EC2 runs against botocore Stubbers; the answer to
CloudFormation is captured locally."""
import json
import time

import boto3
import pytest
import teardown
from botocore.stub import Stubber

STACK_DELETING = teardown.stack_deleting
TAG = [{"Name": "tag:gpu-capacity-finder-probe", "Values": ["true"]}]
SG_TAG = [{"Name": "tag:gpu-capacity-finder-probe-sg", "Values": ["true"]}]
RUNNING = {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}
EVENT = {"RequestType": "Delete", "ResponseURL": "https://cloudformation-custom-resource-response.example/x",
         "StackId": "stack", "RequestId": "req", "LogicalResourceId": "ProbeTeardown", "PhysicalResourceId": "probe-teardown"}


class Context:
    def __init__(self, ms=300000):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


@pytest.fixture(autouse=True)
def deleting(monkeypatch):
    """The stack itself is being deleted, unless a test says otherwise."""
    monkeypatch.setattr(teardown, "stack_deleting", lambda event: True)


@pytest.fixture
def answers(monkeypatch):
    """Every PUT to the response URL, as the parsed JSON body."""
    sent = []

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, timeout):
        assert request.get_method() == "PUT" and request.full_url == EVENT["ResponseURL"] and timeout <= 10
        sent.append(json.loads(request.data))
        return Answer()
    monkeypatch.setattr(teardown._HTTPS_ONLY, "open", urlopen)
    return sent


@pytest.fixture
def regions(monkeypatch):
    clients = {}

    def make(*names):
        monkeypatch.setattr(teardown, "target_regions", lambda: (list(names), False))
        for name in names:
            clients[name] = boto3.client("ec2", region_name=name, aws_access_key_id="x", aws_secret_access_key="x")
        monkeypatch.setattr(teardown.boto3, "client", lambda _svc, region_name, config=None: clients[region_name])
        stubs = {name: Stubber(c) for name, c in clients.items()}
        for s in stubs.values():
            s.activate()
        return stubs
    return make


def expect_region(s, instances=(), templates=(), groups=()):
    s.add_response("describe_instances", {"Reservations": [{"Instances": [{"InstanceId": i} for i in instances]}]},
                   {"Filters": [*TAG, RUNNING]})
    if instances:
        s.add_response("terminate_instances", {"TerminatingInstances": []}, {"InstanceIds": list(instances)})
    s.add_response("describe_launch_templates", {"LaunchTemplates": [{"LaunchTemplateId": t} for t in templates]},
                   {"Filters": TAG})


def expect_groups(s, groups):
    s.add_response("describe_security_groups", {"SecurityGroups": [{"GroupId": g} for g in groups]}, {"Filters": SG_TAG})


@pytest.mark.parametrize("kind", ["Create", "Update"])
def test_create_and_update_do_nothing_and_succeed(answers, monkeypatch, kind):
    monkeypatch.setattr(teardown.boto3, "client", lambda *a, **k: pytest.fail("no AWS call on create or update"))
    teardown.handler({**EVENT, "RequestType": kind}, Context())
    assert [a["Status"] for a in answers] == ["SUCCESS"]
    assert answers[0]["PhysicalResourceId"] == "probe-teardown" and answers[0]["LogicalResourceId"] == "ProbeTeardown"


def test_delete_removes_templates_and_groups_and_terminates_running_probes(answers, regions):
    s = regions("us-east-1")["us-east-1"]
    expect_region(s, instances=["i-1"], templates=["lt-1", "lt-2"])
    s.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    # Already gone: fine.
    s.add_client_error("delete_launch_template", "InvalidLaunchTemplateId.NotFound", http_status_code=400,
                       expected_params={"LaunchTemplateId": "lt-2"})
    expect_groups(s, ["sg-1", "sg-2"])
    s.add_response("delete_security_group", {}, {"GroupId": "sg-1"})
    s.add_client_error("delete_security_group", "InvalidGroup.NotFound", http_status_code=400,
                       expected_params={"GroupId": "sg-2"})
    teardown.handler(EVENT, Context())
    s.assert_no_pending_responses()
    assert answers == [{**answers[0], "Status": "SUCCESS"}]
    # Nothing is reported as left behind: a resource already gone counts as removed.
    assert "left" not in answers[0]["Reason"] and "failed" not in answers[0]["Reason"]


def test_a_group_still_in_use_is_asked_again_until_it_goes(regions, monkeypatch):
    s = regions("us-east-1")["us-east-1"]
    expect_region(s)
    expect_groups(s, ["sg-1"])
    s.add_client_error("delete_security_group", "DependencyViolation", http_status_code=400)
    s.add_client_error("delete_security_group", "DependencyViolation", http_status_code=400)
    s.add_response("delete_security_group", {}, {"GroupId": "sg-1"})
    pauses = []
    assert teardown.clean_region("us-east-1", time.monotonic() + 60, sleep=pauses.append) == {"left": []}
    assert pauses == [teardown.RETRY_SECONDS, teardown.RETRY_SECONDS]
    s.assert_no_pending_responses()


def test_a_group_in_use_at_the_deadline_is_left_and_logged(regions, caplog):
    s = regions("us-east-1")["us-east-1"]
    expect_region(s)
    expect_groups(s, ["sg-1"])
    s.add_client_error("delete_security_group", "DependencyViolation", http_status_code=400)
    with caplog.at_level("WARNING"):
        assert teardown.clean_region("us-east-1", time.monotonic() + 1, sleep=pytest.fail) == {"left": ["sg-1"]}
    assert "sg-1" in caplog.text


def test_a_failing_region_does_not_stop_the_others_and_the_answer_is_success(answers, regions, caplog):
    stubs = regions("ap-east-1", "us-east-1")
    stubs["ap-east-1"].add_client_error("describe_instances", "AuthFailure", http_status_code=401)
    expect_region(stubs["us-east-1"], templates=["lt-1"])
    stubs["us-east-1"].add_client_error("delete_launch_template", "UnauthorizedOperation", http_status_code=403)
    expect_groups(stubs["us-east-1"], ["sg-1"])
    stubs["us-east-1"].add_response("delete_security_group", {}, {"GroupId": "sg-1"})
    with caplog.at_level("WARNING"):
        teardown.handler(EVENT, Context())
    for s in stubs.values():
        s.assert_no_pending_responses()
    assert answers[0]["Status"] == "SUCCESS"
    assert "ap-east-1" in answers[0]["Reason"] and "lt-1" in answers[0]["Reason"]
    assert "probe teardown left" in caplog.text and "probe teardown failed in ap-east-1" in caplog.text


def test_a_stack_delete_waits_for_running_test_launches_before_the_scan(answers, monkeypatch):
    """Deleting the API function does not stop a test launch already running: the scan starts
    only after SETTLE_SECONDS, and the deadline counts from after the wait."""
    events = []
    monkeypatch.setattr(teardown, "SETTLE_SECONDS", 30)
    monkeypatch.setattr(teardown, "teardown", lambda deadline: events.append("scan") or {
        "left": [], "failedRegions": []})
    teardown.handler(EVENT, Context(), sleep=lambda s: events.append(s))
    assert events == [30, "scan"] and answers[0]["Status"] == "SUCCESS"
    # Create and Update do not wait.
    events.clear()
    teardown.handler({**EVENT, "RequestType": "Create"}, Context(), sleep=lambda s: events.append(s))
    assert events == []


def test_a_crash_still_answers_success(answers, monkeypatch):
    monkeypatch.setattr(teardown, "teardown", lambda deadline: 1 / 0)
    teardown.handler(EVENT, Context())
    assert answers[0]["Status"] == "SUCCESS" and "ZeroDivisionError" in answers[0]["Reason"]


def test_regions_past_the_deadline_are_reported_not_awaited(answers, monkeypatch):
    """A Region that does not answer in time never holds the answer: the work stops at the deadline
    (the function timeout minus RESERVE_SECONDS)."""
    monkeypatch.setattr(teardown, "target_regions", lambda: (["us-east-1", "us-west-2"], False))

    def slow(region, deadline):
        if region == "us-west-2":
            time.sleep(1.5)
        return {"left": []}
    monkeypatch.setattr(teardown, "clean_region", slow)
    monkeypatch.setattr(teardown, "RESERVE_SECONDS", 0)
    started = time.monotonic()
    teardown.handler(EVENT, Context(ms=500))
    assert time.monotonic() - started < 1.2
    assert answers[0]["Status"] == "SUCCESS" and "us-west-2" in answers[0]["Reason"]


def test_the_region_list_skips_excluded_regions_and_falls_back_to_home(monkeypatch):
    class Ec2:
        def describe_regions(self, **kw):
            assert kw == {"AllRegions": False}
            return {"Regions": [{"RegionName": r} for r in ("us-east-1", "me-south-1", "eu-west-1")]}
    monkeypatch.setattr(teardown, "EXCLUDED", frozenset({"me-south-1"}))
    monkeypatch.setattr(teardown.boto3, "client", lambda *a, **k: Ec2())
    assert teardown.target_regions() == (["eu-west-1", "us-east-1"], False)

    from botocore.exceptions import ClientError

    class Broken:
        def describe_regions(self, **_):
            raise ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeRegions")
    monkeypatch.setattr(teardown, "HOME_REGION", "us-west-2")
    monkeypatch.setattr(teardown.boto3, "client", lambda *a, **k: Broken())
    assert teardown.target_regions() == (["us-west-2"], True)


# ------------------------------------------------- only a delete of the stack itself
STACK_ID = "arn:aws:cloudformation:eu-west-1:111122223333:stack/gpu-capacity-finder/0a1b2c3d"


def cfn_stub(monkeypatch, status=None, error=None):
    cfn = boto3.client("cloudformation", region_name="eu-west-1", aws_access_key_id="x", aws_secret_access_key="x")
    s = Stubber(cfn)
    if error:
        s.add_client_error("describe_stacks", error, http_status_code=400, expected_params={"StackName": STACK_ID})
    else:
        s.add_response("describe_stacks", {"Stacks": [{"StackName": "gpu-capacity-finder", "StackId": STACK_ID,
                                                       "CreationTime": "2030-01-01T00:00:00Z", "StackStatus": status}]},
                       {"StackName": STACK_ID})
    s.activate()
    made = []
    monkeypatch.setattr(teardown.boto3, "client", lambda svc, region_name, config=None: made.append((svc, region_name)) or cfn)
    return s, made


@pytest.mark.parametrize("status,expected", [("DELETE_IN_PROGRESS", True), ("UPDATE_IN_PROGRESS", False),
                                             ("UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", False),
                                             ("UPDATE_ROLLBACK_IN_PROGRESS", False), ("ROLLBACK_IN_PROGRESS", False)])
def test_only_a_stack_in_delete_in_progress_counts_as_a_stack_delete(monkeypatch, status, expected):
    s, made = cfn_stub(monkeypatch, status=status)
    assert STACK_DELETING({**EVENT, "StackId": STACK_ID}) is expected
    s.assert_no_pending_responses()
    # The stack's own Region, from the StackId ARN.
    assert made == [("cloudformation", "eu-west-1")]


def test_a_failed_stack_lookup_removes_nothing(monkeypatch, caplog):
    s, _ = cfn_stub(monkeypatch, error="AccessDenied")
    with caplog.at_level("WARNING"):
        assert STACK_DELETING({**EVENT, "StackId": STACK_ID}) is False
    assert "probe teardown skipped" in caplog.text


def test_a_delete_during_an_update_answers_success_and_removes_nothing(answers, monkeypatch):
    monkeypatch.setattr(teardown, "stack_deleting", STACK_DELETING)
    s, made = cfn_stub(monkeypatch, status="UPDATE_COMPLETE_CLEANUP_IN_PROGRESS")
    monkeypatch.setattr(teardown, "teardown", lambda deadline: pytest.fail("no teardown outside a stack delete"))
    teardown.handler({**EVENT, "StackId": STACK_ID}, Context())
    assert [a["Status"] for a in answers] == ["SUCCESS"] and made == [("cloudformation", "eu-west-1")]


def test_a_failed_region_lookup_is_named_in_the_answer_not_reported_as_complete(answers, regions, monkeypatch, caplog):
    s = regions("us-west-2")["us-west-2"]
    monkeypatch.setattr(teardown, "target_regions", lambda: (["us-west-2"], True))
    monkeypatch.setattr(teardown, "HOME_REGION", "us-west-2")
    expect_region(s)
    expect_groups(s, [])
    with caplog.at_level("INFO"):
        teardown.handler(EVENT, Context())
    s.assert_no_pending_responses()
    assert answers[0]["Status"] == "SUCCESS"
    assert "region discovery failed; cleaned us-west-2 only" in answers[0]["Reason"]
    assert "removed every probe" not in caplog.text


def test_a_failed_terminate_still_deletes_the_regions_templates_and_names_the_instances(answers, regions, caplog):
    s = regions("us-east-1")["us-east-1"]
    s.add_response("describe_instances", {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]},
                   {"Filters": [*TAG, RUNNING]})
    s.add_client_error("terminate_instances", "RequestLimitExceeded", http_status_code=503)
    s.add_response("describe_launch_templates", {"LaunchTemplates": [{"LaunchTemplateId": "lt-1"}]}, {"Filters": TAG})
    s.add_response("delete_launch_template", {}, {"LaunchTemplateId": "lt-1"})
    expect_groups(s, [])
    with caplog.at_level("WARNING"):
        teardown.handler(EVENT, Context())
    s.assert_no_pending_responses()
    reason = answers[0]["Reason"]
    assert answers[0]["Status"] == "SUCCESS" and "i-1" in reason and "lt-1" not in reason
    # The Region itself did not fail: its other work was done.
    assert "failed in []" in reason and "could not terminate" in caplog.text


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://cloudformation-custom-resource-response.example/x",
                                 "ftp://example/x", ""])
def test_a_response_url_that_is_not_https_is_not_opened(monkeypatch, caplog, url):
    opened = []
    monkeypatch.setattr(teardown._HTTPS_ONLY, "open", lambda *a, **k: opened.append(a))
    teardown.handler({**EVENT, "RequestType": "Create", "ResponseURL": url}, Context())
    assert opened == [] and "not an https URL" in caplog.text


def test_an_https_response_url_is_answered(answers):
    assert teardown.respond(EVENT, "SUCCESS") == 200 and answers[0]["Status"] == "SUCCESS"


def test_the_answer_opener_has_no_file_or_plain_http_handler():
    # Even a URL that slipped past the scheme check could not be read from disk or sent unencrypted.
    schemes = {h.__class__.__name__ for h in teardown._HTTPS_ONLY.handlers}
    assert "HTTPSHandler" in schemes and not schemes & {"FileHandler", "HTTPHandler", "FTPHandler", "DataHandler"}
    # No handler takes a file: URL, so nothing is read.
    assert teardown._HTTPS_ONLY.open("file:///etc/hosts", timeout=1) is None
