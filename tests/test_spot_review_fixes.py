"""Spot score review fixes: configurations in use count for 24 hours, one endpoint for every
request, and page loads that only repeat. Fake tables only; nothing reaches AWS."""
import json
import os

os.environ.update(SPOT_SCORE_FAMILIES="p5.*,g6e.*", TABLE_NAME="t")

import api  # noqa: E402
import spot_configs  # noqa: E402

NOW = 2_000_000_000
HOUR = 3600


class Table:
    def __init__(self):
        self.items, self.puts = {}, []

    def get_item(self, Key, **_):
        item = self.items.get(Key["cacheKey"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, **_):
        self.puts.append(Item["cacheKey"])
        self.items[Item["cacheKey"]] = dict(Item)


# F6: in use for 24 hours, free to repeat for 23
def test_a_configuration_used_23_and_a_half_hours_ago_still_counts_as_in_use():
    entry = {"lastUsedAt": NOW - int(23.5 * HOUR)}
    assert not spot_configs.is_warm(entry, NOW)
    assert spot_configs.in_use(entry, NOW)
    # The release margin: still counted at exactly 24 hours, gone one margin later.
    assert spot_configs.in_use({"lastUsedAt": NOW - 24 * HOUR}, NOW)
    assert spot_configs.in_use({"lastUsedAt": NOW - spot_configs.COUNT_SECONDS + 1}, NOW)
    assert not spot_configs.in_use({"lastUsedAt": NOW - spot_configs.COUNT_SECONDS}, NOW)
    assert not spot_configs.in_use(None, NOW)


def test_active_count_uses_the_24_hour_rule(monkeypatch):
    state = {"index": {"a": {}, "b": {}, "c": {}}, "new24h": []}
    known = {"a": {"lastUsedAt": NOW - HOUR}, "b": {"lastUsedAt": NOW - int(23.5 * HOUR)}, "c": {"lastUsedAt": NOW - 25 * HOUR}}
    assert spot_configs.active_count(None, state, NOW, known=known) == 2


# One endpoint for every request, no click reserve, no priority of page loads
def test_every_request_goes_to_one_endpoint():
    import score_groups
    groups = {"g6e/4": {"types": ["g6e.12xlarge"], "chunks": [["eu-west-1", "us-west-2"]], "added": {}}}
    assert {c.endpoint for _, c in score_groups.configs(groups)} == {"us-east-1"}
    assert api._ledger(NOW).endpoint == "us-east-1"


def test_page_loads_only_repeat():
    assert spot_configs.free_slots({"limitedUntil": 0, "new24h": [], "refusals": []}, spot_configs.Budget(5, 5), NOW,
                                   spot_configs.REPEAT_ONLY) == 0
    cfg = spot_configs.build("g6e/4", ["us-east-1"], "us-east-1", types=["g6e.12xlarge"])
    assert spot_configs.grant(None, [cfg], spot_configs.REPEAT_ONLY, spot_configs.Budget(5, 5), NOW) == set()


def test_run_sps_sends_the_configuration_it_was_given(monkeypatch):
    sent = []
    monkeypatch.setattr(api, "_region_spot_scores", lambda cfg: sent.append(cfg) or {"us-east-1": 3})
    cfg = spot_configs.build("g6e/4", ["us-east-1"], "us-east-1", types=["g6e.12xlarge"])
    assert api._run_sps({cfg.fp: cfg}, None) == {cfg.fp: {"us-east-1": 3}} and sent == [cfg]


# B4: a cache write that fails below the AWS error layer (a timeout) is logged, not raised
def test_a_spot_score_cache_write_timeout_is_logged_and_the_run_goes_on(caplog):
    import spot_cache
    from botocore.exceptions import ReadTimeoutError

    class Slow:
        def put_item(self, **_):
            raise ReadTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com/")
    with caplog.at_level("WARNING"):
        assert spot_cache.write(Slow(), "g6e/4", ["us-east-1"], {"us-east-1": 7}, "t", NOW, 40) is None
    assert "spot score cache write failed for g6e/4 (ReadTimeoutError)" in caplog.text
