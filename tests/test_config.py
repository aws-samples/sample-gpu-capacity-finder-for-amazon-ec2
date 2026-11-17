import pytest

from gpu_capacity_finder.config import ConfigError, parse_context


def ctx(**values):
    return lambda key: values.get(key)


def test_defaults():
    cfg = parse_context(ctx())
    assert cfg["stack_name"] == "gpu-capacity-finder"
    assert cfg["enable_probe"] is True
    assert cfg["cooldown_minutes"] == 15
    # No configured Region list: every feature uses the enabled Regions minus excludeRegions.
    assert "regions" not in cfg and cfg["exclude_regions"] == []
    # The default Spot score families.
    assert cfg["spot_score_families"] == ["g6e.*", "g7.*", "g7e.*", "p5.*", "p5e.*", "p5en.*", "p6-b200.*", "p6-b300.*"]
    # The probe IAM condition covers the rule, because StringLike cannot express "a digit follows".
    assert cfg["probe_type_patterns"] == ["g*", "p*"]
    assert cfg["max_test_launches_per_day"] == 100 and cfg["spot_score_configs_per_day"] == 40


def test_overrides():
    cfg = parse_context(ctx(spotScoreFamilies="p5.*, g7*, p5.*", enableTestLaunch="false",
                            testLaunchCooldownMinutes="30", stackName="finder-2", maxTestLaunchesPerDay="7"))
    assert cfg["spot_score_families"] == ["p5.*", "g7*"]
    # The Spot score families never widen the probe condition.
    assert cfg["probe_type_patterns"] == ["g*", "p*"]
    assert cfg["enable_probe"] is False
    assert cfg["cooldown_minutes"] == 30 and cfg["max_test_launches_per_day"] == 7


@pytest.mark.parametrize("key,value", [
    ("stackName", "1bad"), ("stackName", "a" * 41), ("excludeRegions", "us-east"), ("excludeRegions", " , "),
    ("spotScoreFamilies", "p5"), ("spotScoreFamilies", "P5.*"), ("spotScoreFamilies", "*"), ("spotScoreFamilies", "p5.*;rm"),
    ("spotScoreFamilies", "p[45].*"), ("spotScoreFamilies", " , "), ("spotScoreFamilies", ",".join(f"p{i}*" for i in range(21))),
    ("spotScoreFamilies", "p5." + "x" * 21), ("spotScoreFamilies", "trn2.*"), ("spotScoreFamilies", "gr6.*"),
    ("instanceTypes", "p5.48xlarge"), ("enableTestLaunch", "yes"), ("testLaunchCooldownMinutes", "abc"),
    ("testLaunchCooldownMinutes", "0"), ("testLaunchCooldownMinutes", "1441"),
    ("stackName", "aws-gpu"), ("stackName", "my-Amazon-finder"), ("stackName", "CognitoTest"),
    ("maxTestLaunchesPerDay", "-1"), ("maxTestLaunchesPerDay", "10001"), ("maxTestLaunchesPerDay", "x"),
])
def test_rejects_bad_values(key, value):
    with pytest.raises(ConfigError, match=key):
        parse_context(ctx(**{key: value}))


@pytest.mark.parametrize("value", ["g*", "p*"])
def test_letter_only_family_patterns_are_rejected_with_the_rule(value):
    with pytest.raises(ConfigError, match="generation digit") as err:
        parse_context(ctx(spotScoreFamilies=value))
    assert "letter-only patterns" in str(err.value)


def test_capacity_hold_settings_are_gone():
    cfg = parse_context(ctx())
    assert not any("reservation" in k for k in cfg)


@pytest.mark.parametrize("key,value", [
    ("enableReservation", "true"), ("enableReservation", "false"), ("reservationHours", "1"), ("maxReservationsPerUser", "1"),
])
def test_removed_hold_settings_are_refused_with_a_clear_message(key, value):
    with pytest.raises(ConfigError, match=f"{key} was removed"):
        parse_context(ctx(**{key: value}))


def test_notification_defaults_and_overrides():
    cfg = parse_context(ctx())
    assert (cfg["enable_notifications"], cfg["notify_interval_minutes"], cfg["max_subscriptions_per_user"]) == (True, 15, 10)
    cfg = parse_context(ctx(enableNotifications="false", notifyIntervalMinutes="1440", maxSubscriptionsPerUser="20"))
    assert (cfg["enable_notifications"], cfg["notify_interval_minutes"], cfg["max_subscriptions_per_user"]) == (False, 1440, 20)
    cfg = parse_context(ctx(notifyIntervalMinutes="5", maxSubscriptionsPerUser="1"))
    assert (cfg["notify_interval_minutes"], cfg["max_subscriptions_per_user"]) == (5, 1)


@pytest.mark.parametrize("key,value", [
    ("enableNotifications", "yes"), ("enableNotifications", "0"), ("notifyIntervalMinutes", "4"),
    ("notifyIntervalMinutes", "1441"), ("notifyIntervalMinutes", "15m"), ("maxSubscriptionsPerUser", "0"),
    ("maxSubscriptionsPerUser", "21"), ("maxSubscriptionsPerUser", "2.5"),
])
def test_rejects_bad_notification_values(key, value):
    with pytest.raises(ConfigError, match=key):
        parse_context(ctx(**{key: value}))


@pytest.mark.parametrize("key", ["testLaunchRegions", "regions"])
def test_the_configured_region_list_is_refused_with_the_change(key):
    with pytest.raises(ConfigError, match=f"^{key} was removed: test launches now run in every enabled Region; "
                                          "use excludeRegions to leave Regions out"):
        parse_context(ctx(**{key: "us-east-1"}))


def test_excluded_regions_are_deduplicated():
    assert parse_context(ctx(excludeRegions="me-south-1,af-south-1,me-south-1"))["exclude_regions"] == [
        "me-south-1", "af-south-1"]


def test_spot_probe_switch():
    assert parse_context(ctx())["enable_spot_probe"] is True
    assert parse_context(ctx(testLaunchAllowSpot="false"))["enable_spot_probe"] is False
    with pytest.raises(ConfigError, match="testLaunchAllowSpot"):
        parse_context(ctx(testLaunchAllowSpot="no"))


def test_offering_cache_hours_default_and_override():
    assert parse_context(ctx())["offering_cache_hours"] == 6
    assert parse_context(ctx(offeringCacheHours="1"))["offering_cache_hours"] == 1
    assert parse_context(ctx(offeringCacheHours=48))["offering_cache_hours"] == 48


@pytest.mark.parametrize("value", ["0", "49", "abc", "1.5", "-3"])
def test_offering_cache_hours_rejects_bad_values(value):
    with pytest.raises(ConfigError, match="offeringCacheHours"):
        parse_context(ctx(offeringCacheHours=value))


def test_session_defaults_and_overrides():
    assert parse_context(ctx())["session_hours"] == 12
    assert parse_context(ctx(sessionHours="1"))["session_hours"] == 1
    assert parse_context(ctx(sessionHours=720))["session_hours"] == 720


@pytest.mark.parametrize("value", ["0", "721", "x", "-1"])
def test_rejects_bad_session_values(value):
    with pytest.raises(ConfigError, match="sessionHours"):
        parse_context(ctx(sessionHours=value))


def test_spot_score_cache_minutes_default_and_override():
    assert parse_context(ctx())["spot_score_cache_minutes"] == 15
    assert parse_context(ctx(spotScoreCacheMinutes="15"))["spot_score_cache_minutes"] == 15
    assert parse_context(ctx(spotScoreCacheMinutes=240))["spot_score_cache_minutes"] == 240


# Never below the 15-minute warm run.
@pytest.mark.parametrize("value", ["5", "14", "241", "0", "-5", "abc", "1.5", ""])
def test_rejects_bad_spot_score_cache_minutes(value):
    with pytest.raises(ConfigError, match="spotScoreCacheMinutes"):
        parse_context(ctx(spotScoreCacheMinutes=value))


def test_quota_cache_minutes_default_and_override():
    assert parse_context(ctx())["quota_cache_minutes"] == 30
    assert parse_context(ctx(quotaCacheMinutes="1"))["quota_cache_minutes"] == 1
    assert parse_context(ctx(quotaCacheMinutes=60))["quota_cache_minutes"] == 60


@pytest.mark.parametrize("value", ["0", "61", "-1", "abc", "1.5", ""])
def test_rejects_bad_quota_cache_minutes(value):
    with pytest.raises(ConfigError, match="quotaCacheMinutes"):
        parse_context(ctx(quotaCacheMinutes=value))


def test_api_rate_limit_default_and_overrides():
    assert parse_context(ctx())["api_rate_limit"] == 10
    assert parse_context(ctx(apiRateLimit="100"))["api_rate_limit"] == 100
    assert parse_context(ctx(apiRateLimit=1))["api_rate_limit"] == 1


@pytest.mark.parametrize("value", ["0", "101", "x", "2.5"])
def test_rejects_bad_api_rate_limit(value):
    with pytest.raises(ConfigError, match="apiRateLimit"):
        parse_context(ctx(apiRateLimit=value))


# Settings that became fixed values: refused with a message naming the value now used.
@pytest.mark.parametrize("key,value,now", [
    ("tokenMinutes", "60", "60 minutes"), ("tokenMinutes", "15", "60 minutes"), ("tokenMinutes", 1440, "60 minutes"),
    ("apiBurstLimit", "30", "3 x apiRateLimit"), ("apiBurstLimit", "200", "3 x apiRateLimit"), ("apiBurstLimit", "", "3 x apiRateLimit"),
])
def test_fixed_settings_are_refused_with_a_clear_message(key, value, now):
    with pytest.raises(ConfigError, match=f"^{key} was removed: .*{now}"):
        parse_context(ctx(**{key: value}))


def test_fixed_settings_leave_no_config_entry():
    cfg = parse_context(ctx())
    assert "token_minutes" not in cfg and "api_burst_limit" not in cfg


# Every context key the stack reads, and nothing else.
SETTINGS = {
    "excludeRegions": "me-south-1",
    "stackName": "finder-2", "spotScoreFamilies": "p5.*", "enableTestLaunch": "true",
    "testLaunchAllowSpot": "false", "testLaunchCooldownMinutes": "20", "maxTestLaunchesPerUserPerDay": "5",
    "maxTestLaunchesPerDay": "50",
    "enableNotifications": "false", "notifyIntervalMinutes": "30", "maxSubscriptionsPerUser": "3",
    "offeringCacheHours": "2", "spotScoreCacheMinutes": "60", "quotaCacheMinutes": "5", "sessionHours": "2",
    "apiRateLimit": "4", "spotScoreConfigsPerDay": "12", "alarmTopicArn": "arn:aws:sns:us-east-1:123456789012:ops-alarms",
}


def test_the_stack_reads_exactly_the_18_settings_of_v4():
    asked = []
    parse_context(lambda key: asked.append(key) or SETTINGS.get(key))
    removed = {"instanceTypes", "enableReservation", "reservationHours", "maxReservationsPerUser", "tokenMinutes", "apiBurstLimit",
               "regionScope", "regions", "testLaunchRegions", "instanceFamilies", "enableSpotTestLaunch", "enableProbe",
               "enableSpotProbe", "probeCooldownMinutes", "maxProbesPerUserPerDay",
               "spotScoreNewConfigsPerDay", "spotScoreReservePerDay", "spotScoreMaxConfigs"}
    assert set(asked) - removed == set(SETTINGS)
    assert len(SETTINGS) == 18
    cfg = parse_context(lambda key: SETTINGS.get(key))
    # One entry per setting, plus probe_type_patterns, which the rule derives rather than reads.
    assert len(cfg) == 19


def test_max_probes_per_user_per_day_default_and_override():
    assert parse_context(ctx())["max_probes_per_user_per_day"] == 50
    assert parse_context(ctx(maxTestLaunchesPerUserPerDay="0"))["max_probes_per_user_per_day"] == 0  # 0 turns the cap off
    assert parse_context(ctx(maxTestLaunchesPerUserPerDay="1000"))["max_probes_per_user_per_day"] == 1000
    assert parse_context(ctx(maxTestLaunchesPerUserPerDay=7))["max_probes_per_user_per_day"] == 7


@pytest.mark.parametrize("value", ["-1", "1001", "abc", "1.5", ""])
def test_rejects_bad_max_probes_per_user_per_day(value):
    with pytest.raises(ConfigError, match="maxTestLaunchesPerUserPerDay"):
        parse_context(ctx(maxTestLaunchesPerUserPerDay=value))


def test_spot_score_budget_is_one_setting_without_a_reserve():
    assert parse_context(ctx())["spot_score_configs_per_day"] == 40
    assert parse_context(ctx(spotScoreConfigsPerDay="100"))["spot_score_configs_per_day"] == 100
    assert parse_context(ctx(spotScoreConfigsPerDay=1))["spot_score_configs_per_day"] == 1
    assert not any("reserve" in k for k in parse_context(ctx()))


@pytest.mark.parametrize("value", ["0", "101", "x", "2.5", ""])
def test_rejects_bad_spot_score_configs_per_day(value):
    with pytest.raises(ConfigError, match="spotScoreConfigsPerDay"):
        parse_context(ctx(spotScoreConfigsPerDay=value))


@pytest.mark.parametrize("old,new", [
    ("enableProbe", "enableTestLaunch"), ("enableSpotProbe", "testLaunchAllowSpot"),
    ("enableSpotTestLaunch", "testLaunchAllowSpot"), ("instanceFamilies", "spotScoreFamilies"),
    ("probeCooldownMinutes", "testLaunchCooldownMinutes"), ("maxProbesPerUserPerDay", "maxTestLaunchesPerUserPerDay"),
    ("spotScoreNewConfigsPerDay", "spotScoreConfigsPerDay"), ("spotScoreReservePerDay", "spotScoreConfigsPerDay"),
    ("spotScoreMaxConfigs", "spotScoreConfigsPerDay"),
])
def test_renamed_settings_are_refused_with_the_new_name(old, new):
    with pytest.raises(ConfigError, match=f"^{old} was renamed to {new}"):
        parse_context(ctx(**{old: "1"}))


# -------------------------------------------------- the alarm topic
@pytest.mark.parametrize("value", [
    "arn:aws:sns:us-east-1:123456789012:ops-alarms",
    "arn:aws:sns:ap-southeast-3:123456789012:Ops_Alarms-1",
    "arn:aws-cn:sns:cn-north-1:123456789012:ops",
    " arn:aws:sns:us-east-1:123456789012:ops-alarms ",
])
def test_an_sns_topic_arn_is_accepted_and_trimmed(value):
    assert parse_context(ctx(alarmTopicArn=value))["alarm_topic_arn"] == value.strip()


def test_no_alarm_topic_means_no_action():
    assert parse_context(ctx())["alarm_topic_arn"] is None
    # An empty setting reads as unset, so a blank -c value does not fail the deployment.
    assert parse_context(ctx(alarmTopicArn=""))["alarm_topic_arn"] is None
    assert parse_context(ctx(alarmTopicArn="   "))["alarm_topic_arn"] is None


@pytest.mark.parametrize("value", [
    "ops-alarms",                                            # a name, not an ARN
    "arn:aws:sqs:us-east-1:123456789012:ops-alarms",         # another service
    "arn:aws:sns:us-east-1:12345:ops-alarms",                # not a 12-digit account ID
    "arn:aws:sns:us-east-1:123456789012:ops alarms",         # a space in the name
    "arn:aws:sns:us-east-1:123456789012:ops.fifo",           # a FIFO topic takes no alarm action
    "arn:aws:sns:useast1:123456789012:ops-alarms",           # not a Region name
    "arn:aws:sns:us-east-1:123456789012:",                   # no name
])
def test_rejects_anything_that_is_not_a_standard_sns_topic_arn(value):
    with pytest.raises(ConfigError, match="alarmTopicArn must be the ARN of a standard SNS topic"):
        parse_context(ctx(alarmTopicArn=value))


def test_the_deployment_wide_test_launch_cap_can_be_turned_off():
    assert parse_context(ctx(maxTestLaunchesPerDay="0"))["max_test_launches_per_day"] == 0


@pytest.mark.parametrize("value", ["us-east-1", "me-south-1,us-east-1"])
def test_exclude_regions_refuses_the_spot_score_endpoint(value):
    with pytest.raises(ConfigError, match="excludeRegions must not list us-east-1.*Spot placement score"):
        parse_context(ctx(excludeRegions=value))
