"""excludeRegions: an excluded Region is not listed or called by any feature, never joins a Spot
score group, and leaves one at the next daily update."""
import importlib

import pytest

from gpu_capacity_finder import config


def _regions_module(monkeypatch, excluded):
    monkeypatch.setenv("EXCLUDE_REGIONS", excluded)
    import regions
    return importlib.reload(regions)


def test_an_excluded_region_is_left_out_of_the_list(monkeypatch):
    regions = _regions_module(monkeypatch, "me-south-1")
    assert regions.order(["eu-west-1", "me-south-1", "us-east-1"]) == ["eu-west-1", "us-east-1"]


def test_the_fallback_list_also_leaves_it_out(monkeypatch):
    regions = _regions_module(monkeypatch, "me-south-1")
    assert regions.order(["us-east-1", "me-south-1"]) == ["us-east-1"]


def test_without_the_setting_nothing_is_left_out(monkeypatch):
    regions = _regions_module(monkeypatch, "")
    assert regions.order(["me-south-1", "us-east-1"]) == ["me-south-1", "us-east-1"]


def test_an_excluded_region_never_joins_a_group_and_leaves_one_at_the_daily_update():
    import score_groups
    gpus = {"g6e.12xlarge": 4}
    found = score_groups.collect({"me-south-1": ["g6e.12xlarge"], "us-east-1": ["g6e.12xlarge"]}, gpus, ["g6e.*"])
    groups = score_groups.merge({}, found, ["g6e.*"], {"me-south-1"}, {}, 1_000)
    assert groups["g6e/4"]["chunks"] == [["us-east-1"]]
    # Placed before the exclusion: removed from its chunk at the next daily update.
    placed = score_groups.merge({}, found, ["g6e.*"], set(), {}, 1_000)
    assert placed["g6e/4"]["chunks"] == [["me-south-1", "us-east-1"]]
    assert score_groups.merge(placed, {}, ["g6e.*"], {"me-south-1"}, {}, 2_000)["g6e/4"]["chunks"] == [["us-east-1"]]


def test_the_cleanup_scans_every_enabled_region_but_the_excluded_ones(monkeypatch):
    import cleanup

    class Ec2:
        def describe_regions(self, AllRegions):
            assert AllRegions is False
            return {"Regions": [{"RegionName": r} for r in ("us-east-1", "me-south-1", "eu-west-1")]}
    monkeypatch.setattr(cleanup, "EXCLUDED", frozenset({"me-south-1"}))
    monkeypatch.setattr(cleanup.boto3, "client", lambda *a, **k: Ec2())
    assert cleanup.target_regions() == ["eu-west-1", "us-east-1"]


def _load(**ctx):
    return config.parse_context(lambda key: ctx.get(key))


def test_config_reads_the_list():
    assert _load(excludeRegions="me-south-1,af-south-1")["exclude_regions"] == ["me-south-1", "af-south-1"]
    assert _load()["exclude_regions"] == []


def test_config_accepts_any_region_name_but_the_spot_score_endpoint():
    # Without a configured Region list there is nothing an exclusion could clash with; only the
    # Spot score endpoint (us-east-1) must stay.
    assert _load(excludeRegions="eu-west-1")["exclude_regions"] == ["eu-west-1"]
    with pytest.raises(config.ConfigError):
        _load(excludeRegions="us-east-1")


def test_config_refuses_a_malformed_name():
    with pytest.raises(config.ConfigError):
        _load(excludeRegions="not a region")


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    monkeypatch.delenv("EXCLUDE_REGIONS", raising=False)
    import regions
    importlib.reload(regions)
