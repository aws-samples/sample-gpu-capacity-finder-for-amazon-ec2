"""Scheduled alert check: query each subscribed signal once and email on a
not available -> available transition. Read-only EC2 calls; never launches or reserves.
A Capacity Block query EC2 rejects (an unsupported type, duration, or count) stores the state
"rejected" with EC2's reason code and sends no email.

Spot score alerts check the score of the alert type's score group (its family and GPU count, any
size of the group, as the page shows it). They read the score updater's warm cache only (refreshed
every 15 minutes) and never call GetSpotPlacementScores. Without a current answer for the group's
configuration that holds the Region, or without the alert's Region in the answer, the result is
"unknown": the alert keeps its state, gets only a new check time, and no email goes out."""
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import alerts
import capacity_blocks as cb
import families
import regions as region_list
import score_groups
import spot_cache

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The function's own Region: DescribeRegions is asked there, and it is the Region list while the
# shared enabled-Region list has no answer. Alerts may target any enabled Region (enabled_regions).
HOME_REGION = os.environ.get("AWS_REGION") or score_groups.ENDPOINT
REGIONS = [HOME_REGION]
# The families that get Spot placement scores (spotScoreFamilies, lambda/families.py); the default
# list when the setting is missing.
SPOT_SCORE_FAMILIES = families.score_patterns_or_default(os.environ.get("SPOT_SCORE_FAMILIES", "").split(","))
TABLE_NAME = os.environ.get("SUBSCRIPTIONS_TABLE_NAME", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
SITE_URL = os.environ.get("SITE_URL", "")
# The shared cache table: Spot score alerts read the score updater's items.
OFFERING_CACHE_TABLE_NAME = os.environ.get("OFFERING_CACHE_TABLE_NAME", "")
MAX_OFFERINGS = 50
# The same EC2 client settings as the other functions: one slow Region cannot hold a run for long.
EC2_CONFIG = Config(retries={"mode": "standard", "total_max_attempts": 3}, connect_timeout=3, read_timeout=15)


class Rejected:
    """EC2 refused a Capacity Block query itself; `reason` is the machine code (capacity_blocks.error_code)."""

    def __init__(self, reason):
        self.reason = reason

_clients = {}
_table = None
_cache_table = None


def client(service, region=None):
    key = (service, region)
    if key not in _clients:
        options = {"config": EC2_CONFIG} if service == "ec2" else {}
        _clients[key] = boto3.client(service, region_name=region, **options) if region else boto3.client(service, **options)
    return _clients[key]


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def cache_table():
    global _cache_table
    if _cache_table is None:
        _cache_table = boto3.resource("dynamodb").Table(OFFERING_CACHE_TABLE_NAME)
    return _cache_table


def _epoch():
    return int(datetime.now(timezone.utc).timestamp())


def enabled_regions():
    """The enabled Regions minus the excluded ones, from the shared regions#enabled item (same
    lock, same refresh, same fallback to the stale list, then to the function's own Region).
    Never raises."""
    try:
        return region_list.resolve(cache_table(), lambda: region_list.describe(client("ec2", HOME_REGION)),
                                   REGIONS, _epoch())[0]
    except Exception:  # noqa: BLE001 - the run goes on with the function's own Region
        logger.warning("Region discovery failed; using the function's own Region", exc_info=True)
        return region_list.order(REGIONS)


# The key in fetch's per-run `spot` map that keeps the stored score groups (score_groups, read once).
STATE = "#state"


def spot_score(state, region, itype, spot, now):
    """The alert type's group score in the Region from the warm cache, or None (unknown): no stored
    group for the type, the Region in none of the group's configurations, no current answer, or
    the Region missing from it. `spot` keeps each configuration's cached answer for the run."""
    key = score_groups.group_key(itype, (state or {}).get("gpus"))
    cfg = score_groups.config_for((state or {}).get("groups"), key, region) if key else None
    if cfg is None:
        return None
    if cfg.fp not in spot:
        cached = spot_cache.read(cache_table(), cfg.itype, list(cfg.regions), now)
        spot[cfg.fp] = cached["scores"] if cached is not None else None
    return None if spot[cfg.fp] is None else spot[cfg.fp].get(region)


def group_label(itype, spot):
    """The label of the alert type's score group ("g6e · 4 GPUs"), or None when unknown."""
    key = score_groups.group_key(itype, (spot.get(STATE) or {}).get("gpus"))
    return score_groups.label(key) if key else None


def scan_items():
    items, kwargs = [], {}
    while True:
        page = table().scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def fetch(key, spot=None, regions=None):
    """Query EC2 once for a group. Returns offerings or a score (see alerts.is_available), None
    for a Spot score that is unknown this run, or Rejected when EC2 refused the Capacity Block query.
    `spot` keeps the stored score groups and each configuration's cached answer for this run, so
    every Region's Spot alerts answered by one configuration share one cache read. `regions` is
    not needed for Spot scores (the groups hold their Regions) and kept for the callers."""
    signal, region, itype = key[:3]
    if signal == alerts.CAPACITY_BLOCK:
        days, count = key[3], key[4]
        ec2 = client("ec2", region)
        # Same call as the page's search for the alert's count and days: no date window, so EC2
        # searches its own range.
        try:
            return cb.search(ec2, itype, count, days * 24, max_items=MAX_OFFERINGS)[0]
        except cb.OfferingError as err:
            # EC2 rejected the request (for example a type without Capacity Blocks). One log line
            # for the group, no stack trace, and the other groups go on.
            logger.warning("capacity block check rejected for %s: %s (%s)", key, err.message, err.code)
            return Rejected(err.code)
    spot = {} if spot is None else spot
    if STATE not in spot:
        spot[STATE] = score_groups.read_state(cache_table())
    return spot_score(spot[STATE], region, itype, spot, _epoch())


def publish(item, result, label=None):
    subject, body = alerts.message(item, result, SITE_URL, label)
    client("sns").publish(
        TopicArn=TOPIC_ARN,
        Subject=subject,
        Message=body,
        # The subscription filter policy matches this attribute, so only the owner gets the email.
        MessageAttributes={"userId": {"DataType": "String", "StringValue": item["userId"]}},
    )


def confirmed(profile):
    """True when the user's email subscription is confirmed. SNS drops messages to a
    pending subscription, so a pending one must not use up the transition."""
    arn = (profile or {}).get("subscriptionArn")
    if not arn:
        return False
    try:
        attrs = client("sns").get_subscription_attributes(SubscriptionArn=arn)["Attributes"]
    except ClientError as err:
        # Removed (for example through the unsubscribe link): nothing to deliver to. Clear the
        # ARN, so the user's next new alert subscribes the address again.
        if err.response["Error"]["Code"] == "NotFound":
            forget_subscription(profile, arn)
            return False
        raise
    return attrs.get("PendingConfirmation") == "false"


def forget_subscription(profile, arn):
    try:
        table().update_item(
            Key={"userId": profile["userId"], "sk": alerts.PROFILE_SK},
            UpdateExpression="REMOVE subscriptionArn",
            # Only the ARN that SNS no longer knows; a newer one from the API stays.
            ConditionExpression="subscriptionArn = :a",
            ExpressionAttributeValues={":a": arn},
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("could not clear a removed subscription (%s)", err.response["Error"]["Code"])


def _update_alert(item, expression, values):
    try:
        table().update_item(
            Key={"userId": item["userId"], "sk": item["sk"]},
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(sk)",
            ExpressionAttributeValues=values,
        )
    except ClientError as err:
        # The user deleted the alert during this run: nothing to update.
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def save_state(item, state, now, reason=None):
    """Store the state and check time; lastReason only with the rejected state."""
    if reason is not None:
        _update_alert(item, "SET lastState = :s, lastCheckedAt = :t, lastReason = :r",
                      {":s": state, ":t": now, ":r": reason})
    else:
        _update_alert(item, "SET lastState = :s, lastCheckedAt = :t REMOVE lastReason", {":s": state, ":t": now})


def mark_checked(item, now):
    """Only the check time: the state stays, so the first email still goes out after confirmation."""
    _update_alert(item, "SET lastCheckedAt = :t", {":t": now})


def handler(_event, _context):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = scan_items()
    profiles = {i["userId"]: i for i in items if i.get("sk") == alerts.PROFILE_SK}
    subs = [i for i in items if alerts.is_subscription(i)]
    status = {}  # confirmation state per user, read once per run
    regions = enabled_regions()
    valid = [i for i in subs if alerts.is_valid(i, regions, SPOT_SCORE_FAMILIES)]
    groups = alerts.group(valid)
    counts = {"subscriptions": len(subs), "skipped": len(subs) - len(valid), "groups": len(groups),
              "failedGroups": 0, "notified": 0, "unconfirmed": 0, "rejected": 0, "failedItems": 0, "spotUnknown": 0}
    spot = {}  # scores (or the error) per Spot score configuration, fetched once per run
    for key, members in groups.items():
        try:
            result = fetch(key, spot, regions)
        except (ClientError, BotoCoreError) as err:
            # One failing signal (throttling, an unsupported type) does not stop the others.
            code = err.response["Error"]["Code"] if isinstance(err, ClientError) else type(err).__name__
            logger.warning("signal check failed for %s (%s)", key, code)
            counts["failedGroups"] += 1
            continue
        if isinstance(result, Rejected):
            for item in members:
                try:
                    save_state(item, alerts.REJECTED, now, result.reason)
                    counts["rejected"] += 1
                except (ClientError, BotoCoreError):
                    logger.exception("alert update failed for %s", item["sk"])
                    counts["failedItems"] += 1
            continue
        for item, state, notify in alerts.transitions(members, result):
            try:
                if state == alerts.UNKNOWN:
                    # No score this run: keep the state (never "not available"), no email.
                    counts["spotUnknown"] += 1
                    mark_checked(item, now)
                    continue
                if notify:
                    user = item["userId"]
                    if user not in status:
                        status[user] = confirmed(profiles.get(user))
                    if not status[user]:
                        # Keep the old state, so the email goes out after confirmation; the
                        # check time still moves, so the page shows the alert is being checked.
                        counts["unconfirmed"] += 1
                        mark_checked(item, now)
                        continue
                    publish(item, result, group_label(item["instanceType"], spot) if key[0] == alerts.SPOT_SCORE else None)
                    counts["notified"] += 1
                # Saved after a successful publish, so a failed email is retried next run.
                save_state(item, state, now)
            except (ClientError, BotoCoreError):
                logger.exception("alert update failed for %s", item["sk"])
                counts["failedItems"] += 1
    logger.info("alert check: %s", json.dumps(counts))
    return counts
