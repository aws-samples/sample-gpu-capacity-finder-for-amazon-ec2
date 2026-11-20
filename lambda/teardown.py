"""CloudFormation custom resource that removes what test launches left in the account when the
stack is deleted: probe-tagged launch templates and the probe security groups (one per default
VPC), in every enabled Region minus excludeRegions. Probe-tagged instances that still run are
terminated first, because a security group in use cannot be deleted.

Create and Update do nothing. A Delete acts only when the stack itself is being deleted
(DescribeStacks on the event's StackId says DELETE_IN_PROGRESS); a Delete of the resource alone (an
update that removes it, or a rollback) answers SUCCESS and removes nothing. On a stack delete it
first waits SETTLE_SECONDS, so a test launch that was already running when the API function was
deleted has ended before the scan. Delete is tolerant: a resource that is already gone is fine, a
Region that fails is logged and skipped, and the work stops at a deadline well inside the
function's timeout. The function ALWAYS answers SUCCESS, so it never holds up the stack delete;
whatever it could not remove is named in the log ("probe teardown left ..."). The custom resource
also has a service timeout, so even a crashed run cannot hold the delete for long.
"""
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HOME_REGION = os.environ.get("AWS_REGION") or "us-east-1"
EXCLUDED = frozenset(r for r in os.environ.get("EXCLUDE_REGIONS", "").split(",") if r)
PROBE_TAG = "gpu-capacity-finder-probe"
PROBE_SG_TAG = "gpu-capacity-finder-probe-sg"
EC2_CONFIG = Config(retries={"mode": "standard", "total_max_attempts": 2}, connect_timeout=3, read_timeout=10)
CFN_CONFIG = Config(retries={"mode": "standard", "total_max_attempts": 3}, connect_timeout=3, read_timeout=10)
STACK_DELETING = "DELETE_IN_PROGRESS"
WORKERS = 8
# Seconds kept free before the function's timeout for the answer to CloudFormation.
RESERVE_SECONDS = 30
# A group still used by a terminating instance is asked again after this pause, until the deadline.
RETRY_SECONDS = 10
# Seconds to wait before the scan: deleting the API function does not stop a test launch already
# running, so the teardown waits for its timeout (plus a margin) before it lists what is left.
SETTLE_SECONDS = max(0, int(os.environ.get("SETTLE_SECONDS", "0") or 0))
GONE = {"InvalidGroup.NotFound", "InvalidLaunchTemplateId.NotFound", "InvalidLaunchTemplateName.NotFoundException",
        "InvalidInstanceID.NotFound"}


def _code(err):
    return err.response["Error"]["Code"] if isinstance(err, ClientError) else type(err).__name__


def target_regions():
    """(Regions, discovery failed): the enabled Regions minus the excluded ones, or the function's
    own Region and True when the lookup fails (the other Regions are then not checked)."""
    try:
        resp = boto3.client("ec2", region_name=HOME_REGION, config=EC2_CONFIG).describe_regions(AllRegions=False)
        names = sorted({r["RegionName"] for r in resp["Regions"]} - EXCLUDED)
        if names:
            return names, False
    except (ClientError, BotoCoreError, KeyError) as err:
        logger.warning("Region discovery failed for the probe teardown (%s); cleaning %s only", _code(err), HOME_REGION)
        return [HOME_REGION], True
    return [HOME_REGION], False


def _delete(fn, **kwargs):
    """True when the resource is gone (deleted now or already)."""
    try:
        fn(**kwargs)
        return True
    except ClientError as err:
        if _code(err) in GONE:
            return True
        raise


def clean_region(region, deadline, sleep=time.sleep, clock=time.monotonic):
    """Remove one Region's probe resources. Returns {"left": [ids not removed]}; raises only on a
    failed lookup (the Region is then reported as failed). A failed terminate leaves the instance
    ids in "left" and still deletes the Region's launch templates (and the security groups that
    are free by then)."""
    ec2 = boto3.client("ec2", region_name=region, config=EC2_CONFIG)
    tag = [{"Name": f"tag:{PROBE_TAG}", "Values": ["true"]}]
    running = [i["InstanceId"] for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[*tag, {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        for r in page["Reservations"] for i in r["Instances"]]
    left = []
    if running:
        try:
            ec2.terminate_instances(InstanceIds=running)
        except (ClientError, BotoCoreError) as err:
            logger.warning("could not terminate probe instances %s in %s (%s)", running, region, _code(err))
            left += running
    templates = [t["LaunchTemplateId"] for page in ec2.get_paginator("describe_launch_templates").paginate(Filters=tag)
                 for t in page["LaunchTemplates"]]
    for template_id in templates:
        try:
            _delete(ec2.delete_launch_template, LaunchTemplateId=template_id)
        except (ClientError, BotoCoreError) as err:
            logger.warning("could not delete launch template %s in %s (%s)", template_id, region, _code(err))
            left.append(template_id)
    groups = [g["GroupId"] for page in ec2.get_paginator("describe_security_groups").paginate(
        Filters=[{"Name": f"tag:{PROBE_SG_TAG}", "Values": ["true"]}]) for g in page["SecurityGroups"]]
    for group_id in groups:
        while True:
            try:
                _delete(ec2.delete_security_group, GroupId=group_id)
                break
            except ClientError as err:
                # A terminating probe instance still holds the group: wait while time is left.
                if _code(err) == "DependencyViolation" and clock() + RETRY_SECONDS < deadline:
                    sleep(RETRY_SECONDS)
                    continue
                logger.warning("could not delete security group %s in %s (%s)", group_id, region, _code(err))
                left.append(group_id)
                break
            except BotoCoreError as err:
                logger.warning("could not delete security group %s in %s (%s)", group_id, region, _code(err))
                left.append(group_id)
                break
    return {"left": left}


def teardown(deadline):
    """Clean every Region in parallel until the deadline. Returns {"left": {region: ids},
    "failedRegions": [...], "regionDiscoveryFailed": bool} for the log."""
    regions, discovery_failed = target_regions()
    left, failed = {}, []
    pool = ThreadPoolExecutor(max_workers=min(WORKERS, len(regions)))
    futures = {pool.submit(clean_region, r, deadline): r for r in regions}
    done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    for future in done:
        region = futures[future]
        try:
            result = future.result()
            if result["left"]:
                left[region] = result["left"]
        except (ClientError, BotoCoreError) as err:
            logger.warning("probe teardown failed in %s (%s)", region, _code(err))
            failed.append(region)
    for future in pending:
        logger.warning("probe teardown did not finish in %s before the deadline", futures[future])
        failed.append(futures[future])
    # Do not wait for Regions past the deadline: the answer to CloudFormation goes first.
    pool.shutdown(wait=False, cancel_futures=True)
    return {"left": left, "failedRegions": sorted(failed), "regionDiscoveryFailed": discovery_failed}


def stack_deleting(event):
    """True only when the stack named by the event's StackId is being deleted (DescribeStacks says
    DELETE_IN_PROGRESS). A failed lookup is logged and counts as False: nothing is removed."""
    stack_id = event.get("StackId") or ""
    parts = stack_id.split(":")
    region = parts[3] if len(parts) > 3 and parts[3] else HOME_REGION
    try:
        stacks = boto3.client("cloudformation", region_name=region, config=CFN_CONFIG).describe_stacks(
            StackName=stack_id)["Stacks"]
    except (ClientError, BotoCoreError, KeyError) as err:
        logger.warning("could not read the stack status (%s); probe teardown skipped", _code(err))
        return False
    status = stacks[0].get("StackStatus") if stacks else None
    if status != STACK_DELETING:
        logger.info("custom resource removed while the stack is %s (not a stack delete); probe teardown skipped", status)
        return False
    return True


# An opener that speaks only HTTPS: no file:, ftp: or plain http: handler, so the answer can only
# go to an https URL (CloudFormation's pre-signed ResponseURL).
_HTTPS_ONLY = urllib.request.OpenerDirector()
for _handler in (urllib.request.HTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
    _HTTPS_ONLY.add_handler(_handler)


def respond(event, status, reason="", data=None):
    """PUT the answer to the pre-signed URL CloudFormation gave (no AWS credentials involved). A
    ResponseURL that is not https is logged and nothing is sent (None)."""
    if urllib.parse.urlsplit(str(event.get("ResponseURL", ""))).scheme != "https":
        logger.error("ResponseURL is not an https URL; no answer sent")
        return None
    body = json.dumps({
        "Status": status, "Reason": reason[:1000] or "See the function's log",
        "PhysicalResourceId": event.get("PhysicalResourceId") or "probe-teardown",
        "StackId": event["StackId"], "RequestId": event["RequestId"], "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False, "Data": data or {},
    }).encode()
    request = urllib.request.Request(event["ResponseURL"], data=body, method="PUT",
                                     headers={"Content-Type": "", "Content-Length": str(len(body))})
    with _HTTPS_ONLY.open(request, timeout=10) as answer:
        return answer.status


def handler(event, context, sleep=time.sleep):
    """Always answers SUCCESS (failures are logged), so the stack delete is never blocked."""
    reason = ""
    try:
        if event.get("RequestType") == "Delete" and stack_deleting(event):
            if SETTLE_SECONDS:
                logger.info("waiting %d s for test launches already running to end", SETTLE_SECONDS)
                sleep(SETTLE_SECONDS)
            left_ms = context.get_remaining_time_in_millis() if context else 300000
            result = teardown(time.monotonic() + left_ms / 1000 - RESERVE_SECONDS)
            if result["left"] or result["failedRegions"] or result.get("regionDiscoveryFailed"):
                reason = f"probe teardown left {result['left']} and failed in {result['failedRegions']}"
                if result.get("regionDiscoveryFailed"):
                    reason = f"region discovery failed; cleaned {HOME_REGION} only; {reason}"
                logger.warning(reason)
            else:
                logger.info("probe teardown removed every probe security group and launch template")
    except Exception as err:  # noqa: BLE001 - the stack delete must go on
        logger.exception("probe teardown failed")
        reason = f"probe teardown failed ({type(err).__name__}); see the log"
    respond(event, "SUCCESS", reason)
