"""Alert routes. SNS calls go through a botocore Stubber; one test also stubs the
DynamoDB client so the table request shapes are checked against the service model."""
import json
import os

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import boto3  # noqa: E402
import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402
from unittest.mock import ANY  # noqa: E402

TOPIC = "arn:aws:sns:us-east-1:123456789012:alerts"
SUB_ARN = TOPIC + ":0b1c2d3e-0000-4000-8000-000000000000"


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(api, "ENABLE_NOTIFICATIONS", True)
    monkeypatch.setattr(api, "TOPIC_ARN", TOPIC)
    monkeypatch.setattr(api, "MAX_SUBSCRIPTIONS_PER_USER", 2)
    monkeypatch.setattr(api, "REGIONS", ["us-east-1", "us-west-2"])
    monkeypatch.setattr(api, "region_offerings", lambda region: ["g7e.2xlarge", "p5.48xlarge"] if region == "us-east-1" else [])
    monkeypatch.setattr(api, "_clients", {})


def ccf(op):
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, op)


class FakeTable:
    """Alert table that evaluates the few condition expressions the API uses."""
    name = "subs"

    def __init__(self):
        self.items = {}
        self.before_write = None  # hook to simulate a concurrent request just before a profile write
        self.meta = type("Meta", (), {"client": type("Client", (), {
            "transact_write_items": lambda _, TransactItems: self.transact(TransactItems)})()})()

    @staticmethod
    def ok(cond, item, vals):
        if cond is None:
            return True
        count = (item or {}).get("alertCount")
        return {
            "attribute_not_exists(sk)": item is None,
            "attribute_exists(sk)": item is not None,
            "attribute_exists(sk) AND (attribute_not_exists(alertCount) OR alertCount < :max)":
                item is not None and (count is None or count < (vals or {}).get(":max", 0)),
            "attribute_not_exists(alertCount) OR alertCount <= :zero": count is None or count <= 0,
        }[cond]

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ConsistentRead):
        user = ExpressionAttributeValues[":u"]
        return {"Items": [dict(v) for (u, _), v in sorted(self.items.items()) if u == user]}

    def put_item(self, Item, ConditionExpression=None):
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook()
        key = (Item["userId"], Item["sk"])
        if not self.ok(ConditionExpression, self.items.get(key), None):
            raise ccf("PutItem")
        self.items[key] = dict(Item)

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        """Only the profile email change: SET email and subscriptionArn, fill a missing count."""
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook()
        assert UpdateExpression == "SET email = :e, subscriptionArn = :a, alertCount = if_not_exists(alertCount, :n)"
        key = (Key["userId"], Key["sk"])
        if not self.ok(ConditionExpression, self.items.get(key), None):
            raise ccf("UpdateItem")
        item = self.items[key]
        item.update(email=ExpressionAttributeValues[":e"], subscriptionArn=ExpressionAttributeValues[":a"])
        item.setdefault("alertCount", ExpressionAttributeValues[":n"])

    def delete_item(self, Key, ConditionExpression=None, ExpressionAttributeValues=None, ReturnValues=None):
        key = (Key["userId"], Key["sk"])
        if not self.ok(ConditionExpression, self.items.get(key), ExpressionAttributeValues):
            raise ccf("DeleteItem")
        old = self.items.pop(key, None)
        return {"Attributes": old} if old and ReturnValues == "ALL_OLD" else {}

    def transact(self, items):
        reasons, ops = [], []
        for entry in items:
            (op, spec), = entry.items()
            assert spec["TableName"] == "subs"
            src = spec.get("Item") or spec["Key"]
            key = (src["userId"], src["sk"])
            good = self.ok(spec.get("ConditionExpression"), self.items.get(key), spec.get("ExpressionAttributeValues"))
            reasons.append({"Code": "None" if good else "ConditionalCheckFailed"})
            ops.append((op, key, spec))
        if any(r["Code"] != "None" for r in reasons):
            raise ClientError({"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": reasons},
                              "TransactWriteItems")
        for op, key, spec in ops:
            if op == "Put":
                self.items[key] = dict(spec["Item"])
            elif op == "Delete":
                self.items.pop(key, None)
            else:
                vals = spec["ExpressionAttributeValues"]
                item = self.items[key]
                item["alertCount"] = item.get("alertCount", 0) + vals.get(":one", vals.get(":neg"))


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(api, "subscriptions_table", lambda: fake)
    return fake


@pytest.fixture
def sns():
    c = boto3.client("sns", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    api._clients[("sns", None)] = c
    s = Stubber(c)
    yield s
    s.assert_no_pending_responses()


def expect_subscribe(sns, sub="sub-alice", email="a@example.com", arn=SUB_ARN):
    sns.add_response("subscribe", {"SubscriptionArn": arn}, {
        "TopicArn": TOPIC, "Protocol": "email", "Endpoint": email,
        "Attributes": {"FilterPolicy": json.dumps({"userId": [sub]}), "FilterPolicyScope": "MessageAttributes"},
        "ReturnSubscriptionArn": True,
    })


def call(method, path, query=None, body=None, sub="sub-alice", email="a@example.com", verified="true"):
    claims = {"sub": sub, "email": email, "email_verified": verified, "token_use": "id"}
    ev = {"requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
          "rawPath": path, "queryStringParameters": query, "body": json.dumps(body) if body is not None else None}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


SPOT = {"region": "us-east-1", "instanceType": "p5.48xlarge", "signal": "spot-score", "threshold": 7}
BLOCK = {"region": "us-east-1", "instanceType": "p5.48xlarge", "signal": "capacity-block", "days": 2}


def test_config_exposes_notification_settings():
    _, body = call("GET", "/api/config")
    assert body["notificationsEnabled"] is True and body["maxSubscriptionsPerUser"] == 2
    assert "notifyIntervalMinutes" in body


def test_disabled_rejects_every_route(monkeypatch, table):
    monkeypatch.setattr(api, "ENABLE_NOTIFICATIONS", False)
    assert call("GET", "/api/subscriptions")[0] == 403
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 403
    assert call("DELETE", "/api/subscriptions", {"id": "us-east-1#p5.48xlarge#spot-score#7"})[0] == 403
    assert not table.items


def test_first_alert_subscribes_token_email_once(table, sns):
    expect_subscribe(sns)
    sns.activate()
    # An email or user ID in the body is ignored: both come from the token.
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "email": "evil@example.com", "userId": "sub-bob"})
    assert status == 200 and body["confirmationSent"] is True and body["email"] == "a@example.com"
    assert body["subscription"] == {"id": "us-east-1#p5.48xlarge#spot-score#7", "region": "us-east-1",
                                    "instanceType": "p5.48xlarge", "signal": "spot-score", "threshold": 7,
                                    "lastState": "pending", "state": "waiting", "reason": None,
                                    "lastCheckedAt": None, "createdAt": body["subscription"]["createdAt"]}
    assert table.items[("sub-alice", "#profile")]["subscriptionArn"] == SUB_ARN
    # Second alert: no new Subscribe call (the Stubber has no response queued).
    status, body = call("POST", "/api/subscriptions", body=BLOCK)
    assert status == 200 and body["confirmationSent"] is False
    status, body = call("GET", "/api/subscriptions")
    assert [s["id"] for s in body["subscriptions"]] == ["us-east-1#p5.48xlarge#capacity-block#2", "us-east-1#p5.48xlarge#spot-score#7"]
    assert body["email"] == "a@example.com" and body["max"] == 2


NEW_ARN = TOPIC + ":11111111-0000-4000-8000-000000000000"


def test_changed_token_email_subscribes_the_new_address(table, sns):
    expect_subscribe(sns)
    expect_subscribe(sns, email="new@example.com", arn=NEW_ARN)
    # The old address stops getting this user's alerts.
    sns.add_response("unsubscribe", {}, {"SubscriptionArn": SUB_ARN})
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    status, body = call("POST", "/api/subscriptions", body=BLOCK, email="new@example.com")
    assert status == 200 and body["confirmationSent"] is True
    profile = table.items[("sub-alice", "#profile")]
    assert (profile["email"], profile["subscriptionArn"], profile["alertCount"]) == ("new@example.com", NEW_ARN, 2)


def test_unsubscribe_failure_does_not_block_the_alert(table, sns):
    expect_subscribe(sns)
    expect_subscribe(sns, email="new@example.com", arn=NEW_ARN)
    sns.add_client_error("unsubscribe", "InvalidParameter")  # for example, a still-pending ARN
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    assert call("POST", "/api/subscriptions", body=BLOCK, email="new@example.com")[0] == 200


def test_count_on_profile_blocks_concurrent_adds(table, sns):
    # Another request added an alert after this one read the list: the transaction refuses.
    expect_subscribe(sns)
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    table.items[("sub-alice", "#profile")]["alertCount"] = 2
    status, body = call("POST", "/api/subscriptions", body=BLOCK)
    assert status == 409 and "limit is 2" in body["error"]
    assert ("sub-alice", "us-east-1#p5.48xlarge#capacity-block#2") not in table.items


def test_profile_removed_concurrently_is_recreated(table, sns, monkeypatch):
    # A concurrent delete removed the profile after this request read it.
    expect_subscribe(sns)
    expect_subscribe(sns)
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    real = api._user_items
    calls = []

    def stale(user):
        calls.append(user)
        result = real(user)
        if len(calls) == 1:
            table.items.pop(("sub-alice", "#profile"))
        return result
    monkeypatch.setattr(api, "_user_items", stale)
    status, body = call("POST", "/api/subscriptions", body=BLOCK)
    assert status == 200 and body["confirmationSent"] is True
    assert table.items[("sub-alice", "#profile")]["alertCount"] == 2


def test_delete_keeps_profile_when_an_alert_was_just_added(table, sns, monkeypatch):
    expect_subscribe(sns)
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    real = api._user_items

    def racing(user):
        profile, subs = real(user)
        # A concurrent add raised the count after this read saw no alerts.
        table.items[("sub-alice", "#profile")]["alertCount"] += 1
        return profile, subs
    monkeypatch.setattr(api, "_user_items", racing)
    assert call("DELETE", "/api/subscriptions", {"id": "us-east-1#p5.48xlarge#spot-score#7"})[0] == 200
    assert ("sub-alice", "#profile") in table.items  # no unsubscribe queued either


def test_limit_and_duplicates(table, sns):
    expect_subscribe(sns)
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "threshold": "7"})
    assert status == 409 and "already" in body["error"]
    assert call("POST", "/api/subscriptions", body=BLOCK)[0] == 200
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "threshold": 3})
    assert status == 409 and "limit is 2" in body["error"]


def test_users_are_isolated(table, sns):
    expect_subscribe(sns)
    expect_subscribe(sns, sub="sub-bob", email="b@example.com")
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    assert call("POST", "/api/subscriptions", body=SPOT, sub="sub-bob", email="b@example.com")[0] == 200
    _, body = call("GET", "/api/subscriptions", sub="sub-bob", email="b@example.com")
    assert len(body["subscriptions"]) == 1 and body["email"] == "b@example.com"
    # Bob cannot delete Alice's alert: the key always uses the caller's sub.
    assert call("DELETE", "/api/subscriptions", {"id": "us-east-1#p5.48xlarge#spot-score#7"}, sub="sub-carol")[0] == 404
    assert ("sub-alice", "us-east-1#p5.48xlarge#spot-score#7") in table.items


def test_delete_last_alert_removes_profile(table, sns):
    expect_subscribe(sns)
    sns.activate()
    call("POST", "/api/subscriptions", body=SPOT)
    call("POST", "/api/subscriptions", body=BLOCK)
    assert call("DELETE", "/api/subscriptions", {"id": "us-east-1#p5.48xlarge#spot-score#7"}) == (
        200, {"id": "us-east-1#p5.48xlarge#spot-score#7", "deleted": True})
    assert table.items[("sub-alice", "#profile")]["alertCount"] == 1
    # The last delete removes the profile and its SNS subscription.
    sns.add_response("unsubscribe", {}, {"SubscriptionArn": SUB_ARN})
    assert call("DELETE", "/api/subscriptions", {"id": "us-east-1#p5.48xlarge#capacity-block#2"})[0] == 200
    assert not table.items
    assert call("DELETE", "/api/subscriptions", {"id": "us-east-1#p5.48xlarge#capacity-block#2"})[0] == 404


@pytest.mark.parametrize("query", [None, {"id": "#profile"}, {"id": "us-east-1#p5.48xlarge#probe#1"}, {"id": "x" * 300}])
def test_delete_rejects_bad_id(table, query):
    assert call("DELETE", "/api/subscriptions", query)[0] == 400


@pytest.mark.parametrize("body", [
    {**SPOT, "region": "eu-west-1"},
    {**SPOT, "instanceType": "g5.xlarge"},
    {**SPOT, "instanceType": "g6e.12xlarge"},  # allowed family, not offered
    {**SPOT, "region": "us-west-2"},
    {**SPOT, "signal": "probe"},
    {**SPOT, "threshold": 0}, {**SPOT, "threshold": 11}, {**SPOT, "threshold": 7.5}, {**SPOT, "threshold": True},
    {**SPOT, "threshold": None}, {k: v for k, v in SPOT.items() if k != "threshold"},
    {**BLOCK, "days": 183}, {**BLOCK, "days": "2d"}, {**BLOCK, "days": 15}, {**BLOCK, "days": 22}, {k: v for k, v in BLOCK.items() if k != "days"},
])
def test_post_rejects_bad_input(table, body):
    assert call("POST", "/api/subscriptions", body=body)[0] == 400
    assert not table.items


@pytest.mark.parametrize("claims,status", [
    ({"email": "", "verified": "true"}, 403),
    ({"email": "a@example.com", "verified": "false"}, 403),
    ({"email": "not-an-email", "verified": "true"}, 403),
    ({"sub": "", "email": "a@example.com", "verified": "true"}, 403),
])
def test_post_needs_identity_and_verified_email(table, claims, status):
    assert call("POST", "/api/subscriptions", body=SPOT, **claims)[0] == status
    assert not table.items


def test_table_request_shapes(monkeypatch, sns):
    """Run POST and DELETE against a stubbed DynamoDB client to check the request shapes.
    Expected parameters use the Table resource form; stubbed responses use the wire form."""
    t = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x").Table("subs")
    monkeypatch.setattr(api, "subscriptions_table", lambda: t)
    ddb = Stubber(t.meta.client)
    query = {"TableName": "subs", "KeyConditionExpression": "userId = :u",
             "ExpressionAttributeValues": {":u": "sub-alice"}, "ConsistentRead": True}
    sk = "us-east-1#p5.48xlarge#capacity-block#2"
    profile_key = {"userId": "sub-alice", "sk": "#profile"}
    item = {"userId": "sub-alice", "sk": sk, "region": "us-east-1", "instanceType": "p5.48xlarge", "signal": "capacity-block",
            "days": 2, "count": 1, "lastState": "pending", "createdAt": ANY}
    ddb.add_response("query", {"Items": []}, query)
    ddb.add_response("put_item", {}, {"TableName": "subs", "Item": {
        **profile_key, "email": "a@example.com", "subscriptionArn": SUB_ARN, "alertCount": 0, "createdAt": ANY},
        "ConditionExpression": "attribute_not_exists(sk)"})
    ddb.add_response("transact_write_items", {}, {"TransactItems": [
        {"Update": {"TableName": "subs", "Key": profile_key, "UpdateExpression": "ADD alertCount :one",
                    "ConditionExpression": "attribute_exists(sk) AND (attribute_not_exists(alertCount) OR alertCount < :max)",
                    "ExpressionAttributeValues": {":one": 1, ":max": 2}}},
        {"Put": {"TableName": "subs", "Item": item, "ConditionExpression": "attribute_not_exists(sk)"}},
    ]})
    ddb.add_response("transact_write_items", {}, {"TransactItems": [
        {"Delete": {"TableName": "subs", "Key": {"userId": "sub-alice", "sk": sk}, "ConditionExpression": "attribute_exists(sk)"}},
        {"Update": {"TableName": "subs", "Key": profile_key, "UpdateExpression": "ADD alertCount :neg",
                    "ConditionExpression": "attribute_exists(sk)", "ExpressionAttributeValues": {":neg": -1}}},
    ]})
    ddb.add_response("query", {"Items": [{"userId": {"S": "sub-alice"}, "sk": {"S": "#profile"}, "alertCount": {"N": "0"}}]}, query)
    ddb.add_response("delete_item", {"Attributes": {"subscriptionArn": {"S": SUB_ARN}}}, {
        "TableName": "subs", "Key": profile_key,
        "ConditionExpression": "attribute_not_exists(alertCount) OR alertCount <= :zero",
        "ExpressionAttributeValues": {":zero": 0}, "ReturnValues": "ALL_OLD"})
    expect_subscribe(sns)
    sns.add_response("unsubscribe", {}, {"SubscriptionArn": SUB_ARN})
    sns.activate()
    ddb.activate()
    assert call("POST", "/api/subscriptions", body=BLOCK)[0] == 200
    assert call("DELETE", "/api/subscriptions", {"id": sk})[0] == 200
    ddb.assert_no_pending_responses()


def test_transaction_cancel_reasons_are_read_from_the_service_shape(monkeypatch, sns):
    """A duplicate alert cancels the transaction on the Put; botocore exposes the reasons."""
    t = boto3.resource("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x").Table("subs")
    monkeypatch.setattr(api, "subscriptions_table", lambda: t)
    ddb = Stubber(t.meta.client)
    ddb.add_response("query", {"Items": [{"userId": {"S": "sub-alice"}, "sk": {"S": "#profile"},
                                          "email": {"S": "a@example.com"}, "subscriptionArn": {"S": SUB_ARN},
                                          "alertCount": {"N": "0"}}]})
    ddb.add_client_error("transact_write_items", "TransactionCanceledException", http_status_code=400,
                         response_meta={}, modeled_fields={"CancellationReasons": [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}]})
    ddb.activate()
    status, body = call("POST", "/api/subscriptions", body=BLOCK)
    assert status == 409 and "already" in body["error"]
    ddb.assert_no_pending_responses()


def test_two_concurrent_first_posts_keep_the_count(table, sns):
    """Two first POSTs both read "no profile". The second one's profile write must not replace the
    first one's profile and reset alertCount; it reads again and only adds its own alert."""
    expect_subscribe(sns)
    sns.activate()

    def other_request_wins():
        # The concurrent first POST created the profile and added its alert in the meantime.
        table.items[("sub-alice", "#profile")] = {"userId": "sub-alice", "sk": "#profile", "email": "a@example.com",
                                                  "subscriptionArn": SUB_ARN, "alertCount": 1, "createdAt": "t0"}
        table.items[("sub-alice", "us-east-1#p5.48xlarge#spot-score#7")] = {
            "userId": "sub-alice", "sk": "us-east-1#p5.48xlarge#spot-score#7", "region": "us-east-1",
            "instanceType": "p5.48xlarge", "signal": "spot-score", "threshold": 7, "lastState": "pending"}
    table.before_write = other_request_wins
    status, body = call("POST", "/api/subscriptions", body=BLOCK)
    assert status == 200 and body["confirmationSent"] is True
    profile = table.items[("sub-alice", "#profile")]
    # Two alerts exist and the count says two: the limit (2) now refuses a third.
    assert profile["alertCount"] == 2 and profile["createdAt"] == "t0"
    assert ("sub-alice", "us-east-1#p5.48xlarge#capacity-block#2") in table.items
    assert call("POST", "/api/subscriptions", body={**SPOT, "threshold": 3})[0] == 409


def test_email_change_never_rewrites_the_count(table, sns):
    """The email change updates email and subscriptionArn only: a count raised by a concurrent
    add after this request's read stays."""
    expect_subscribe(sns)
    expect_subscribe(sns, email="new@example.com", arn=NEW_ARN)
    sns.add_response("unsubscribe", {}, {"SubscriptionArn": SUB_ARN})
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    table.before_write = lambda: table.items[("sub-alice", "#profile")].update(alertCount=5)
    status, _ = call("POST", "/api/subscriptions", body=BLOCK, email="new@example.com")
    # The count was 5 (above the limit of 2) when the transaction ran, so it refused the add.
    assert status == 409
    profile = table.items[("sub-alice", "#profile")]
    assert (profile["email"], profile["subscriptionArn"], profile["alertCount"]) == ("new@example.com", NEW_ARN, 5)


def test_cleared_subscription_is_subscribed_again(table, sns):
    """The notifier removes subscriptionArn when SNS no longer knows it; the next POST subscribes again."""
    expect_subscribe(sns)
    expect_subscribe(sns, arn=NEW_ARN)
    sns.activate()
    assert call("POST", "/api/subscriptions", body=SPOT)[0] == 200
    del table.items[("sub-alice", "#profile")]["subscriptionArn"]
    status, body = call("POST", "/api/subscriptions", body=BLOCK)
    assert status == 200 and body["confirmationSent"] is True
    profile = table.items[("sub-alice", "#profile")]
    assert (profile["subscriptionArn"], profile["alertCount"]) == (NEW_ARN, 2)


# ------------------------------------------------------- Capacity Block alerts
def test_capacity_block_alert_stores_count_and_days(table, sns):
    expect_subscribe(sns)
    sns.activate()
    status, body = call("POST", "/api/subscriptions", body={**BLOCK, "count": 16})
    assert status == 200
    sub = body["subscription"]
    assert (sub["id"], sub["days"], sub["count"], sub["state"]) == ("us-east-1#p5.48xlarge#capacity-block#2#16", 2, 16, "waiting")
    item = table.items[("sub-alice", "us-east-1#p5.48xlarge#capacity-block#2#16")]
    assert (item["days"], item["count"]) == (2, 16)
    # Another count for the same search is another alert; count 1 keeps the key alerts had before counts.
    status, body = call("POST", "/api/subscriptions", body={**BLOCK, "count": "1"})
    assert status == 200 and body["subscription"]["id"] == "us-east-1#p5.48xlarge#capacity-block#2"
    # An alert can be deleted by its new ID.
    assert call("DELETE", "/api/subscriptions", {"id": "us-east-1#p5.48xlarge#capacity-block#2#16"})[0] == 200


def test_count_defaults_to_one_and_an_old_item_reads_as_one(table, sns):
    expect_subscribe(sns)
    sns.activate()
    status, body = call("POST", "/api/subscriptions", body=BLOCK)
    assert status == 200 and body["subscription"]["count"] == 1
    # An alert stored before counts existed (no count attribute) is a one-instance alert.
    table.items[("sub-alice", "us-east-1#p5.48xlarge#capacity-block#3")] = {
        "userId": "sub-alice", "sk": "us-east-1#p5.48xlarge#capacity-block#3", "region": "us-east-1",
        "instanceType": "p5.48xlarge", "signal": "capacity-block", "days": 3, "lastState": "unavailable"}
    subs = {s["id"]: s for s in call("GET", "/api/subscriptions")[1]["subscriptions"]}
    old = subs["us-east-1#p5.48xlarge#capacity-block#3"]
    assert (old["count"], old["state"], old["reason"]) == (1, "not-available", None)
    # The same search as the old alert is a duplicate, not a second item.
    status, body = call("POST", "/api/subscriptions", body={**BLOCK, "days": 3, "count": 1})
    assert status == 409 and body["code"] == "alert-exists"


@pytest.mark.parametrize("count", [0, 65, "x", 2.5, True, -1])
def test_capacity_block_count_must_be_1_to_64(table, count):
    status, body = call("POST", "/api/subscriptions", body={**BLOCK, "count": count})
    assert status == 400 and body["params"]["name"] == "count"
    assert not table.items


def test_capacity_block_alert_refuses_types_without_capacity_blocks(table):
    status, body = call("POST", "/api/subscriptions", body={**BLOCK, "instanceType": "g7e.2xlarge"})
    assert (status, body["code"]) == (400, "cb-unsupported-type")
    assert body["params"]["type"] == "g7e.2xlarge" and "p5" in body["params"]["families"]
    assert not table.items


def test_capacity_block_alert_checks_the_types_detected_in_its_region(table, sns, monkeypatch):
    """With a detected list for the Region, the alert's type must be in it (not only in a fallback family)."""
    monkeypatch.setattr(api, "cb_types", lambda region: ["trn1.32xlarge"] if region == "us-east-1" else None)
    status, body = call("POST", "/api/subscriptions", body=BLOCK)
    assert (status, body["code"]) == (400, "cb-unsupported-type") and "in us-east-1" in body["error"]
    assert body["params"] == {"type": "p5.48xlarge", "families": "trn1"} and not table.items
    monkeypatch.setattr(api, "cb_types", lambda region: [])
    assert call("POST", "/api/subscriptions", body=BLOCK)[1]["params"]["families"] == ""
    monkeypatch.setattr(api, "cb_types", lambda region: ["p5.48xlarge"])
    expect_subscribe(sns)
    sns.activate()
    assert call("POST", "/api/subscriptions", body=BLOCK)[0] == 200


def test_spot_score_alerts_have_no_count_and_any_offered_type(table, sns):
    expect_subscribe(sns)
    sns.activate()
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g7e.2xlarge", "count": 9})
    assert status == 200 and "count" not in body["subscription"]
    assert "count" not in table.items[("sub-alice", "us-east-1#g7e.2xlarge#spot-score#7")]


def test_rejected_alert_shows_state_and_reason(table, sns):
    expect_subscribe(sns)
    sns.activate()
    assert call("POST", "/api/subscriptions", body=BLOCK)[0] == 200
    item = table.items[("sub-alice", "us-east-1#p5.48xlarge#capacity-block#2")]
    item.update(lastState="rejected", lastReason="cb-invalid-duration", lastCheckedAt="2030-01-01T00:00:00Z")
    sub = call("GET", "/api/subscriptions")[1]["subscriptions"][0]
    assert (sub["state"], sub["reason"], sub["lastState"]) == ("rejected", "cb-invalid-duration", "rejected")
    assert sub["lastCheckedAt"] == "2030-01-01T00:00:00Z"
    item["lastState"] = "available"
    sub = call("GET", "/api/subscriptions")[1]["subscriptions"][0]
    assert (sub["state"], sub["reason"]) == ("available", None)


# ------------------------------------------- Spot score alerts are per score group (C1)
G6E = ["g6e.12xlarge", "g6e.24xlarge", "g6e.xlarge", "g6e.48xlarge"]
G6E_GPUS = {"g6e.xlarge": 1, "g6e.12xlarge": 4, "g6e.24xlarge": 4, "g6e.48xlarge": 8}


@pytest.fixture
def groups(monkeypatch):
    monkeypatch.setattr(api, "MAX_SUBSCRIPTIONS_PER_USER", 10)
    monkeypatch.setattr(api, "region_offerings", lambda region: list(G6E) if region == "us-east-1" else ["g6e.24xlarge"])
    monkeypatch.setattr(api, "_groups_state", lambda: {"gpus": dict(G6E_GPUS), "groups": {}})


def test_a_spot_alert_is_stored_under_the_first_size_of_its_group_in_the_region(table, sns, groups):
    expect_subscribe(sns)
    sns.activate()
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.24xlarge"})
    assert status == 200 and body["subscription"]["instanceType"] == "g6e.12xlarge"
    assert body["subscription"]["id"] == "us-east-1#g6e.12xlarge#spot-score#7"
    # In a Region that sells only the 24xlarge of the group, that size is the first one.
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "region": "us-west-2", "instanceType": "g6e.24xlarge"})
    assert status == 200 and body["subscription"]["id"] == "us-west-2#g6e.24xlarge#spot-score#7"


def test_two_sizes_of_one_group_region_and_threshold_are_the_same_alert(table, sns, groups):
    expect_subscribe(sns)
    sns.activate()
    assert call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.12xlarge"})[0] == 200
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.24xlarge"})
    assert (status, body["code"]) == (409, "alert-exists")
    # Another threshold, another group (8 GPUs) or a Capacity Block alert of the same size is another alert.
    assert call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.24xlarge", "threshold": 5})[0] == 200
    assert call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.48xlarge"})[0] == 200
    assert call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.xlarge"})[0] == 200


def test_an_older_alert_on_another_size_of_the_group_counts_as_the_same(table, sns, groups):
    table.items[("sub-alice", "#profile")] = {"userId": "sub-alice", "sk": "#profile", "email": "a@example.com",
                                              "subscriptionArn": SUB_ARN, "alertCount": 1}
    table.items[("sub-alice", "us-east-1#g6e.24xlarge#spot-score#7")] = {
        "userId": "sub-alice", "sk": "us-east-1#g6e.24xlarge#spot-score#7", "region": "us-east-1",
        "instanceType": "g6e.24xlarge", "signal": "spot-score", "threshold": 7, "lastState": "pending"}
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.12xlarge"})
    assert (status, body["code"]) == (409, "alert-exists")


def test_capacity_block_alerts_keep_their_own_size(table, sns, groups, monkeypatch):
    monkeypatch.setattr(api, "cb_types", lambda region: list(G6E))
    expect_subscribe(sns)
    sns.activate()
    status, body = call("POST", "/api/subscriptions", body={**BLOCK, "instanceType": "g6e.24xlarge"})
    assert status == 200 and body["subscription"]["instanceType"] == "g6e.24xlarge"
    assert call("POST", "/api/subscriptions", body={**BLOCK, "instanceType": "g6e.12xlarge"})[0] == 200


def test_a_size_without_a_known_group_keeps_its_name(table, sns, groups, monkeypatch):
    monkeypatch.setattr(api, "_groups_state", lambda: None)
    expect_subscribe(sns)
    sns.activate()
    status, body = call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.24xlarge"})
    assert status == 200 and body["subscription"]["instanceType"] == "g6e.24xlarge"
    assert call("POST", "/api/subscriptions", body={**SPOT, "instanceType": "g6e.12xlarge"})[0] == 200
