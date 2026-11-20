"""The permanent first-seen record of the account's Regions (region_slots): written by the Region
discovery, read by the Spot score groups to place new Regions in first-seen order. In-memory
tables only."""
import json

import cache_fakes  # noqa: F401 - the conditional writes of the fake table
import region_slots
import regions
from botocore.exceptions import ClientError

NOW = 2_000_000_000
DAY = 86400
CONFIGURED = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "ap-east-1", "ap-northeast-1", "ap-northeast-2",
              "ap-south-1", "ap-southeast-3"]
OTHERS = ["af-south-1", "ap-northeast-3", "ap-south-2", "ap-southeast-1", "ap-southeast-2", "ca-central-1",
          "eu-central-1", "eu-north-1", "eu-south-2", "eu-west-1", "eu-west-2", "me-central-1", "sa-east-1"]


class Table:
    """get_item/put_item by cacheKey with the conditional writes DynamoDB evaluates."""

    def __init__(self):
        self.items, self.puts = {}, []
        self.fail_get = self.fail_put = None

    def get_item(self, Key, **_):
        if self.fail_get:
            raise self.fail_get
        item = self.items.get(Key["cacheKey"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        if self.fail_put:
            raise self.fail_put
        cache_fakes.conditional_put(self.items, Item, ConditionExpression, ExpressionAttributeValues)
        self.puts.append(Item["cacheKey"])


# ------------------------------------------------------------------ first seen
def test_the_first_seen_seed_happens_once_and_is_the_same_twice():
    a, b = Table(), Table()
    for t in (a, b):
        region_slots.observe(t, OTHERS + CONFIGURED, NOW)
        region_slots.observe(t, CONFIGURED + OTHERS, NOW + DAY)  # a second run: nothing new
    assert a.items == b.items
    item = a.items[region_slots.FIRST_SEEN_KEY]
    assert "expires" not in item and item["seededAt"] == NOW and item["version"] == 1
    assert set(json.loads(item["regions"]).values()) == {NOW}


def test_a_region_seen_later_gets_its_own_date_and_keeps_it():
    t = Table()
    region_slots.observe(t, CONFIGURED, NOW)
    seen = region_slots.observe(t, CONFIGURED + ["eu-west-1"], NOW + 3 * DAY)
    assert seen["eu-west-1"] == NOW + 3 * DAY and seen["us-east-1"] == NOW
    # Gone from the list, then back: the first date stays.
    region_slots.observe(t, CONFIGURED, NOW + 4 * DAY)
    assert region_slots.observe(t, ["eu-west-1"], NOW + 9 * DAY)["eu-west-1"] == NOW + 3 * DAY


def test_two_first_seeds_at_once_keep_one_and_merge_the_other():
    t = Table()
    real_put = t.put_item
    raced = []

    def put(Item, **kw):
        if not raced:
            raced.append(True)
            real_put(Item={"cacheKey": region_slots.FIRST_SEEN_KEY, "version": 1,
                           "regions": json.dumps({"us-east-1": NOW - 10})},
                     ConditionExpression=region_slots.SAME_VERSION, ExpressionAttributeValues={":v": 0})
        return real_put(Item=Item, **kw)
    t.put_item = put
    seen = region_slots.observe(t, ["us-east-1", "eu-west-1"], NOW)
    assert seen == {"us-east-1": NOW - 10, "eu-west-1": NOW}
    assert json.loads(t.items[region_slots.FIRST_SEEN_KEY]["regions"]) == seen


def test_a_failed_first_seen_read_changes_nothing():
    t = Table()
    t.fail_get = ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
    assert region_slots.observe(t, ["us-east-1"], NOW) == {"us-east-1": NOW} and t.puts == []


def test_discovery_records_first_seen_regions(monkeypatch):
    t = Table()
    names, _, source = regions.resolve(t, lambda: ["us-west-2", "eu-west-1", "us-east-1"], ["us-east-1"], NOW,
                                       take_lock=lambda: True)
    assert source == "live"
    assert json.loads(t.items[region_slots.FIRST_SEEN_KEY]["regions"]) == {r: NOW for r in names}


def test_the_slot_groups_of_earlier_versions_are_gone():
    for name in ("resolve", "place", "order_new", "read_slots", "stored_endpoint", "view", "slots_key"):
        assert not hasattr(region_slots, name), name
