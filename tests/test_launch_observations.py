"""Test launches and Spot placement scores (a score is an estimate, not a launch result): a test
launch stores no zone score any more (zone-level scores were removed) while older records keep
theirs, the observed launch results of the last 24 hours across all users, and the short score
history. Fake tables only; nothing reaches AWS."""
import json
import os
import time
from datetime import datetime, timezone

os.environ.update(SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t", ENABLE_PROBE="true")

import api  # noqa: E402
import cache_fakes  # noqa: E402
import pytest  # noqa: E402
import spot_cache  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from score_fakes import Table  # noqa: E402
from test_api import ProbeTable, event  # noqa: E402

ITYPE, REGION, AZ = "p5.48xlarge", "us-east-1", "us-east-1a"
PROBE = {"region": REGION, "instanceType": ITYPE, "market": "spot"}
OFFERS = {
    "us-east-1": {"families": {"p5": [ITYPE]}, "zones": {ITYPE: ["us-east-1a", "us-east-1b"]},
                  "fetchedAt": "2033-05-18T03:00:00.000000Z", "fromCache": True},
    "us-west-2": {"families": {}, "zones": {}, "fetchedAt": "2033-05-18T03:00:00.000000Z", "fromCache": True},
}


def epoch():
    return int(time.time())


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def world(monkeypatch):
    """A probe table, a cache table, and no EC2 at all except the stubbed launch."""
    monkeypatch.setattr(api, "_offering_entry", lambda region: OFFERS[region])
    probes, cache = ProbeTable(), Table()
    cache_fakes.seeded(cache.items)
    launches = []
    monkeypatch.setattr(api, "table", lambda: probes)
    monkeypatch.setattr(api, "offering_cache_table", lambda: cache)
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [])
    monkeypatch.setattr(api, "client", lambda service, region: pytest.fail(f"no {service} call expected"))
    monkeypatch.setattr(api, "run_probe", lambda *a: launches.append(a) or {
        "result": "no-capacity", "errorCode": "InsufficientInstanceCapacity", "obtained": None, "instanceIds": [],
        "errors": [], "sizes": [ITYPE], "skipped": []})
    return probes, cache, launches


def post(body=PROBE):
    r = api.handler(event("POST", "/api/probe", body=body), None)
    assert r["statusCode"] == 200, r
    return json.loads(r["body"])


# ------------------------------------------------- A: the score of a test launch
def test_a_spot_launch_stores_no_zone_score_and_asks_for_none(world):
    probes, cache, launches = world
    body = post()
    assert body["cached"] is False and body["result"] == "no-capacity" and len(launches) == 1
    assert (body["score"], body["scoreSource"], body["scoreFetchedAt"]) == (None, "none", None)
    record = probes.records()[0]
    assert record["scoreSource"] == "none" and "score" not in record
    assert not [k for k in cache.items if k.startswith(("spsaz#", "spscfg#"))]


def test_cooldown_answers_carry_the_stored_score_of_an_older_record(world, monkeypatch):
    probes, cache, launches = world
    record = {"sk": f"{stamp(epoch() - 60)}#{ITYPE}#spot", "target": ITYPE, "az": AZ, "type": ITYPE, "mode": "spot",
              "result": "launched", "user": "bob", "score": 3, "scoreSource": "live",
              "scoreFetchedAt": stamp(epoch() - 61)}
    monkeypatch.setattr(api, "_recent_probes", lambda *_: [record])
    body = post()
    assert body["cached"] is True and launches == []
    assert (body["score"], body["scoreSource"], body["scoreFetchedAt"]) == (3, "live", record["scoreFetchedAt"])
    # An older record without a score.
    old = {k: v for k, v in record.items() if not k.startswith("score")}
    assert api._score_fields(old) == {"score": None, "scoreFetchedAt": None, "scoreSource": "none"}


# The observed results of the last 24 hours per score group are pinned in test_fleet_launch.py.


# --------------------------------------------------------- C: score history
def test_history_appends_trims_to_48_readings_and_one_day():
    t = Table()
    now = epoch()
    key = spot_cache.history_key(ITYPE, REGION)
    assert key == f"spshist#{ITYPE}#{REGION}"
    for i in range(60):
        at = now - (60 - i) * 600
        spot_cache.append_history(t, key, {AZ: i % 10 + 1}, stamp(at), at)
    item = t.items[key]
    readings = json.loads(item["readings"])
    assert len(readings) == 48 and readings[0]["at"] == stamp(now - 48 * 600) and readings[-1]["scores"] == {AZ: 10}
    assert [r["at"] for r in readings] == sorted(r["at"] for r in readings)
    assert readings[-1]["at"] == stamp(now - 600) and item["expires"] == now - 600 + 2 * 86400
    # Readings older than a day are dropped on read and on the next write.
    later = now + 86400 - 600 * 5
    kept = spot_cache.read_history(t, key, later)
    assert kept and all(later - spot_cache.stamp_epoch(r["at"]) < 86400 for r in kept)
    assert spot_cache.shown(readings) == readings[-24:]


def test_history_takes_at_most_one_reading_per_five_minutes():
    t = Table()
    now = epoch()
    key = spot_cache.history_key(ITYPE, "regions")
    spot_cache.append_history(t, key, {"us-east-1": 3}, stamp(now - 299), now)
    out = spot_cache.append_history(t, key, {"us-east-1": 9}, stamp(now), now)
    assert [r["scores"] for r in out] == [{"us-east-1": 3}]
    assert len(json.loads(t.items[key]["readings"])) == 1
    out = spot_cache.append_history(t, key, {"us-east-1": 9}, stamp(now + 1), now + 1)
    assert [r["scores"] for r in out] == [{"us-east-1": 3}, {"us-east-1": 9}]
    # An empty answer is no reading.
    assert len(spot_cache.append_history(t, key, {}, stamp(now + 900), now + 900)) == 2


def test_history_read_and_write_failures_are_ignored():
    class Broken:
        def get_item(self, **_):
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")

        def put_item(self, **_):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")
    now = epoch()
    assert spot_cache.read_history(Broken(), "spshist#x#regions", now) == []
    assert spot_cache.append_history(Broken(), "spshist#x#regions", {"us-east-1": 3}, stamp(now), now) == [
        {"at": stamp(now), "scores": {"us-east-1": 3}}]


def test_a_group_answer_shows_the_last_24_readings():
    t = Table()
    now = epoch()
    readings = [{"at": stamp(now - (30 - i) * 600), "scores": {"us-east-1": i % 10 + 1}} for i in range(30)]
    t.items["spshist#p5/8#regions"] = {"cacheKey": "spshist#p5/8#regions", "readings": json.dumps(readings),
                                       "expires": now + 3600}
    assert spot_cache.shown(spot_cache.read_history(t, spot_cache.history_key("p5/8", "regions"), now)) == readings[-24:]
