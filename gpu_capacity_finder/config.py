"""Parse and validate CDK context values in one testable place."""
import importlib.util
import re
from pathlib import Path

# The family rules live next to the Lambda code, so the stack, the API, the notifier, and the
# alert checks share one definition. "lambda" is a Python keyword, so the module cannot be
# imported by name and is loaded by path instead.
_RULE_PATH = Path(__file__).resolve().parent.parent / "lambda" / "families.py"
_RULE_SPEC = importlib.util.spec_from_file_location("gcf_families", _RULE_PATH)
families = importlib.util.module_from_spec(_RULE_SPEC)
_RULE_SPEC.loader.exec_module(families)

# `spotScoreFamilies`: the instance-type patterns whose families get Spot placement scores
# (lambda/families.py). AWS restricts how many different Spot placement score requests an account
# can make, and every family costs one request per GPU count and per 10 Regions that sell it, so
# the list is short and set by an administrator. Every other part of the page covers every GPU
# family by the naming rule, whatever this list says.
DEFAULT_SPOT_SCORE_FAMILIES = list(families.DEFAULT_SPOT_SCORE_PATTERNS)
MAX_FAMILIES = 20
# Every feature uses one Region list: the Regions the account has enabled, minus `excludeRegions`.
MAX_EXCLUDED_REGIONS = 40
# The Region whose EC2 endpoint receives every Spot placement score request (score_groups.ENDPOINT
# in lambda/score_groups.py); it cannot be excluded.
SPOT_SCORE_ENDPOINT = "us-east-1"
# Cognito rejects a domain prefix that contains these words; the prefix comes from stackName.
RESERVED_DOMAIN_WORDS = ("aws", "amazon", "cognito")
# Settings of the removed capacity-hold feature: refused, so a deployment that still sets them
# learns that they no longer do anything.
REMOVED_KEYS = ("enableReservation", "reservationHours", "maxReservationsPerUser")
# Settings of the configured Region list, which every enabled Region replaced.
REGION_LIST_KEYS = ("testLaunchRegions", "regions")
# Settings that became fixed values in the stack: refused with the value now used.
FIXED_KEYS = {
    "tokenMinutes": "the ID and access tokens now last 60 minutes",
    "apiBurstLimit": "the API burst limit is now 3 x apiRateLimit",
    "regionScope": "the pages cover every enabled Region; use excludeRegions to leave Regions out",
}
# Settings that were renamed: refused with the new name, so an old deploy command does not silently
# fall back to a default.
RENAMED_KEYS = {
    "enableProbe": "enableTestLaunch",
    "enableSpotProbe": "testLaunchAllowSpot",
    "enableSpotTestLaunch": "testLaunchAllowSpot",
    "probeCooldownMinutes": "testLaunchCooldownMinutes",
    "maxProbesPerUserPerDay": "maxTestLaunchesPerUserPerDay",
    "spotScoreNewConfigsPerDay": "spotScoreConfigsPerDay",
    "spotScoreReservePerDay": "spotScoreConfigsPerDay",
    "spotScoreMaxConfigs": "spotScoreConfigsPerDay",
    "instanceFamilies": "spotScoreFamilies",
}

STACK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,39}$")
# An existing SNS topic that receives the stack's CloudWatch alarm notifications. A standard topic
# ARN: arn:<partition>:sns:<region>:<account>:<name>, where the name is at most 256 characters of
# letters, digits, hyphens, and underscores (a FIFO topic ends in ".fifo" and cannot take alarm
# notifications, so the pattern leaves it out). It must be in the stack's own Region, because a
# CloudWatch alarm publishes only to a topic in its Region.
ALARM_TOPIC_ARN_RE = re.compile(r"^arn:aws[a-z0-9-]*:sns:[a-z]{2}(-[a-z]+)+-\d:\d{12}:[A-Za-z0-9_-]{1,256}$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
# Starts with a letter; only letters, digits, "-", "." and the wildcards "*" and "?".
FAMILY_RE = re.compile(r"^[a-z][a-z0-9*?-]{0,29}(\.[a-z0-9*?]{1,20})?$")


class ConfigError(ValueError):
    pass


def _csv(key, raw, pattern, default):
    if raw is None:
        return list(default)
    items = [i.strip() for i in str(raw).split(",") if i.strip()]
    if not items:
        raise ConfigError(f"{key} must list at least one value")
    for item in items:
        if not pattern.match(item):
            raise ConfigError(f"{key} has an invalid value: {item!r}")
    return items


def _families(raw):
    if raw is None:
        return list(DEFAULT_SPOT_SCORE_FAMILIES)
    items = _csv("spotScoreFamilies", raw, FAMILY_RE, [])
    for item in items:
        if not any(c in item for c in ".*?"):
            raise ConfigError(f"spotScoreFamilies has an invalid value: {item!r} (add a size or a wildcard, such as 'p5.*')")
        if not families.tracked(item):
            raise ConfigError(f"spotScoreFamilies has an invalid value: {item!r} "
                              f"(name a g or p family with its generation digit, such as 'g6e.*' or 'p6e-gb200.*'; "
                              f"letter-only patterns such as 'g*' are not accepted)")
    if len(items) > MAX_FAMILIES:
        raise ConfigError(f"spotScoreFamilies accepts at most {MAX_FAMILIES} patterns")
    return list(dict.fromkeys(items))


def _excluded(raw):
    items = list(dict.fromkeys(_csv("excludeRegions", raw, REGION_RE, [])))
    if SPOT_SCORE_ENDPOINT in items:
        raise ConfigError(f"excludeRegions must not list {SPOT_SCORE_ENDPOINT}: every Spot placement score request "
                          f"goes to the {SPOT_SCORE_ENDPOINT} endpoint, so the Spot scores need it")
    if len(items) > MAX_EXCLUDED_REGIONS:
        raise ConfigError(f"excludeRegions accepts at most {MAX_EXCLUDED_REGIONS} Regions")
    return items


def _bool(key, raw, default):
    if raw is None:
        return default
    if raw in (True, "true"):
        return True
    if raw in (False, "false"):
        return False
    raise ConfigError(f"{key} must be true or false")


def _int(key, raw, default, low, high):
    if raw is None:
        return default
    try:
        value = int(str(raw))
    except ValueError:
        raise ConfigError(f"{key} must be an integer")
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low} and {high}")
    return value


def _alarm_topic_arn(raw):
    """The SNS topic the alarms notify, or None. Without it the alarms still exist, with no action."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if not ALARM_TOPIC_ARN_RE.match(text):
        raise ConfigError("alarmTopicArn must be the ARN of a standard SNS topic in the stack Region "
                          "(arn:aws:sns:<region>:<account-id>:<name>)")
    return text


def parse_context(get):
    """`get` is a callable like node.try_get_context."""
    stack_name = get("stackName") or "gpu-capacity-finder"
    if not STACK_NAME_RE.match(str(stack_name)):
        raise ConfigError("stackName must start with a letter and use only letters, digits, and hyphens (max 40)")
    if any(word in str(stack_name).lower() for word in RESERVED_DOMAIN_WORDS):
        raise ConfigError(f"stackName must not contain {', '.join(RESERVED_DOMAIN_WORDS)} (it becomes the sign-in domain prefix)")
    if get("instanceTypes") is not None:
        raise ConfigError("instanceTypes was removed: the page covers every GPU family; spotScoreFamilies "
                          "(wildcard patterns such as 'p5.*') picks the families that get Spot scores")
    for key in REMOVED_KEYS:
        if get(key) is not None:
            raise ConfigError(f"{key} was removed: the sample no longer holds capacity; remove the setting")
    for key, now in FIXED_KEYS.items():
        if get(key) is not None:
            raise ConfigError(f"{key} was removed: {now}; remove the setting")
    for key, new in RENAMED_KEYS.items():
        if get(key) is not None:
            raise ConfigError(f"{key} was renamed to {new}; use -c {new}=... instead")
    for key in REGION_LIST_KEYS:
        if get(key) is not None:
            raise ConfigError(f"{key} was removed: test launches now run in every enabled Region; "
                              "use excludeRegions to leave Regions out")
    # Sign-in session: the refresh token lifetime, the longest time before the next full sign-in.
    # It is at least 1 hour, so it always outlasts the 60-minute ID and access tokens.
    session_hours = _int("sessionHours", get("sessionHours"), 12, 1, 720)
    # Default-route throttling of the HTTP API, shared by every signed-in user of the deployment.
    api_rate_limit = _int("apiRateLimit", get("apiRateLimit"), 10, 1, 100)
    score_families = _families(get("spotScoreFamilies"))
    return {
        "stack_name": str(stack_name),
        # Regions to leave out everywhere (for example a Region that does not answer): never listed,
        # never called by any feature, and removed from the Spot score groups at the next daily
        # update. Remove one to bring it back.
        "exclude_regions": _excluded(get("excludeRegions")),
        # The instance-type patterns whose families get Spot placement scores.
        "spot_score_families": score_families,
        # The probe role's ec2:InstanceType condition. StringLike cannot express "a digit follows",
        # so it holds the naming rule's wildcards ("g*", "p*"), and the API's check_family refuses a
        # type outside the rule (gr6, for example) before any launch request.
        "probe_type_patterns": families.filter_values(),
        "enable_probe": _bool("enableTestLaunch", get("enableTestLaunch"), True),
        # Set false when the account cannot use Spot: the Spot test launch is then hidden and refused.
        "enable_spot_probe": _bool("testLaunchAllowSpot", get("testLaunchAllowSpot"), True),
        "cooldown_minutes": _int("testLaunchCooldownMinutes", get("testLaunchCooldownMinutes"), 15, 1, 1440),
        # Real launch attempts one user may start per UTC day (cached results do not count); 0 turns the cap off.
        "max_probes_per_user_per_day": _int("maxTestLaunchesPerUserPerDay", get("maxTestLaunchesPerUserPerDay"), 50, 0, 1000),
        # Real launch attempts of all users together per UTC day (a deployment-wide counter).
        # 0 turns the cap off.
        "max_test_launches_per_day": _int("maxTestLaunchesPerDay", get("maxTestLaunchesPerDay"), 100, 0, 10000),
        "enable_notifications": _bool("enableNotifications", get("enableNotifications"), True),
        "notify_interval_minutes": _int("notifyIntervalMinutes", get("notifyIntervalMinutes"), 15, 5, 1440),
        "max_subscriptions_per_user": _int("maxSubscriptionsPerUser", get("maxSubscriptionsPerUser"), 10, 1, 20),
        # Hours the Region x family offering matrix stays in the DynamoDB cache.
        "offering_cache_hours": _int("offeringCacheHours", get("offeringCacheHours"), 6, 1, 48),
        # Minutes one Spot score answer stays current. The score updater runs every 15 minutes and
        # repeats an answer once it is this old, so a value above 15 refreshes less often.
        "spot_score_cache_minutes": _int("spotScoreCacheMinutes", get("spotScoreCacheMinutes"), 15, 15, 240),
        # The most different Spot placement score request configurations in use in a rolling 24
        # hours (AWS restricts how many different requests an account can make; repeating one is
        # allowed). The daily update grants within it; what does not fit waits as planned.
        "spot_score_configs_per_day": _int("spotScoreConfigsPerDay", get("spotScoreConfigsPerDay"), 40, 1, 100),
        # Minutes one Region's Service Quotas rows stay in the DynamoDB cache.
        "quota_cache_minutes": _int("quotaCacheMinutes", get("quotaCacheMinutes"), 30, 1, 60),
        "api_rate_limit": api_rate_limit,
        "session_hours": session_hours,
        # An existing SNS topic for the stack's CloudWatch alarms (the alert topic of the email
        # alerts is not used for them). Unset: the alarms exist with no notification action.
        "alarm_topic_arn": _alarm_topic_arn(get("alarmTopicArn")),
    }
