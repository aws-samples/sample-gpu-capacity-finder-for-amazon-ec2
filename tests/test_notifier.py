"""Notifier run against botocore Stubbers for EC2, SNS, and DynamoDB."""
import json
import os
from datetime import datetime, timezone
from unittest.mock import ANY

os.environ.update(SPOT_SCORE_FAMILIES="p5.*,g7*")

import boto3  # noqa: E402
import cache_fakes  # noqa: E402
import families  # noqa: E402
import notifier  # noqa: E402
import pytest  # noqa: E402
import score_groups  # noqa: E402
import spot_cache  # noqa: E402
import spot_configs  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

REGIONS = ["us-east-1", "us-west-2"]

TOPIC = "arn:aws:sns:us-east-1:123456789012:alerts"


def creds(service, region="us-east-1"):
    return boto3.client(service, region_name=region, aws_access_key_id="x", aws_secret_access_key="x")


class FakeCache:
    """The API's cache table: get_item and put_item by cacheKey, with conditional writes."""

    def __init__(self):
        self.items = {}
        self.fail_reads = False
        self.puts = []

    def get_item(self, Key, ConsistentRead=False):
        if self.fail_reads is True and Key["cacheKey"].startswith("sps#") or self.fail_reads == "all":
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")
        item = self.items.get(Key["cacheKey"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        cache_fakes.conditional_put(self.items, Item, ConditionExpression, ExpressionAttributeValues)
        self.puts.append(Item["cacheKey"])


def epoch():
    return int(datetime.now(timezone.utc).timestamp())


# The GPU count of the types these tests use: p5.48xlarge is p5/8, g7e.2xlarge is g7e/1.
GPUS = {"p5.48xlarge": 8, "g7e.2xlarge": 1, "g7e.48xlarge": 8}


def group(cache, itype, chunks=(REGIONS,)):
    """Store the score updater's group of the type with the given chunks; returns the group key."""
    key = score_groups.group_key(itype, GPUS)
    item = cache.items.get(score_groups.STATE_KEY)
    groups = json.loads(item["groups"]) if item else {}
    groups[key] = {"types": [t for t in GPUS if score_groups.group_key(t, GPUS) == key],
                   "chunks": [list(c) for c in chunks], "added": {}}
    cache.items[score_groups.STATE_KEY] = {"cacheKey": score_groups.STATE_KEY, "version": 1,
                                           "families": json.dumps(["g7*", "p5.*"]), "groups": json.dumps(groups),
                                           "gpus": json.dumps(GPUS), "offered": "{}", "dailyAt": 1, "updatedAt": 1}
    return key


def warm(cache, itype, scores, regions=REGIONS):
    """The score updater's group of the type and its warm cache item (the latest answer)."""
    key = group(cache, itype, [regions])
    spot_cache.write(cache, key, list(regions), scores, "2033-01-01T00:00:00.000000Z", epoch(), 40)
    return key


@pytest.fixture
def cache(monkeypatch):
    fake = FakeCache()
    cache_fakes.seeded(fake.items)
    monkeypatch.setattr(notifier, "_cache_table", fake)
    return fake


@pytest.fixture
def aws(monkeypatch, cache):
    monkeypatch.setattr(notifier, "REGIONS", ["us-east-1", "us-west-2"])
    monkeypatch.setattr(notifier, "SPOT_SCORE_FAMILIES", ["p5.*", "g7*"])
    monkeypatch.setattr(notifier, "TOPIC_ARN", TOPIC)
    monkeypatch.setattr(notifier, "SITE_URL", "https://example.cloudfront.net/")
    monkeypatch.setattr(notifier, "_clients", {})
    t = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x").Table("subs")
    monkeypatch.setattr(notifier, "_table", t)
    stubs = {"ddb": Stubber(t.meta.client)}
    for region in ("us-east-1", "us-west-2"):
        c = creds("ec2", region)
        notifier._clients[("ec2", region)] = c
        stubs[region] = Stubber(c)
    c = creds("sns")
    notifier._clients[("sns", None)] = c
    stubs["sns"] = Stubber(c)
    yield stubs
    for s in stubs.values():
        s.assert_no_pending_responses()


def row(user, region, itype, signal, value, last, count=None):
    name = "days" if signal == "capacity-block" else "threshold"
    sk = f"{region}#{itype}#{signal}#{value}" + (f"#{count}" if count not in (None, 1) else "")
    item = {"userId": {"S": user}, "sk": {"S": sk}, "region": {"S": region},
            "instanceType": {"S": itype}, "signal": {"S": signal}, name: {"N": str(value)}, "lastState": {"S": last}}
    if count is not None:
        item["count"] = {"N": str(count)}
    return item


def profile(user):
    return {"userId": {"S": user}, "sk": {"S": "#profile"}, "email": {"S": "a@example.com"},
            "subscriptionArn": {"S": f"{TOPIC}:{user}"}}


def expect_confirmed(sns, user, pending="false"):
    sns.add_response("get_subscription_attributes", {"Attributes": {"PendingConfirmation": pending}},
                     {"SubscriptionArn": f"{TOPIC}:{user}"})


def expect_update(ddb, user, sk, state, reason=None):
    if reason:
        expression, values = "SET lastState = :s, lastCheckedAt = :t, lastReason = :r", {":s": state, ":t": ANY, ":r": reason}
    else:
        expression, values = "SET lastState = :s, lastCheckedAt = :t REMOVE lastReason", {":s": state, ":t": ANY}
    ddb.add_response("update_item", {}, {
        "TableName": "subs", "Key": {"userId": user, "sk": sk},
        "UpdateExpression": expression, "ConditionExpression": "attribute_exists(sk)",
        "ExpressionAttributeValues": values})


def expect_checked(ddb, user, sk):
    """Only lastCheckedAt: the state stays for a user whose email is not confirmed."""
    ddb.add_response("update_item", {}, {
        "TableName": "subs", "Key": {"userId": user, "sk": sk}, "UpdateExpression": "SET lastCheckedAt = :t",
        "ConditionExpression": "attribute_exists(sk)", "ExpressionAttributeValues": {":t": ANY}})


def expect_forget(ddb, user):
    """The removed subscription's ARN is cleared, only if the profile still holds that ARN."""
    ddb.add_response("update_item", {}, {
        "TableName": "subs", "Key": {"userId": user, "sk": "#profile"}, "UpdateExpression": "REMOVE subscriptionArn",
        "ConditionExpression": "subscriptionArn = :a", "ExpressionAttributeValues": {":a": f"{TOPIC}:{user}"}})


def expect_publish(sns, user):
    sns.add_response("publish", {"MessageId": "m"}, {
        "TopicArn": TOPIC, "Subject": ANY, "Message": ANY,
        "MessageAttributes": {"userId": {"DataType": "String", "StringValue": user}}})


def activate(aws):
    for s in aws.values():
        s.activate()


def test_one_query_per_signal_and_publish_only_on_transition(aws):
    ddb = aws["ddb"]
    ddb.add_response("scan", {"Items": [
        profile("sub-alice"),
        row("sub-alice", "us-east-1", "p5.48xlarge", "spot-score", 7, "unavailable"),
        row("sub-bob", "us-east-1", "p5.48xlarge", "spot-score", 9, "pending"),
    ], "LastEvaluatedKey": {"userId": {"S": "sub-bob"}, "sk": {"S": "x"}}}, {"TableName": "subs"})
    ddb.add_response("scan", {"Items": [
        row("sub-carol", "us-east-1", "p5.48xlarge", "capacity-block", 2, "available"),
        row("sub-dave", "us-west-2", "p5.48xlarge", "capacity-block", 1, "pending"),
        row("sub-erin", "eu-west-1", "p5.48xlarge", "spot-score", 5, "pending"),  # Region no longer enabled
    ]}, {"TableName": "subs", "ExclusiveStartKey": {"userId": "sub-bob", "sk": "x"}})
    # Spot group: one score call for both thresholds (a repeat of a configuration EC2 accepted).
    warm(notifier._cache_table, "p5.48xlarge", {"us-east-1": 8})
    expect_confirmed(aws["sns"], "sub-alice")
    expect_publish(aws["sns"], "sub-alice")
    expect_update(ddb, "sub-alice", "us-east-1#p5.48xlarge#spot-score#7", "available")
    expect_update(ddb, "sub-bob", "us-east-1#p5.48xlarge#spot-score#9", "unavailable")
    # Capacity Block group in us-east-1: still available, no email.
    offer = {"CapacityBlockOfferingId": "cb-1", "InstanceType": "p5.48xlarge", "AvailabilityZone": "us-east-1a",
             "InstanceCount": 1, "StartDate": datetime(2030, 1, 2, tzinfo=timezone.utc),
             "EndDate": datetime(2030, 1, 4, tzinfo=timezone.utc), "CapacityBlockDurationHours": 48}
    # No date window: EC2 rejects an end date more than about nine days ahead.
    aws["us-east-1"].add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": [offer]}, {
        "InstanceType": "p5.48xlarge", "InstanceCount": 1, "CapacityDurationHours": 48})
    expect_update(ddb, "sub-carol", "us-east-1#p5.48xlarge#capacity-block#2", "available")
    # Capacity Block group in us-west-2: fails, isolated from the others.
    aws["us-west-2"].add_client_error("describe_capacity_block_offerings", "RequestLimitExceeded", http_status_code=503)
    activate(aws)
    counts = notifier.handler({}, None)
    assert counts == {"subscriptions": 5, "skipped": 1, "groups": 3, "failedGroups": 1, "notified": 1,
                      "unconfirmed": 0, "rejected": 0, "failedItems": 0, "spotUnknown": 0}


def test_publish_failure_keeps_state_for_retry(aws):
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"),
                                               row("sub-alice", "us-west-2", "g7e.2xlarge", "capacity-block", 1, "unavailable")]})
    aws["us-west-2"].add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": [
        {"AvailabilityZone": "us-west-2b", "StartDate": datetime(2030, 1, 2, tzinfo=timezone.utc)}]})
    expect_confirmed(aws["sns"], "sub-alice")
    aws["sns"].add_client_error("publish", "Throttled", http_status_code=400)
    activate(aws)  # no update_item queued: the state stays unavailable
    assert notifier.handler({}, None)["failedItems"] == 1


def test_deleted_alert_is_not_recreated(aws):
    aws["ddb"].add_response("scan", {"Items": [row("sub-alice", "us-east-1", "p5.48xlarge", "spot-score", 9, "pending")]})
    warm(notifier._cache_table, "p5.48xlarge", {"us-east-1": 2})
    aws["ddb"].add_client_error("update_item", "ConditionalCheckFailedException", http_status_code=400)
    activate(aws)
    counts = notifier.handler({}, None)
    assert counts["failedItems"] == 0 and counts["notified"] == 0


def test_pending_subscription_keeps_state_until_confirmed(aws):
    # Alice has not confirmed; Bob unsubscribed (the subscription is gone); Carol has no profile.
    aws["ddb"].add_response("scan", {"Items": [
        profile("sub-alice"), profile("sub-bob"),
        row("sub-alice", "us-east-1", "p5.48xlarge", "spot-score", 5, "pending"),
        row("sub-alice", "us-east-1", "p5.48xlarge", "spot-score", 6, "unavailable"),
        row("sub-bob", "us-east-1", "p5.48xlarge", "spot-score", 5, "pending"),
        row("sub-carol", "us-east-1", "p5.48xlarge", "spot-score", 5, "pending"),
    ]})
    warm(notifier._cache_table, "p5.48xlarge", {"us-east-1": 9})
    expect_confirmed(aws["sns"], "sub-alice", pending="true")  # read once for both of Alice's alerts
    expect_checked(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5")
    expect_checked(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#6")
    aws["sns"].add_client_error("get_subscription_attributes", "NotFound", http_status_code=404)
    expect_forget(aws["ddb"], "sub-bob")
    expect_checked(aws["ddb"], "sub-bob", "us-east-1#p5.48xlarge#spot-score#5")
    expect_checked(aws["ddb"], "sub-carol", "us-east-1#p5.48xlarge#spot-score#5")
    activate(aws)  # no publish, and no state change: only the check time moves
    counts = notifier.handler({}, None)
    assert (counts["notified"], counts["unconfirmed"], counts["failedItems"]) == (0, 4, 0)


def test_confirmed_later_sends_the_first_email(aws):
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"),
                                               row("sub-alice", "us-east-1", "p5.48xlarge", "spot-score", 5, "pending")]})
    warm(notifier._cache_table, "p5.48xlarge", {"us-east-1": 9})
    expect_confirmed(aws["sns"], "sub-alice")
    expect_publish(aws["sns"], "sub-alice")
    expect_update(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5", "available")
    activate(aws)
    assert notifier.handler({}, None)["notified"] == 1


def test_rejected_query_stores_rejected_with_the_reason_and_sends_no_email(aws, caplog):
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"),
                                               row("sub-alice", "us-east-1", "g7e.2xlarge", "capacity-block", 1, "pending"),
                                               row("sub-bob", "us-east-1", "g7e.2xlarge", "capacity-block", 1, "available")]})
    aws["us-east-1"].add_client_error(
        "describe_capacity_block_offerings", "InvalidParameterValue", http_status_code=400,
        service_message="The instance type 'g7e.2xlarge' is not supported for Capacity Blocks in 'us-east-1'. "
                        "Specify a compatible instance type and try again.",
        expected_params={"InstanceType": "g7e.2xlarge", "InstanceCount": 1, "CapacityDurationHours": 24})
    expect_update(aws["ddb"], "sub-alice", "us-east-1#g7e.2xlarge#capacity-block#1", "rejected", "cb-unsupported-type")
    expect_update(aws["ddb"], "sub-bob", "us-east-1#g7e.2xlarge#capacity-block#1", "rejected", "cb-unsupported-type")
    activate(aws)  # no subscription read and no publish queued
    with caplog.at_level("WARNING"):
        counts = notifier.handler({}, None)
    assert (counts["failedGroups"], counts["notified"], counts["rejected"], counts["failedItems"]) == (0, 0, 2, 0)
    rejected = [r for r in caplog.records if "rejected" in r.getMessage()]
    assert len(rejected) == 1 and "cb-unsupported-type" in rejected[0].getMessage() and not rejected[0].exc_info


def spot_rows(*specs):
    return [row(user, region, itype, "spot-score", value, last) for user, region, itype, value, last in specs]


def test_spot_alerts_read_one_cached_answer_per_group_across_regions(aws, cache):
    """Alerts for one type in two Regions read the one cached answer of the group's
    configuration; each alert reads its own Region's score. No GetSpotPlacementScores call."""
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"), *spot_rows(
        ("sub-alice", "us-east-1", "p5.48xlarge", 5, "unavailable"),
        ("sub-alice", "us-west-2", "p5.48xlarge", 5, "unavailable"))]})
    warm(cache, "p5.48xlarge", {"us-east-1": 3, "us-west-2": 8})
    expect_update(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5", "unavailable")
    expect_confirmed(aws["sns"], "sub-alice")
    expect_publish(aws["sns"], "sub-alice")
    expect_update(aws["ddb"], "sub-alice", "us-west-2#p5.48xlarge#spot-score#5", "available")
    activate(aws)  # no get_spot_placement_scores queued
    reads = []
    cache.puts.clear()
    real = cache.get_item
    cache.get_item = lambda Key, **kw: reads.append(Key["cacheKey"]) or real(Key, **kw)
    counts = notifier.handler({}, None)
    assert (counts["groups"], counts["failedGroups"], counts["notified"]) == (2, 0, 1)
    assert reads.count(spot_cache.cache_key("p5/8", REGIONS)) == 1 and reads.count(score_groups.STATE_KEY) == 1
    # The notifier writes no score item and no history.
    assert not [k for k in cache.puts if k.startswith(("sps#", "spshist#", "spscfg#", "#sps-groups"))]


def test_spot_alerts_use_the_chunk_that_holds_their_region(aws, cache):
    group(cache, "p5.48xlarge", chunks=[["us-east-1"], ["us-west-2"]])
    spot_cache.write(cache, "p5/8", ["us-west-2"], {"us-west-2": 9}, "t1", epoch(), 40)
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"), *spot_rows(
        ("sub-alice", "us-west-2", "p5.48xlarge", 7, "available"),
        ("sub-bob", "us-east-1", "p5.48xlarge", 1, "available"))]})
    expect_update(aws["ddb"], "sub-alice", "us-west-2#p5.48xlarge#spot-score#7", "available")
    # No current answer for us-east-1's chunk: unknown, not "not available"; Bob's state stays.
    expect_checked(aws["ddb"], "sub-bob", "us-east-1#p5.48xlarge#spot-score#1")
    activate(aws)
    counts = notifier.handler({}, None)
    assert (counts["failedGroups"], counts["spotUnknown"], counts["notified"]) == (0, 1, 0)


def test_the_email_names_the_score_group(aws, cache):
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"), *spot_rows(
        ("sub-alice", "us-east-1", "p5.48xlarge", 5, "pending"))]})
    warm(cache, "p5.48xlarge", {"us-east-1": 9})
    expect_confirmed(aws["sns"], "sub-alice")
    aws["sns"].add_response("publish", {"MessageId": "m"}, {
        "TopicArn": TOPIC, "Subject": "Spot score 9: p5 · 8 GPUs in us-east-1", "Message": ANY,
        "MessageAttributes": {"userId": {"DataType": "String", "StringValue": "sub-alice"}}})
    expect_update(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5", "available")
    activate(aws)
    assert notifier.handler({}, None)["notified"] == 1


def test_an_unreadable_cache_is_unknown_and_calls_nothing(aws, cache):
    warm(cache, "p5.48xlarge", {"us-east-1": 9, "us-west-2": 9})
    cache.fail_reads = True  # a failed score item read is a miss
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"), *spot_rows(
        ("sub-alice", "us-east-1", "p5.48xlarge", 5, "pending"))]})
    expect_checked(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5")
    activate(aws)
    counts = notifier.handler({}, None)
    assert (counts["spotUnknown"], counts["failedGroups"]) == (1, 0)


def test_removed_subscription_is_cleared_once(aws):
    """SNS no longer knows the ARN: the profile loses it, so the next POST /api/subscriptions subscribes again."""
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"), *spot_rows(
        ("sub-alice", "us-east-1", "p5.48xlarge", 5, "pending"),
        ("sub-alice", "us-west-2", "p5.48xlarge", 5, "pending"))]})
    warm(notifier._cache_table, "p5.48xlarge", {"us-east-1": 9, "us-west-2": 9})
    aws["sns"].add_client_error("get_subscription_attributes", "NotFound", http_status_code=404)
    expect_forget(aws["ddb"], "sub-alice")
    expect_checked(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5")
    expect_checked(aws["ddb"], "sub-alice", "us-west-2#p5.48xlarge#spot-score#5")
    activate(aws)
    counts = notifier.handler({}, None)
    assert (counts["unconfirmed"], counts["notified"], counts["failedItems"]) == (2, 0, 0)


def test_capacity_block_alert_queries_with_its_count_and_days(aws):
    """An alert for 8 instances for 3 days asks EC2 for 8 instances and 72 hours; an old alert
    without a count asks for 1."""
    aws["ddb"].add_response("scan", {"Items": [
        profile("sub-alice"),
        row("sub-alice", "us-east-1", "p5.48xlarge", "capacity-block", 3, "unavailable", count=8),
        row("sub-alice", "us-west-2", "p5.48xlarge", "capacity-block", 3, "unavailable"),
    ]})
    offer = {"AvailabilityZone": "us-east-1a", "InstanceCount": 8, "StartDate": datetime(2030, 1, 2, tzinfo=timezone.utc)}
    aws["us-east-1"].add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": [offer]}, {
        "InstanceType": "p5.48xlarge", "InstanceCount": 8, "CapacityDurationHours": 72})
    aws["us-west-2"].add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": []}, {
        "InstanceType": "p5.48xlarge", "InstanceCount": 1, "CapacityDurationHours": 72})
    expect_confirmed(aws["sns"], "sub-alice")
    aws["sns"].add_response("publish", {"MessageId": "m"}, {
        "TopicArn": TOPIC, "Subject": ANY, "Message": ANY,
        "MessageAttributes": {"userId": {"DataType": "String", "StringValue": "sub-alice"}}})
    expect_update(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#capacity-block#3#8", "available")
    expect_update(aws["ddb"], "sub-alice", "us-west-2#p5.48xlarge#capacity-block#3", "unavailable")
    activate(aws)
    sent = []
    aws["sns"].client.meta.events.register("provide-client-params.sns.Publish", lambda params, **_: sent.append(params["Message"]))
    counts = notifier.handler({}, None)
    assert (counts["groups"], counts["notified"], counts["failedItems"]) == (2, 1, 0)
    assert "8 x p5.48xlarge, 3 day(s)" in sent[0]


@pytest.mark.parametrize("message,reason", [
    ("The requested duration is not valid for this instance type.", "cb-invalid-duration"),
    ("The requested instance count is not valid for this instance type.", "cb-invalid-count"),
])
def test_rejected_duration_or_count_keeps_its_reason(aws, message, reason):
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"),
                                               row("sub-alice", "us-east-1", "p5.48xlarge", "capacity-block", 3, "pending", count=5)]})
    aws["us-east-1"].add_client_error("describe_capacity_block_offerings", "InvalidParameterValue", http_status_code=400,
                                      service_message=message)
    expect_update(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#capacity-block#3#5", "rejected", reason)
    activate(aws)
    counts = notifier.handler({}, None)
    assert (counts["rejected"], counts["notified"], counts["failedGroups"]) == (1, 0, 0)


def test_rejected_alert_that_ec2_accepts_again_emails_once(aws):
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"),
                                               row("sub-alice", "us-east-1", "p5.48xlarge", "capacity-block", 2, "rejected")]})
    aws["us-east-1"].add_response("describe_capacity_block_offerings", {"CapacityBlockOfferings": [
        {"AvailabilityZone": "us-east-1a", "StartDate": datetime(2030, 1, 2, tzinfo=timezone.utc)}]})
    expect_confirmed(aws["sns"], "sub-alice")
    expect_publish(aws["sns"], "sub-alice")
    # The available state clears the old reason.
    expect_update(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#capacity-block#2", "available")
    activate(aws)
    assert notifier.handler({}, None)["notified"] == 1


# ------------------------------------------------- unknown scores (no request at all)
def test_without_a_stored_group_the_alert_is_unknown(aws, cache):
    """No group for the type yet (before the bootstrap, or a size the daily update has not seen):
    no request, no email, the state stays (here: available, so a later score would not email again)."""
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"), *spot_rows(
        ("sub-alice", "us-east-1", "p5.48xlarge", 5, "available"),
        ("sub-bob", "us-west-2", "g7e.2xlarge", 5, "unavailable"))]})
    expect_checked(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5")
    expect_checked(aws["ddb"], "sub-bob", "us-west-2#g7e.2xlarge#spot-score#5")
    activate(aws)  # no EC2 call, no publish queued
    counts = notifier.handler({}, None)
    assert (counts["spotUnknown"], counts["notified"], counts["failedGroups"]) == (2, 0, 0)
    assert spot_configs.read_budget(cache)["new24h"] == [] and not any(k.startswith("spscfg#") for k in cache.items)


def test_an_expired_answer_is_unknown(aws, cache):
    key = group(cache, "p5.48xlarge")
    spot_cache.write(cache, key, REGIONS, {"us-east-1": 9}, "t0", epoch() - 3 * 3600, 40)
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"), *spot_rows(
        ("sub-alice", "us-east-1", "p5.48xlarge", 5, "pending"))]})
    expect_checked(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5")
    activate(aws)
    assert notifier.handler({}, None)["spotUnknown"] == 1


def test_a_missing_score_never_flips_an_available_alert_or_sends_a_second_email(aws, cache):
    """EC2 left the alert's Region out of the answer: that is not "not available", so the state
    stays available and the next score sends no second email."""
    warm(cache, "p5.48xlarge", {"us-west-2": 8})
    aws["ddb"].add_response("scan", {"Items": [profile("sub-alice"), *spot_rows(
        ("sub-alice", "us-east-1", "p5.48xlarge", 5, "available"))]})
    expect_checked(aws["ddb"], "sub-alice", "us-east-1#p5.48xlarge#spot-score#5")
    activate(aws)
    counts = notifier.handler({}, None)
    assert (counts["spotUnknown"], counts["notified"]) == (1, 0)


def test_the_ec2_clients_have_short_timeouts_and_bounded_retries(monkeypatch):
    """One slow Region cannot hold a run near its timeout: the same EC2 settings as the other functions."""
    monkeypatch.setattr(notifier, "_clients", {})
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    cfg = notifier.client("ec2", "us-east-1").meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (3, 15)
    assert cfg.retries == {"mode": "standard", "total_max_attempts": 3}
