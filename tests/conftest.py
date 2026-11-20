"""Shared safety net: no test reaches AWS, or the real DynamoDB cache table, by accident."""
import sys

import botocore.endpoint
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: a real-clock test (a plain `pytest` runs it; `-m 'not slow'` skips it)")


class RealAwsCall(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _no_real_aws_calls(monkeypatch):
    """Stubbed calls never reach the endpoint; any request that would go on the wire is refused and
    fails the test at teardown, even when the code under test catches the error (Region fan-out)."""
    attempts = []

    def refuse(self, request, *_args, **_kwargs):
        attempts.append(f"{request.method} {request.url}")
        raise RealAwsCall(f"a test tried to call AWS: {request.method} {request.url}")
    monkeypatch.setattr(botocore.endpoint.Endpoint, "_send", refuse)
    yield
    assert not attempts, f"tests must not call AWS: {attempts}"


# The Regions the tests treat as enabled unless a test sets its own.
TEST_REGIONS = ("us-east-1", "us-west-2")

# The real discovery functions, kept before the offline fixture replaces them.
_REAL = {}
_REAL_CB = {}


def _keep_real(module, attr):
    fn = getattr(module, attr)
    if getattr(fn, "__module__", None) == module.__name__:
        _REAL[(module.__name__, attr)] = fn


class _NoCacheTable:
    """Every read misses and every write is dropped (a lock claim succeeds)."""

    def get_item(self, **_kwargs):
        return {}

    def put_item(self, **_kwargs):
        return {}


@pytest.fixture(autouse=True)
def _offline_cache_table(monkeypatch):
    api = sys.modules.get("api")
    if api is not None:
        monkeypatch.setattr(api, "offering_cache_table", lambda: _NoCacheTable())
        # Memory layers of the DynamoDB-backed caches start empty in every test.
        for name in ("_price_cache", "_offering_mem", "_quota_cache", "_arch_cache", "_ami_cache", "_sps_mem",
                     "_region_names_mem"):
            monkeypatch.setattr(api, name, {})
        # The Region long-name lookup (SSM) knows no names; the live_region_names fixture puts the
        # real lookup back (its SSM client stays a test Stubber).
        _keep_real(api, "_fetch_region_names")
        monkeypatch.setattr(api, "_fetch_region_names", lambda codes: {})
        # A botocore Stubber answers in queue order: the two quota lists run one after the other
        # (defaults first) unless a test asks for the parallel path.
        monkeypatch.setattr(api, "QUOTA_LIST_WORKERS", 1)
        # The Region list while discovery has no answer is the function's own Region in a
        # deployment; tests use two Regions (a test may set its own).
        monkeypatch.setattr(api, "REGIONS", list(TEST_REGIONS))
        # Region discovery answers with api.REGIONS; the live_region_discovery fixture
        # puts the real lookup (cache item, lock, DescribeRegions) back.
        _keep_real(api, "_region_info")
        monkeypatch.setattr(api, "_regions_mem", None)
        monkeypatch.setattr(api, "_describe_regions", lambda: list(api.REGIONS))
        monkeypatch.setattr(api, "_region_info", lambda: {"regions": list(api.REGIONS), "fetchedAt": None})
        # Capacity Block type detection is unknown (the FAMILIES fallback); the live_cb_types fixture
        # puts the real lookup (cbtypes# item, DescribeInstanceTypes) back.
        if "cb_types" not in _REAL_CB:
            _REAL_CB["cb_types"] = api.cb_types
        monkeypatch.setattr(api, "_cb_types_mem", {})
        monkeypatch.setattr(api, "cb_types", lambda region: None)
    notifier = sys.modules.get("notifier")
    if notifier is not None:
        monkeypatch.setattr(notifier, "REGIONS", list(TEST_REGIONS))
        _keep_real(notifier, "enabled_regions")
        monkeypatch.setattr(notifier, "enabled_regions", lambda: list(notifier.REGIONS))
    yield



@pytest.fixture
def live_region_discovery(monkeypatch):
    """The real Region discovery of api and notifier (the DescribeRegions lookup stays a test stub)."""
    for (name, attr), fn in _REAL.items():
        if attr != "_fetch_region_names":
            monkeypatch.setattr(sys.modules[name], attr, fn)


@pytest.fixture
def live_cb_types(monkeypatch):
    """The real Capacity Block type detection of api (tests stub its EC2 client and cache table)."""
    monkeypatch.setattr(sys.modules["api"], "cb_types", _REAL_CB["cb_types"])


@pytest.fixture
def live_region_names(monkeypatch):
    """The real Region long-name lookup of api (tests stub its SSM client)."""
    monkeypatch.setattr(sys.modules["api"], "_fetch_region_names", _REAL[("api", "_fetch_region_names")])
