"""Safety net: terminate probe instances that outlived their launch call, and delete probe launch
templates that a failed delete left behind."""
import logging
import os
from datetime import datetime, timedelta, timezone

from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Test launches run in every enabled Region minus excludeRegions, so the cleanup scans the same list:
# DescribeRegions in the function's own Region on every run, without the excluded Regions.
HOME_REGION = os.environ.get("AWS_REGION") or "us-east-1"
EXCLUDED = frozenset(r for r in os.environ.get("EXCLUDE_REGIONS", "").split(",") if r)
PROBE_TAG = "gpu-capacity-finder-probe"
MAX_AGE = timedelta(minutes=10)
# A test launch deletes its launch template right after the fleet answer; one older than this was
# left behind by a failed delete.
TEMPLATE_MAX_AGE = timedelta(hours=1)
# A Region whose endpoint does not answer must not hold the run for botocore's default 60 seconds.
EC2_CONFIG = Config(retries={"mode": "standard", "total_max_attempts": 2}, connect_timeout=3, read_timeout=15)
WORKERS = 8
TAG_FILTER = [{"Name": f"tag:{PROBE_TAG}", "Values": ["true"]}]


class CleanupFailed(Exception):
    """Every Region failed: raised so the invocation shows as failed."""


def target_regions():
    """The enabled commercial Regions minus the excluded ones (DescribeRegions without AllRegions);
    the function's own Region when the lookup fails. A failed lookup logs "probe cleanup failed in
    region-discovery", the line the Region-failure metric filter counts, so the alarm fires while
    the other Regions go unchecked."""
    try:
        resp = boto3.client("ec2", region_name=HOME_REGION, config=EC2_CONFIG).describe_regions(AllRegions=False)
        names = sorted({r["RegionName"] for r in resp["Regions"]} - EXCLUDED)
        if names:
            return names
    except (ClientError, BotoCoreError, KeyError) as err:
        logger.warning("probe cleanup failed in region-discovery (%s); cleaning %s only", _error_code(err), HOME_REGION)
    return [HOME_REGION]


def _old_instances(ec2, cutoff):
    ids = []
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[*TAG_FILTER, {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    )
    for page in pages:
        for reservation in page["Reservations"]:
            ids += [i["InstanceId"] for i in reservation["Instances"] if i["LaunchTime"] < cutoff]
    return ids


def _old_templates(ec2, cutoff):
    ids = []
    for page in ec2.get_paginator("describe_launch_templates").paginate(Filters=TAG_FILTER):
        ids += [t["LaunchTemplateId"] for t in page["LaunchTemplates"] if t["CreateTime"] < cutoff]
    return ids


def _clean_region(region, cutoff, template_cutoff):
    """(terminated instance ids, deleted template ids, the first error or None) of one Region. A
    failed terminate does not skip the Region's template deletion, and a failed template lookup or
    delete (caught per template) keeps the Region's terminate result; either error fails the
    Region (the next run tries again). A template that is already gone counts as deleted."""
    ec2 = boto3.client("ec2", region_name=region, config=EC2_CONFIG)
    ids = _old_instances(ec2, cutoff)
    terminate_error = None
    if ids:
        try:
            ec2.terminate_instances(InstanceIds=ids)
        except (ClientError, BotoCoreError) as err:
            logger.warning("terminate failed in %s for %s (%s)", region, ids, _error_code(err))
            terminate_error, ids = err, []
    deleted, error = [], terminate_error
    try:
        old = _old_templates(ec2, template_cutoff)
    except (ClientError, BotoCoreError, KeyError) as err:
        logger.warning("template lookup failed in %s (%s)", region, _error_code(err))
        old, error = [], error or err
    for template_id in old:
        try:
            ec2.delete_launch_template(LaunchTemplateId=template_id)
        except (ClientError, BotoCoreError) as err:
            if _error_code(err) not in ("InvalidLaunchTemplateId.NotFound", "InvalidLaunchTemplateName.NotFoundException"):
                logger.warning("template delete failed in %s for %s (%s)", region, template_id, _error_code(err))
                error = error or err
                continue
        deleted.append(template_id)
    return ids, deleted, error


def _error_code(err):
    if isinstance(err, ClientError):
        return err.response["Error"]["Code"]
    return type(err).__name__


def handler(_event, _context):
    now = datetime.now(timezone.utc)
    cutoff, template_cutoff = now - MAX_AGE, now - TEMPLATE_MAX_AGE
    regions = target_regions()

    def one(region):
        try:
            return region, _clean_region(region, cutoff, template_cutoff), None
        except (ClientError, BotoCoreError) as err:
            return region, None, err

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(regions))) as pool:
        results = list(pool.map(one, regions))
    terminated, templates, failed = {}, {}, []
    for region, done, err in results:
        if err is not None:
            # One Region (for example one that does not answer) must not stop the others.
            logger.warning("probe cleanup failed in %s (%s)", region, _error_code(err))
            failed.append(region)
            continue
        ids, deleted, region_error = done
        if region_error is not None:
            # The Region's other work was still done; the Region counts as failed (alarm).
            logger.warning("probe cleanup failed in %s (%s)", region, _error_code(region_error))
            failed.append(region)
        if ids:
            terminated[region] = ids
        if deleted:
            templates[region] = deleted
    logger.info("terminated leftover probe instances: %s; deleted leftover probe launch templates: %s; "
                "failed Regions: %s", terminated, templates, failed)
    if regions and len(failed) == len(regions):
        raise CleanupFailed(f"probe cleanup failed in every Region: {', '.join(failed)}")
    return {"terminated": terminated, "deletedTemplates": templates, "failedRegions": failed}
