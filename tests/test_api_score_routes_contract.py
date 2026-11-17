"""The response keys of the Spot score routes that the page reads. Fakes only."""
import json
import os

os.environ.update(SPOT_SCORE_FAMILIES="g6e.*,p5.*", TABLE_NAME="t")

import spot_prices  # noqa: E402
from score_fakes import NOW  # noqa: E402
from test_spot_score_routes import call, cached, env, granted  # noqa: E402,F401

TOP = {"generatedAt", "state", "endpoint", "regions", "groups", "spotScoreLimitedUntil", "lastDailyUpdateAt",
       "nextDailyUpdateAt", "nextWarmAt", "warmMinutes", "dailyUpdateUtc"}
GROUP = {"key", "family", "gpus", "label", "sizes", "regions", "pendingRegions", "scores", "regionStates", "fetchedAt",
         "fromCache", "configurations", "prices", "history"}


def test_the_documented_keys(env):
    t, _, _, _ = env
    granted(t, ("g6e/4", "us-east-1"))
    cached(t, "g6e/4", "us-east-1", {"us-east-1": 7})
    spot_prices.write(t, "us-east-1", {"g6e.12xlarge": {"us-east-1a": "5.10"}}, "t0", NOW)
    body = call("/api/spot-scores")[1]
    assert set(body) == TOP and all(set(g) == GROUP for g in body["groups"])
    refreshed = call("/api/spot-scores", {"group": "g6e/4", "refresh": "true"})[1]["groups"][0]
    assert set(refreshed) - GROUP == {"refreshed"}
    one = call("/api/spot-scores", {"instanceType": "g6e.12xlarge"})[1]
    assert set(one) == TOP | {"instanceType", "group", "scoreState"}
    heat = call("/api/spot-heatmap", {"types": "g6e.12xlarge"})[1]
    assert set(heat) == {"generatedAt", "state", "regions", "scores", "fromCache", "fetchedAt", "scoreState",
                         "scoreGroups", "groupLabels", "history", "errors", "scoreLevel", "nextDailyUpdateAt",
                         "lastDailyUpdateAt", "nextWarmAt", "warmMinutes"}
    prices = call("/api/spot-prices", {"region": "us-east-1"})
    assert prices[0] == 200
    assert set(prices[1]) == {"region", "prices", "groups", "fetchedAt", "fromCache"}
    assert set(prices[1]["groups"]["g6e/4"]) == {"fromUsdPerHour", "instanceType", "az", "sizes"}
    assert json.loads(json.dumps(body))["groups"][1]["prices"]["us-east-1"]["fromUsdPerHour"] == "5.1"
