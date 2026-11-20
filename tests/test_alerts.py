"""Pure alert logic: grouping, availability, transitions, and the email text."""
from datetime import datetime, timezone
from decimal import Decimal

import alerts
import pytest

REGIONS = ["us-east-1", "us-west-2"]
# The Spot score families of this deployment (spotScoreFamilies).
EXTRA = ["p5.*", "g6e.*"]


def sub(signal="spot-score", region="us-east-1", itype="p5.48xlarge", value=7, user="sub-alice", last="pending"):
    name = alerts.PARAMS[signal][0]
    return {"userId": user, "sk": alerts.sort_key(region, itype, signal, value), "region": region,
            "instanceType": itype, "signal": signal, name: Decimal(value), "lastState": last}


def test_sort_key_matches_pattern_and_profile_is_not_a_subscription():
    sk = alerts.sort_key("us-east-1", "p5.48xlarge", "capacity-block", 14)
    assert sk == "us-east-1#p5.48xlarge#capacity-block#14" and alerts.SK_RE.match(sk)
    assert not alerts.SK_RE.match("#profile") and not alerts.SK_RE.match("us-east-1#p5.48xlarge#probe#1")
    assert not alerts.is_subscription({"sk": alerts.PROFILE_SK}) and alerts.is_subscription({"sk": sk})


@pytest.mark.parametrize("item,ok", [
    (sub(), True),
    (sub(signal="capacity-block", value=182), True),
    (sub(region="eu-west-1"), False),
    (sub(itype="g6e.xlarge"), True),
    # g5 is tracked by the rule but not a Spot score family of this deployment.
    (sub(itype="g5.xlarge"), False),
    (sub(signal="capacity-block", itype="g5.xlarge"), True),
    (sub(value=11), False),
    (sub(signal="capacity-block", value=0), False),
    (sub(signal="capacity-block", value=15), False),
    (sub(signal="capacity-block", value=21), True),
    ({**sub(), "signal": "probe"}, False),
    ({**sub(), "threshold": "x"}, False),
])
def test_is_valid_checks_current_settings(item, ok):
    assert alerts.is_valid(item, REGIONS, EXTRA) is ok


def test_group_queries_each_signal_once():
    items = [sub(value=5), sub(value=8, user="sub-bob"), sub(region="us-west-2"),
             sub(signal="capacity-block", value=1), sub(signal="capacity-block", value=1, user="sub-bob"),
             sub(signal="capacity-block", value=7)]
    groups = alerts.group(items)
    assert set(groups) == {("spot-score", "us-east-1", "p5.48xlarge"), ("spot-score", "us-west-2", "p5.48xlarge"),
                           ("capacity-block", "us-east-1", "p5.48xlarge", 1, 1), ("capacity-block", "us-east-1", "p5.48xlarge", 7, 1)}
    assert len(groups[("spot-score", "us-east-1", "p5.48xlarge")]) == 2
    assert len(groups[("capacity-block", "us-east-1", "p5.48xlarge", 1, 1)]) == 2


@pytest.mark.parametrize("threshold,score,ok", [(7, 7, True), (7, 9, True), (7, 6, False), (1, None, False)])
def test_spot_availability(threshold, score, ok):
    assert alerts.is_available(sub(value=threshold), score) is ok


def test_capacity_block_availability():
    item = sub(signal="capacity-block", value=1)
    assert alerts.is_available(item, [{"StartDate": datetime(2030, 1, 1, tzinfo=timezone.utc)}])
    assert not alerts.is_available(item, [])


@pytest.mark.parametrize("last,score,state,notify", [
    ("pending", 9, "available", True),
    ("unavailable", 9, "available", True),
    ("available", 9, "available", False),
    ("available", 2, "unavailable", False),
    ("unavailable", 2, "unavailable", False),
    (None, 9, "available", True),
    # No score this run: unknown, whatever the last state, and never an email.
    ("available", None, "unknown", False),
    ("unavailable", None, "unknown", False),
    ("pending", None, "unknown", False),
    (None, None, "unknown", False),
])
def test_transitions_notify_only_when_turning_available(last, score, state, notify):
    item = sub(last=last)
    if last is None:
        del item["lastState"]
    assert list(alerts.transitions([item], score)) == [(item, state, notify)]


def test_message_text():
    subject, body = alerts.message(sub(value=6), 8, "https://example.cloudfront.net/", "p5 · 8 GPUs")
    assert subject == "Spot score 8: p5 · 8 GPUs in us-east-1" and "threshold of 6" in body
    assert "any size of the group, your alert names p5.48xlarge" in body
    # Without a known group: the family.
    assert alerts.message(sub(value=6), 8, "https://x/")[0] == "Spot score 8: p5 family in us-east-1"
    assert "https://example.cloudfront.net/" in body
    offers = [{"StartDate": datetime(2030, 1, 3, tzinfo=timezone.utc), "AvailabilityZone": "us-east-1b"},
              {"StartDate": datetime(2030, 1, 2, tzinfo=timezone.utc), "AvailabilityZone": "us-east-1a"}]
    subject, body = alerts.message(sub(signal="capacity-block", value=2), offers, "https://x/")
    assert subject.startswith("Capacity Block available") and len(subject) <= 100
    assert "2 Capacity Block offering(s)" in body and "2030-01-02T00:00:00+00:00 in us-east-1a" in body


def test_block_days_follow_ec2_steps():
    assert alerts.BLOCK_DAYS[:14] == tuple(range(1, 15))
    assert alerts.BLOCK_DAYS[14:] == tuple(range(21, 183, 7)) and alerts.BLOCK_DAYS[-1] == 182
    assert [d for d in (14, 15, 20, 21, 22, 182, 183) if alerts.valid_days(d)] == [14, 21, 182]
    assert alerts.valid_value("spot-score", 10) and not alerts.valid_value("spot-score", 11)


# ------------------------------------------------------- Capacity Block count
def block(days=2, count=None, **kw):
    item = sub(signal="capacity-block", value=days, **kw)
    if count is not None:
        item["count"] = Decimal(count)
        item["sk"] = alerts.sort_key(item["region"], item["instanceType"], "capacity-block", days, count)
    return item


def test_count_is_part_of_the_key_and_one_keeps_the_old_key():
    assert alerts.sort_key("us-east-1", "p5.48xlarge", "capacity-block", 2) == "us-east-1#p5.48xlarge#capacity-block#2"
    assert alerts.sort_key("us-east-1", "p5.48xlarge", "capacity-block", 2, 1) == "us-east-1#p5.48xlarge#capacity-block#2"
    sk = alerts.sort_key("us-east-1", "p5.48xlarge", "capacity-block", 2, 16)
    assert sk == "us-east-1#p5.48xlarge#capacity-block#2#16" and alerts.SK_RE.match(sk)
    assert alerts.SK_RE.match("us-east-1#p5.48xlarge#capacity-block#182#64")
    # Spot alerts have no count part, and a count needs one or two digits.
    assert not alerts.SK_RE.match("us-east-1#p5.48xlarge#spot-score#7#2")
    assert not alerts.SK_RE.match("us-east-1#p5.48xlarge#capacity-block#2#100")
    assert alerts.sort_key("us-east-1", "p5.48xlarge", "spot-score", 7, 5) == "us-east-1#p5.48xlarge#spot-score#7"


def test_old_items_without_count_mean_one_instance():
    old = block()
    assert "count" not in old and alerts.count_of(old) == 1
    assert alerts.fetch_key(old) == ("capacity-block", "us-east-1", "p5.48xlarge", 2, 1)
    assert alerts.fetch_key(block(count=8)) == ("capacity-block", "us-east-1", "p5.48xlarge", 2, 8)
    # The same search with different counts is two EC2 queries.
    assert len(alerts.group([block(), block(count=1, user="sub-bob"), block(count=8)])) == 2


@pytest.mark.parametrize("count,ok", [(1, True), (64, True), (0, False), (65, False)])
def test_is_valid_checks_the_count(count, ok):
    assert alerts.is_valid(block(count=count), REGIONS, EXTRA) is ok


def test_count_limit_matches_capacity_blocks():
    import capacity_blocks
    assert alerts.COUNT_MAX == capacity_blocks.MAX_COUNT == 64


def test_message_names_the_count():
    offers = [{"StartDate": datetime(2030, 1, 2, tzinfo=timezone.utc), "AvailabilityZone": "us-east-1a"}]
    assert "for 16 x p5.48xlarge, 2 day(s)" in alerts.message(block(count=16), offers, "https://x/")[1]
    assert "for 1 x p5.48xlarge" in alerts.message(block(), offers, "https://x/")[1]


@pytest.mark.parametrize("last,reason,state,shown_reason", [
    ("available", None, "available", None),
    ("unavailable", None, "not-available", None),
    ("pending", None, "waiting", None),
    (None, None, "waiting", None),
    ("rejected", "cb-invalid-duration", "rejected", "cb-invalid-duration"),
    # A reason left from an older run is not shown once the state moved on.
    ("unavailable", "cb-invalid-duration", "not-available", None),
])
def test_state_view(last, reason, state, shown_reason):
    item = block(last=last)
    if last is None:
        del item["lastState"]
    if reason:
        item["lastReason"] = reason
    assert alerts.state_view(item) == (state, shown_reason)


def test_rejected_alert_that_turns_available_notifies():
    assert list(alerts.transitions([block(last="rejected")], [{"StartDate": 1}]))[0][1:] == ("available", True)


def test_unknown_is_never_a_stored_state_and_only_for_spot_scores():
    assert alerts.UNKNOWN not in alerts.STATES
    # A Capacity Block search with no answer list is still "not available" (an empty search).
    block_item = sub(signal="capacity-block", value=1, last="available")
    assert list(alerts.transitions([block_item], []))[0][1:] == ("unavailable", False)
    # A missing score then a score: the available alert never emails twice.
    item = sub(last="available")
    assert list(alerts.transitions([item], None))[0][1:] == ("unknown", False)
    assert list(alerts.transitions([item], 9))[0][1:] == ("available", False)
