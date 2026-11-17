"""The capacity-hold feature is gone: no hold route, no hold setting, and no code path that calls
a Capacity Reservation API. The only billed resource the API creates is the bounded test launch."""
import inspect
import os

os.environ.update(REGIONS="us-east-1,us-west-2", SPOT_SCORE_FAMILIES="p5.*,g6e.*,g7*", TABLE_NAME="t")

import api  # noqa: E402
import notifier  # noqa: E402
import pytest  # noqa: E402


# The hold routes (404) and the /api/config hold fields are pinned in test_api.py
# (test_removed_routes_return_404_without_touching_aws, test_config_has_no_removed_fields).


@pytest.mark.parametrize("module", [api, notifier])
def test_no_capacity_reservation_call_or_setting_in_the_functions(module):
    source = inspect.getsource(module)
    for name in ("create_capacity_reservation", "cancel_capacity_reservation", "describe_capacity_reservations",
                 "HOLDS_TABLE_NAME", "ENABLE_RESERVATION", "RESERVATION_HOURS", "MAX_RESERVATIONS_PER_USER"):
        assert name not in source


def test_only_the_probe_creates_a_billed_resource():
    """The instant fleet of a test launch (then terminate) is the only create call that bills; the
    launch template and the security group are free."""
    import fleet_launch
    source = inspect.getsource(api) + inspect.getsource(fleet_launch)
    creates = {w for w in ("run_instances", "create_capacity_reservation", "purchase_capacity_block",
                           "request_spot_instances", "create_fleet", "allocate_hosts") if f".{w}(" in source}
    assert creates == {"create_fleet"}
