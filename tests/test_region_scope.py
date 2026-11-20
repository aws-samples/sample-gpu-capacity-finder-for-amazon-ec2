"""regionScope was removed: the read-only pages always cover every enabled Region (minus excludeRegions)."""
import pytest

from gpu_capacity_finder.config import ConfigError, parse_context


def ctx(**kw):
    return lambda key: kw.get(key)


def test_the_config_has_no_region_scope():
    assert "region_scope" not in parse_context(ctx())


@pytest.mark.parametrize("value", ["enabled", "configured"])
def test_region_scope_is_refused_with_the_replacement(value):
    with pytest.raises(ConfigError, match="^regionScope was removed: .*excludeRegions"):
        parse_context(ctx(regionScope=value))
