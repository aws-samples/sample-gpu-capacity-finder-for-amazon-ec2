import functools
import json
import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from gpu_capacity_finder.config import parse_context
from gpu_capacity_finder.stack import FinderStack


def synth(**ctx):
    """The synthesised template for these context values. Tests only read a Template, so one synth
    per distinct context serves every test that asks for it (site_config below synthesises its own)."""
    return _synth(frozenset(ctx.items()))


@functools.lru_cache(maxsize=None)
def _synth(ctx):
    ctx = dict(ctx)
    app = cdk.App()
    cfg = parse_context(lambda k: ctx.get(k))
    stack = FinderStack(app, "t", cfg=cfg, env=cdk.Environment(account="123456789012", region="us-east-1"))
    return Template.from_stack(stack)


def statements(template):
    # CDK moves statements past the inline size limit into overflow managed policies; read both.
    out = []
    for kind in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        for policy in template.find_resources(kind).values():
            out += policy["Properties"]["PolicyDocument"]["Statement"]
    return out


def test_mfa_required_and_no_self_signup():
    t = synth()
    t.has_resource_properties("AWS::Cognito::UserPool", {
        "MfaConfiguration": "ON", "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}})


def test_launch_is_scoped_to_tag_and_families_in_every_region():
    t = synth(spotScoreFamilies="p5.*,g7*")
    launch = [s for s in statements(t) if s.get("Sid") == "LaunchTaggedProbeInstance"][0]
    # Test launches run in every enabled Region: one instance ARN for any Region.
    assert len(launch["Resource"]) == 1 and ":ec2:*:" in json.dumps(launch["Resource"])
    # The rule's coarse wildcards (StringLike cannot express "a digit follows"); spotScoreFamilies
    # never widens them. The API refuses a type the rule leaves out before it launches anything.
    assert launch["Condition"]["StringLike"]["ec2:InstanceType"] == ["g*", "p*"]
    assert "ec2:InstanceType" not in launch["Condition"]["StringEquals"]
    assert launch["Condition"]["StringEquals"]["aws:RequestTag/gpu-capacity-finder-probe"] == "true"


def test_launch_condition_covers_the_rule_without_any_extra_setting():
    launch = [s for s in statements(synth()) if s.get("Sid") == "LaunchTaggedProbeInstance"][0]
    assert launch["Condition"]["StringLike"]["ec2:InstanceType"] == ["g*", "p*"]


@pytest.mark.parametrize("ctx", [{}, {"enableNotifications": "false"}, {"enableTestLaunch": "false"}])
def test_no_server_side_instance_type_list_table(ctx):
    """The page keeps its instance-type list in the browser: no Watchlist table, env var, or grant."""
    t = synth(**ctx)
    tables = t.find_resources("AWS::DynamoDB::Table")
    assert not [k for k in tables if k.startswith("Watchlist")]
    # The only userId-keyed table is Subscriptions (userId + sk); no table has userId alone.
    assert not [k for k, v in tables.items() if v["Properties"]["KeySchema"] == [{"AttributeName": "userId", "KeyType": "HASH"}]]
    assert len(tables) == (3 if ctx.get("enableNotifications") != "false" else 2)
    assert "Watchlist" not in json.dumps(t.to_json())
    assert "WATCHLIST" not in json.dumps(t.find_resources("AWS::Lambda::Function"))


def test_api_role_deletes_items_only_for_alerts():
    """DeleteItem now serves only the alert subscriptions; without alerts the API role has none."""
    assert "dynamodb:DeleteItem" not in _api_actions(synth(enableNotifications="false"))
    assert "dynamodb:DeleteItem" in _api_actions(synth())


def test_offering_cache_table_and_narrow_grant():
    t = synth(offeringCacheHours="12")
    tables = t.find_resources("AWS::DynamoDB::Table")
    cache = [k for k, v in tables.items() if v["Properties"]["KeySchema"] == [{"AttributeName": "cacheKey", "KeyType": "HASH"}]]
    assert len(cache) == 1
    props = tables[cache[0]]["Properties"]
    assert props["TimeToLiveSpecification"] == {"AttributeName": "expires", "Enabled": True}
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert tables[cache[0]]["DeletionPolicy"] == "Delete"
    actions = set()
    for s in statements(t):
        if cache[0] in str(s["Resource"]):
            actions |= set(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
            resources = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
            assert "*" not in resources
            assert all("Fn::GetAtt" in r or "Ref" in r for r in resources)
    assert actions == {"dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem"}
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "OFFERING_CACHE_TABLE_NAME": {"Ref": cache[0]}, "OFFERING_CACHE_HOURS": "12"})}})


def test_quota_usage_read_covers_every_enabled_region():
    """Quotas answer for any enabled Region, so GetMetricData (no resource-level permissions) has no
    Region condition; it stays the only CloudWatch action."""
    t = synth(excludeRegions="ap-northeast-1")
    usage = [s for s in statements(t) if s.get("Sid") == "ReadQuotaUsage"][0]
    assert usage["Action"] == "cloudwatch:GetMetricData" and usage["Resource"] == "*"
    assert "Condition" not in usage
    # GetMetricData is the only CloudWatch action the API role holds.
    cw = {a for s in statements(t) for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
          if a.startswith("cloudwatch:")}
    assert cw == {"cloudwatch:GetMetricData"}


def test_api_routes_and_cors_allow_only_the_methods_the_api_handles():
    t = synth()
    apis = t.find_resources("AWS::ApiGatewayV2::Api")
    methods = [set(a["Properties"]["CorsConfiguration"]["AllowMethods"]) for a in apis.values()]
    assert methods == [{"GET", "POST", "DELETE"}]
    keys = {r["Properties"]["RouteKey"] for r in t.find_resources("AWS::ApiGatewayV2::Route").values()}
    assert keys == {"GET /api/{proxy+}", "POST /api/{proxy+}", "DELETE /api/{proxy+}"}


def test_no_ec2_write_statement_uses_wildcard_resource():
    t = synth()
    for s in statements(t):
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        if any(a in ("ec2:RunInstances", "ec2:TerminateInstances", "ec2:CreateTags") for a in actions):
            assert s["Resource"] != "*"


def test_probe_disabled_removes_launch_permissions_and_cleanup():
    t = synth(enableTestLaunch="false")
    assert not any("ec2:RunInstances" in str(s["Action"]) for s in statements(t))
    # The alert and score updater schedules are left.
    t.resource_count_is("AWS::Events::Rule", 2)
    assert "CleanupFunction" not in str(t.find_resources("AWS::Events::Rule"))


def test_bucket_is_private_and_tls_only():
    t = synth()
    t.has_resource_properties("AWS::S3::Bucket", {"PublicAccessBlockConfiguration": Match.object_like({"BlockPublicAcls": True})})
    t.has_resource_properties("AWS::S3::BucketPolicy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Condition": {"Bool": {"aws:SecureTransport": "false"}}})])}})


def by_sid(template, sid):
    return [s for s in statements(template) if s.get("Sid") == sid]


HOLD_ACTIONS = {"ec2:CreateCapacityReservation", "ec2:CancelCapacityReservation", "ec2:DescribeCapacityReservations",
                "ec2:ModifyCapacityReservation"}


@pytest.mark.parametrize("ctx", [{}, {"enableTestLaunch": "false"}, {"enableNotifications": "false"}])
def test_no_capacity_reservation_permission_or_hold_table(ctx):
    """The sample holds no capacity: no Capacity Reservation action, no CreateTags for one, no
    Holds table, and no hold settings on the function."""
    t = synth(**ctx)
    for s in statements(t):
        acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        assert not HOLD_ACTIONS & set(acts)
        assert "CapacityReservation" not in json.dumps(s)
        assert "capacity-reservation" not in json.dumps(s["Resource"])
    assert not [k for k in t.find_resources("AWS::DynamoDB::Table") if k.startswith("Holds")]
    env = json.dumps(t.find_resources("AWS::Lambda::Function"))
    for name in ("HOLDS_TABLE_NAME", "ENABLE_RESERVATION", "RESERVATION_HOURS", "MAX_RESERVATIONS_PER_USER"):
        assert name not in env


def test_read_statement_adds_instance_types_and_list_prices_only():
    read = by_sid(synth(), "ReadCapacitySignals")[0]
    assert read["Resource"] == "*" and "Condition" not in read
    assert set(read["Action"]) == {
        "ec2:DescribeCapacityBlockOfferings", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeSpotPriceHistory",
        "ec2:GetSpotPlacementScores", "ec2:DescribeAvailabilityZones", "ec2:DescribeInstanceTypes",
        "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeRegions",
        "servicequotas:ListServiceQuotas", "servicequotas:ListAWSDefaultServiceQuotas", "pricing:GetProducts",
    }
    # pricing:GetProducts is the only Price List action.
    assert {a for a in _api_actions(synth()) if a.startswith("pricing:")} == {"pricing:GetProducts"}


def test_probe_image_parameters_cover_both_architectures_in_every_region():
    t = synth()
    image = by_sid(t, "ReadProbeImage")[0]
    assert image["Action"] == "ssm:GetParameter"
    names = [json.dumps(r) for r in image["Resource"]]
    assert len(names) == 2
    for arch in ("x86_64", "arm64"):
        assert sum(f":ssm:*::parameter/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}" in n
                   for n in names) == 1


def test_nag_reason_names_the_new_reads_and_no_hold():
    """Every API role suppression reason (inline and overflow policies) names the newer reads and the
    Region name parameters, and no hold."""
    t = synth()
    reasons = []
    for kind in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        for k, v in t.find_resources(kind).items():
            if k.startswith("ApiRole"):
                reasons += [r["reason"] for r in v.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])]
    assert reasons and all("pricing:GetProducts" in r and "DescribeInstanceTypes" in r for r in reasons)
    assert all("global-infrastructure/regions/*" in r and "ssm:GetParameters" in r for r in reasons)
    assert not any("Reservation" in r or "hold" in r.lower() for r in reasons)


def test_alert_topic_is_encrypted_and_ssl_only():
    t = synth()
    t.has_resource_properties("AWS::SNS::Topic", {"KmsMasterKeyId": Match.any_value()})
    t.has_resource_properties("AWS::SNS::TopicPolicy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Effect": "Deny", "Condition": {"Bool": {"aws:SecureTransport": "false"}}})])}})
    t.has_resource_properties("AWS::DynamoDB::Table", {"KeySchema": [
        {"AttributeName": "userId", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}]})


def test_alert_permissions_are_scoped():
    t = synth()
    topic_ref = {"Ref": next(k for k in t.find_resources("AWS::SNS::Topic"))}
    sub = by_sid(t, "SubscribeEmailToAlertTopic")[0]
    assert sub["Action"] == "sns:Subscribe" and sub["Resource"] == topic_ref
    assert sub["Condition"] == {"StringEquals": {"sns:Protocol": "email"}}
    pub = by_sid(t, "PublishAlerts")[0]
    assert pub["Action"] == "sns:Publish" and pub["Resource"] == topic_ref
    read = by_sid(t, "ReadAlertSignals")[0]
    # Alerts may target any enabled Region: read-only actions, no Region condition, plus DescribeRegions.
    # Spot score alerts read the score updater's cache: no GetSpotPlacementScores.
    assert set(read["Action"]) == {"ec2:DescribeCapacityBlockOfferings", "ec2:DescribeRegions"}
    assert read["Resource"] == "*" and "Condition" not in read
    # No SNS or EC2 write action beyond these.
    for s in statements(t):
        acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        assert not any(a.startswith("sns:") and a not in (
            "sns:Subscribe", "sns:Publish", "sns:Unsubscribe", "sns:GetSubscriptionAttributes") for a in acts)
    topic_scope = [topic_ref, {"Fn::Join": ["", [topic_ref, ":*"]]}]
    unsub = by_sid(t, "UnsubscribeFromAlertTopic")[0]
    assert unsub["Action"] == "sns:Unsubscribe" and unsub["Resource"] == topic_scope
    state = by_sid(t, "ReadAlertSubscriptionState")[0]
    assert state["Action"] == "sns:GetSubscriptionAttributes" and state["Resource"] == topic_scope
    table_actions = set()
    for s in statements(t):
        if "Subscriptions" in str(s["Resource"]):
            table_actions |= set(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    assert table_actions == {"dynamodb:Query", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Scan", "dynamodb:UpdateItem"}
    # Only the notifier scans; the API role has no Scan.
    api_policy = [p for k, p in t.find_resources("AWS::IAM::Policy").items() if k.startswith("ApiRole")][0]
    assert "dynamodb:Scan" not in str(api_policy)


def test_notifier_schedule_and_settings():
    t = synth(notifyIntervalMinutes="30", maxSubscriptionsPerUser="4")
    t.has_resource_properties("AWS::Events::Rule", {"ScheduleExpression": "rate(30 minutes)"})
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "notifier.handler", "Timeout": 240})
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "ENABLE_NOTIFICATIONS": "true", "NOTIFY_INTERVAL_MINUTES": "30", "MAX_SUBSCRIPTIONS_PER_USER": "4"})}})
    t.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})
    t.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "DELETE /api/{proxy+}"})
    t.has_resource_properties("AWS::ApiGatewayV2::Api", {"CorsConfiguration": Match.object_like({
        "AllowMethods": Match.array_with(["DELETE"])})})


def test_notifications_disabled_removes_alert_resources():
    t = synth(enableNotifications="false")
    t.resource_count_is("AWS::SNS::Topic", 0)
    assert not any("sns:" in str(s["Action"]) for s in statements(t))
    assert "notifier.handler" not in str(t.find_resources("AWS::Lambda::Function"))
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "ENABLE_NOTIFICATIONS": "false"})}})


def _stage_throttle(template):
    stages = template.find_resources("AWS::ApiGatewayV2::Stage")
    assert len(stages) == 1
    settings = next(iter(stages.values()))["Properties"]["DefaultRouteSettings"]
    return settings["ThrottlingRateLimit"], settings["ThrottlingBurstLimit"]


@pytest.mark.parametrize("rate,burst", [(None, 30), ("1", 3), ("7", 21), ("25", 75), ("100", 300)])
def test_stage_burst_is_three_times_the_rate(rate, burst):
    """The default (10 requests per second, burst 30) covers the page's fan-out."""
    ctx = {"apiRateLimit": rate} if rate else {}
    assert _stage_throttle(synth(**ctx)) == (int(rate or 10), burst)


def test_cache_and_throttle_settings_reach_the_api_function():
    t = synth(quotaCacheMinutes="20", apiRateLimit="5")
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "QUOTA_CACHE_MINUTES": "20", "API_RATE_LIMIT": "5"})}})
    synth().has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "QUOTA_CACHE_MINUTES": "30", "API_RATE_LIMIT": "10"})}})
    assert "API_BURST_LIMIT" not in json.dumps(t.find_resources("AWS::Lambda::Function"))


def _arn_suffixes(resources):
    """The resource part of each Fn::Join EC2 ARN (for example "security-group/*")."""
    resources = resources if isinstance(resources, list) else [resources]
    return {r["Fn::Join"][1][-1].rsplit(":", 1)[-1] for r in resources}


def test_probe_security_group_is_created_with_tag_condition_only():
    t = synth()
    sg = by_sid(t, "CreateProbeSecurityGroup")[0]
    assert sg["Action"] == "ec2:CreateSecurityGroup"
    assert sg["Condition"] == {"StringEquals": {"aws:RequestTag/gpu-capacity-finder-probe-sg": "true"}}
    # The request-tag condition covers the new group only; the VPC has its own statement without it.
    assert len(sg["Resource"]) == 1 and _arn_suffixes(sg["Resource"]) == {"security-group/*"}
    vpc = by_sid(t, "CreateProbeSecurityGroupInVpc")[0]
    assert vpc["Action"] == "ec2:CreateSecurityGroup" and "Condition" not in vpc
    assert len(vpc["Resource"]) == 1 and _arn_suffixes(vpc["Resource"]) == {"vpc/*"}
    # No other statement grants CreateSecurityGroup.
    creates = [s for s in statements(t) if "ec2:CreateSecurityGroup" in str(s["Action"])]
    assert {s["Sid"] for s in creates} == {"CreateProbeSecurityGroup", "CreateProbeSecurityGroupInVpc"}
    t_off = synth(enableTestLaunch="false")
    assert not any("ec2:CreateSecurityGroup" in str(s["Action"]) for s in statements(t_off))


def _probe_table(template):
    tables = template.find_resources("AWS::DynamoDB::Table")
    return [k for k in tables if k.startswith("Probes")][0]


def test_daily_probe_counter_grant_is_scoped_to_counter_items():
    t = synth()
    probes = _probe_table(t)
    count = by_sid(t, "CountDailyProbes")[0]
    assert count["Action"] == "dynamodb:UpdateItem"
    assert count["Resource"] == {"Fn::GetAtt": [probes, "Arn"]}
    # The per-user counters and the deployment-wide counter (maxTestLaunchesPerDay).
    assert count["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["#probe-count#*", "#probes#*"]}}
    # PutItem (probe records and cooldown locks) is kept off the counter partitions, so it cannot
    # overwrite a count.
    put = by_sid(t, "StoreProbeRecords")[0]
    assert put["Action"] == "dynamodb:PutItem"
    assert put["Resource"] == {"Fn::GetAtt": [probes, "Arn"]}
    assert put["Condition"] == {"ForAllValues:StringNotLike": {"dynamodb:LeadingKeys": ["#probe-count#*", "#probes#*"]}}
    for s in _api_statements(t):
        acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        if probes in str(s["Resource"]) and "dynamodb:PutItem" in acts:
            assert s.get("Sid") == "StoreProbeRecords"
    # UpdateItem on the probe table comes only from that statement.
    for s in statements(t):
        acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        if probes in str(s["Resource"]) and "dynamodb:UpdateItem" in acts:
            assert s.get("Sid") == "CountDailyProbes"
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "MAX_PROBES_PER_USER_PER_DAY": "50"})}})


def test_daily_probe_cap_setting_reaches_the_function_and_zero_drops_the_grant():
    # One cap still on keeps the counter grant; both off drop it.
    assert by_sid(synth(maxTestLaunchesPerUserPerDay="0"), "CountDailyProbes")
    assert by_sid(synth(maxTestLaunchesPerDay="0"), "CountDailyProbes")
    assert not by_sid(synth(maxTestLaunchesPerUserPerDay="0", maxTestLaunchesPerDay="0"), "CountDailyProbes")
    t = synth(maxTestLaunchesPerUserPerDay="0")
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "MAX_PROBES_PER_USER_PER_DAY": "0"})}})
    synth(maxTestLaunchesPerUserPerDay="7").has_resource_properties("AWS::Lambda::Function", {
        "Handler": "api.handler", "Environment": {"Variables": Match.object_like({"MAX_PROBES_PER_USER_PER_DAY": "7"})}})
    assert not by_sid(synth(enableTestLaunch="false"), "CountDailyProbes")


def test_web_client_allows_only_the_hosted_code_flow_and_refresh():
    """No SRP, password, or custom auth flow: tokens with the aws.cognito.signin.user.admin scope
    (UpdateUserAttributes, DeleteUser) cannot be obtained through this client."""
    p = _client(synth())
    assert p["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]
    assert p["AllowedOAuthFlows"] == ["code"] and p["AllowedOAuthScopes"] == ["openid", "email"]
    assert "aws.cognito.signin.user.admin" not in str(p)
    assert p["WriteAttributes"] == []
    # The API reads email and email_verified from the ID token.
    assert sorted(p["ReadAttributes"]) == ["email", "email_verified"]
    assert p["GenerateSecret"] is False


def test_notifier_reads_the_shared_spot_score_cache():
    t = synth(spotScoreCacheMinutes="45")
    tables = t.find_resources("AWS::DynamoDB::Table")
    cache = [k for k, v in tables.items() if v["Properties"]["KeySchema"] == [{"AttributeName": "cacheKey", "KeyType": "HASH"}]][0]
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "notifier.handler", "Environment": {"Variables": Match.object_like({
        "OFFERING_CACHE_TABLE_NAME": {"Ref": cache}})}})
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "score_updater.handler", "Environment": {
        "Variables": Match.object_like({"OFFERING_CACHE_TABLE_NAME": {"Ref": cache}, "SPOT_SCORE_CACHE_MINUTES": "45"})}})
    notifier = [k for k in t.find_resources("AWS::IAM::Role") if k.startswith("NotifierRole")][0]
    actions = set()
    for policy in t.find_resources("AWS::IAM::Policy").values():
        if {"Ref": notifier} in policy["Properties"].get("Roles", []):
            for s in policy["Properties"]["PolicyDocument"]["Statement"]:
                if cache in str(s["Resource"]):
                    actions |= set(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    assert actions == {"dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem"}


def test_nag_suppressions_are_resource_level_only():
    """No stack-level suppression, no suppression for a rule that does not fire (COG3), and the
    API role's overflow policies carry the role's own reason."""
    t = synth()
    assert "cdk_nag" not in t.to_json().get("Metadata", {})
    pools = t.find_resources("AWS::Cognito::UserPool")
    rules = {r["id"] for v in pools.values() for r in v.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])}
    assert rules == {"AwsSolutions-COG8"}
    # The API role carries its own reason; any overflow policy CDK splits off carries it too.
    role = [v for k, v in t.find_resources("AWS::IAM::Role").items() if k.startswith("ApiRole")][0]
    reasons = {r["id"]: r["reason"] for r in role["Metadata"]["cdk_nag"]["rules_to_suppress"]}
    assert "vpc/*" in reasons["AwsSolutions-IAM5"] and "lambda:InvokeFunction" in reasons["AwsSolutions-IAM5"]
    overflow = [v for k, v in t.find_resources("AWS::IAM::ManagedPolicy").items() if k.startswith("ApiRoleOverflowPolicy")]
    for v in overflow:
        reasons = {r["id"]: r["reason"] for r in v["Metadata"]["cdk_nag"]["rules_to_suppress"]}
        assert "vpc/*" in reasons["AwsSolutions-IAM5"] and "image/*" in reasons["AwsSolutions-IAM5"]


def test_spot_probe_switch_reaches_lambda():
    t = synth(testLaunchAllowSpot="false")
    fns = t.find_resources("AWS::Lambda::Function")
    assert any(f["Properties"].get("Environment", {}).get("Variables", {}).get("TEST_LAUNCH_ALLOW_SPOT") == "false"
               for f in fns.values())


def _client(t):
    clients = t.find_resources("AWS::Cognito::UserPoolClient")
    assert len(clients) == 1
    return next(iter(clients.values()))["Properties"]


def test_session_lengths_default():
    p = _client(synth())
    assert (p["IdTokenValidity"], p["AccessTokenValidity"], p["RefreshTokenValidity"]) == (60, 60, 720)
    assert p["TokenValidityUnits"] == {"IdToken": "minutes", "AccessToken": "minutes", "RefreshToken": "minutes"}
    assert p["EnableTokenRevocation"] is True


@pytest.mark.parametrize("hours", ["1", "24", "720"])
def test_session_hours_override_keeps_60_minute_tokens(hours):
    p = _client(synth(sessionHours=hours))
    assert (p["IdTokenValidity"], p["AccessTokenValidity"], p["RefreshTokenValidity"]) == (60, 60, int(hours) * 60)
    assert p["TokenValidityUnits"] == {"IdToken": "minutes", "AccessToken": "minutes", "RefreshToken": "minutes"}


def _api_statements(template):
    """Every IAM statement of the API role (inline and overflow policies)."""
    role = [k for k in template.find_resources("AWS::IAM::Role") if k.startswith("ApiRole")][0]
    out = []
    for kind in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        for policy in template.find_resources(kind).values():
            props = policy["Properties"]
            if {"Ref": role} in props.get("Roles", []):
                out += props["PolicyDocument"]["Statement"]
    return out


def _api_actions(template):
    """Every IAM action granted to the API role (inline and overflow policies)."""
    actions = set()
    for s in _api_statements(template):
        actions |= set(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    return actions


def test_spot_score_cache_minutes_reaches_the_api_function():
    t = synth(spotScoreCacheMinutes="45")
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "SPOT_SCORE_CACHE_MINUTES": "45"})}})
    t = synth()
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "api.handler", "Environment": {"Variables": Match.object_like({
        "SPOT_SCORE_CACHE_MINUTES": "15"})}})


def test_spot_score_budget_reaches_the_updater_and_the_api():
    t = synth(spotScoreConfigsPerDay="24")
    for handler in ("api.handler", "score_updater.handler"):
        t.has_resource_properties("AWS::Lambda::Function", {"Handler": handler, "Environment": {"Variables": Match.object_like({
            "SPOT_SCORE_CONFIGS_PER_DAY": "24"})}})
    t = synth()
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "score_updater.handler", "Environment": {
        "Variables": Match.object_like({"SPOT_SCORE_CONFIGS_PER_DAY": "40"})}})
    for f in t.find_resources("AWS::Lambda::Function").values():
        names = f["Properties"].get("Environment", {}).get("Variables", {})
        # No click reserve and no separate new-configuration number any more.
        assert not {"SPOT_SCORE_RESERVE_PER_DAY", "SPOT_SCORE_NEW_CONFIGS_PER_DAY", "SPOT_SCORE_MAX_CONFIGS", "REGIONS",
                    "INSTANCE_FAMILIES", "ENABLE_SPOT_PROBE"} & set(names)
        # The notifier never creates a configuration: it gets no budget setting.
        if f["Properties"].get("Handler") == "notifier.handler":
            assert not any(k.startswith("SPOT_SCORE_") and k != "SPOT_SCORE_FAMILIES" for k in names)


def test_the_score_updater_runs_every_fifteen_minutes_on_the_quarter_hour():
    t = synth()
    rules = [r["Properties"] for r in t.find_resources("AWS::Events::Rule").values()
             if "ScoreUpdater" in json.dumps(r["Properties"]["Targets"])]
    assert [r["ScheduleExpression"] for r in rules] == ["cron(0/15 * * * ? *)"]
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "score_updater.handler", "Timeout": 300})
    # The API may start it (the bootstrap), and only it.
    invoke = [s for s in statements(t) if "lambda:InvokeFunction" in json.dumps(s["Action"])]
    assert len(invoke) == 1 and "ScoreUpdaterFunction" in json.dumps(invoke[0]["Resource"])


def test_the_score_updater_role_is_read_only_ec2_and_the_cache_table():
    t = synth()
    role = [k for k in t.find_resources("AWS::IAM::Role") if k.startswith("ScoreUpdaterRole")][0]
    actions = set()
    for policy in t.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role} in policy["Properties"].get("Roles", []):
            for st in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions |= set(st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
    assert actions == {"logs:CreateLogStream", "logs:PutLogEvents", "dynamodb:GetItem", "dynamodb:BatchGetItem",
                       "dynamodb:PutItem", "ec2:DescribeRegions", "ec2:DescribeInstanceTypeOfferings",
                       "ec2:DescribeInstanceTypes", "ec2:GetSpotPlacementScores", "ec2:DescribeSpotPriceHistory"}


def test_api_role_action_set():
    """The complete API role action set: the heatmap, the shared caches, and the prices reuse
    GetItem/BatchGetItem/PutItem on the cache table; the Region long names add ssm:GetParameters; the probe adds DescribeInstanceTypes for its image; the
    list price adds pricing:GetProducts. No Capacity Reservation action."""
    actions = _api_actions(synth())
    assert "dynamodb:BatchWriteItem" not in actions
    assert {"ec2:GetSpotPlacementScores", "ec2:DescribeInstanceTypeOfferings", "dynamodb:GetItem", "dynamodb:PutItem"} <= actions
    assert actions == {
        "logs:CreateLogStream", "logs:PutLogEvents",
        "dynamodb:Query", "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:DeleteItem",
        "dynamodb:UpdateItem",
        "ec2:DescribeCapacityBlockOfferings", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeSpotPriceHistory",
        "ec2:GetSpotPlacementScores", "ec2:DescribeAvailabilityZones", "ec2:DescribeInstanceTypes",
        "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeRegions",
        "servicequotas:ListServiceQuotas", "servicequotas:ListAWSDefaultServiceQuotas", "cloudwatch:GetMetricData",
        "pricing:GetProducts",
        "ssm:GetParameter", "ssm:GetParameters", "ec2:RunInstances", "ec2:CreateSecurityGroup", "ec2:CreateTags", "ec2:TerminateInstances",
        "ec2:CreateFleet", "ec2:CreateLaunchTemplate", "ec2:DeleteLaunchTemplate",
        # The lookup of a lost CreateFleet answer by its per-launch tag.
        "ec2:DescribeInstances",
        "sns:Subscribe", "sns:Unsubscribe", "lambda:InvokeFunction",
    }


def test_api_function_has_1024_mb_and_the_other_functions_keep_theirs():
    """The API's init and quota path are CPU bound; Lambda gives CPU in proportion to memory."""
    t = synth(enableNotifications="true")
    sizes = {k: v["Properties"]["MemorySize"] for k, v in t.find_resources("AWS::Lambda::Function").items()
             if "MemorySize" in v["Properties"]}
    api = [v for k, v in sizes.items() if k.startswith("ApiFunction")]
    assert api == [1024]
    assert sorted(v for k, v in sizes.items() if k.startswith(("Notifier", "Cleanup"))) == [128, 256]


# ------------------------------------------------ cleanup failure alarms
def _alarm(template, name):
    alarms = [a["Properties"] for a in template.find_resources("AWS::CloudWatch::Alarm").values()
              if a["Properties"].get("AlarmName") == name]
    assert len(alarms) == 1, name
    return alarms[0]


def test_one_failed_cleanup_run_alarms_on_the_function_errors():
    t = synth()
    a = _alarm(t, "t-probe-cleanup-errors")
    fn = [k for k in t.find_resources("AWS::Lambda::Function") if k.startswith("CleanupFunction")]
    assert (a["Namespace"], a["MetricName"], a["Dimensions"]) == ("AWS/Lambda", "Errors", [{"Name": "FunctionName", "Value": {"Ref": fn[0]}}])
    # One period covers one scheduled run (every 15 minutes); one error in one period is enough.
    assert (a["Statistic"], a["Period"], a["EvaluationPeriods"], a["Threshold"]) == ("Sum", 900, 1, 1)
    assert a["ComparisonOperator"] == "GreaterThanOrEqualToThreshold" and a["TreatMissingData"] == "notBreaching"
    assert "AlarmActions" not in a


def test_a_region_the_cleanup_could_not_clean_alarms_through_a_log_metric():
    t = synth()
    filters = t.find_resources("AWS::Logs::MetricFilter")
    assert len(filters) == 1
    f = next(iter(filters.values()))["Properties"]
    logs_id = [k for k in t.find_resources("AWS::Logs::LogGroup") if k.startswith("CleanupLogs")][0]
    assert f["LogGroupName"] == {"Ref": logs_id}
    assert f["FilterPattern"] == '"probe cleanup failed in"'
    # The pattern is the exact warning the function logs for each failed Region.
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "lambda" / "cleanup.py").read_text(encoding="utf-8")
    assert 'logger.warning("probe cleanup failed in %s (%s)"' in source
    assert f["MetricTransformations"] == [{"MetricName": "ProbeCleanupRegionFailures", "MetricNamespace": "GpuCapacityFinder/t",
                                           "MetricValue": "1", "DefaultValue": 0}]
    a = _alarm(t, "t-probe-cleanup-region-failures")
    assert (a["Namespace"], a["MetricName"]) == ("GpuCapacityFinder/t", "ProbeCleanupRegionFailures")
    assert "Dimensions" not in a
    assert (a["Statistic"], a["Period"], a["EvaluationPeriods"], a["Threshold"]) == ("Sum", 900, 1, 1)
    assert a["ComparisonOperator"] == "GreaterThanOrEqualToThreshold" and a["TreatMissingData"] == "notBreaching"
    assert "AlarmActions" not in a


def test_cleanup_alarms_add_no_permissions_and_go_with_the_cleanup():
    t = synth()
    actions = json.dumps([s["Action"] for s in statements(t)])
    assert "cloudwatch:PutMetric" not in actions and "logs:PutMetricFilter" not in actions
    off = synth(enableTestLaunch="false")
    # The cleanup alarms and the metric filter go with the cleanup function; the API and notifier
    # alarms stay (3 each).
    assert sorted(alarm_names(off)) == ["t-api-duration", "t-api-errors", "t-api-throttles",
                                        "t-notifier-duration", "t-notifier-errors", "t-notifier-throttles",
                                        "t-score-updater-duration", "t-score-updater-errors", "t-score-updater-throttles"]
    off.resource_count_is("AWS::Logs::MetricFilter", 0)


# ------------------------------------------- per-function alarms
def alarm_names(template):
    return [a["Properties"]["AlarmName"] for a in template.find_resources("AWS::CloudWatch::Alarm").values()]


def test_every_function_alarms_on_errors_duration_and_throttles():
    t = synth()
    assert sorted(alarm_names(t)) == [
        "t-api-duration", "t-api-errors", "t-api-throttles",
        "t-notifier-duration", "t-notifier-errors", "t-notifier-throttles",
        "t-probe-cleanup-duration", "t-probe-cleanup-errors", "t-probe-cleanup-region-failures",
        "t-probe-cleanup-throttles",
        "t-score-updater-duration", "t-score-updater-errors", "t-score-updater-throttles",
    ]
    fns = {name: [k for k in t.find_resources("AWS::Lambda::Function") if k.startswith(prefix)][0]
           for name, prefix in (("api", "ApiFunction"), ("notifier", "NotifierFunction"),
                                ("probe-cleanup", "CleanupFunction"), ("score-updater", "ScoreUpdaterFunction"))}
    for label, fn in fns.items():
        for metric in ("Errors", "Duration", "Throttles"):
            if (label, metric) == ("probe-cleanup", "Errors"):
                continue  # the cleanup Errors alarm is the 15-minute one of the safety net
            a = _alarm(t, f"t-{label}-{metric.lower()}")
            assert (a["Namespace"], a["MetricName"]) == ("AWS/Lambda", metric)
            assert a["Dimensions"] == [{"Name": "FunctionName", "Value": {"Ref": fn}}]
            assert (a["EvaluationPeriods"], a["Period"]) == (1, 300)
            assert a["ComparisonOperator"] == "GreaterThanThreshold"
            # An idle deployment runs nothing, so a period with no data is not a problem.
            assert a["TreatMissingData"] == "notBreaching"
            assert "AlarmActions" not in a


@pytest.mark.parametrize("label,statistic,threshold", [
    ("errors", "Sum", 0), ("throttles", "Sum", 0), ("duration", "Maximum", 20000)])
def test_the_api_alarms_watch_one_error_and_a_run_near_the_timeout(label, statistic, threshold):
    """The Duration threshold is 80 % of the function's own timeout, the warning before the platform
    stops a run (a stopped run writes no request line of its own)."""
    a = _alarm(synth(), f"t-api-{label}")
    assert (a["Statistic"], a["Threshold"]) == (statistic, threshold)
    fn = next(v for k, v in synth().find_resources("AWS::Lambda::Function").items() if k.startswith("ApiFunction"))
    assert fn["Properties"]["Timeout"] == 25 and threshold <= 25_000


def test_the_other_functions_measure_duration_against_their_own_timeout():
    t = synth()
    assert _alarm(t, "t-notifier-duration")["Threshold"] == int(4 * 60 * 1000 * 0.8)
    assert _alarm(t, "t-probe-cleanup-duration")["Threshold"] == int(2 * 60 * 1000 * 0.8)


def test_without_the_alarm_topic_no_alarm_has_an_action_and_no_topic_is_imported():
    t = synth()
    assert not [a for a in t.find_resources("AWS::CloudWatch::Alarm").values() if a["Properties"].get("AlarmActions")]
    assert "AlarmTopic" not in json.dumps(t.to_json())


def test_the_alarm_topic_setting_becomes_the_action_of_every_alarm():
    """One operator topic for every alarm of the stack, probe on or off. The users' alert topic is
    never used for it: alarms would reach every subscriber, and its AWS managed key does not let
    CloudWatch publish."""
    arn = "arn:aws:sns:us-east-1:123456789012:ops-alarms"
    for ctx in ({}, {"enableTestLaunch": "false"}, {"enableNotifications": "false"}):
        t = synth(alarmTopicArn=arn, **ctx)
        alarms = [a["Properties"] for a in t.find_resources("AWS::CloudWatch::Alarm").values()]
        assert alarms and all(a["AlarmActions"] == [arn] for a in alarms), ctx
        # An existing topic is only referenced: the stack creates no topic for the alarms.
        topics = [v["Properties"].get("DisplayName") for v in t.find_resources("AWS::SNS::Topic").values()]
        assert arn not in json.dumps(topics)


def test_the_alarm_topic_needs_no_new_iam_statement():
    """The topic policy is the operator's own; the stack asks for no sns:Publish on it."""
    arn = "arn:aws:sns:us-east-1:123456789012:ops-alarms"
    assert arn not in json.dumps(statements(synth(alarmTopicArn=arn)))
    assert len(statements(synth(alarmTopicArn=arn))) == len(statements(synth()))


def test_every_function_gets_the_excluded_regions_and_the_families_that_need_them():
    t = synth(excludeRegions="me-south-1,af-south-1", spotScoreFamilies="p5.*,g6e.*", enableNotifications="true")
    for handler in ("api.handler", "notifier.handler", "score_updater.handler"):
        t.has_resource_properties("AWS::Lambda::Function", {"Handler": handler, "Environment": {"Variables": Match.object_like({
            "EXCLUDE_REGIONS": "me-south-1,af-south-1", "SPOT_SCORE_FAMILIES": "p5.*,g6e.*"})}})
    t.has_resource_properties("AWS::Lambda::Function", {"Handler": "cleanup.handler", "Environment": {"Variables": {
        "EXCLUDE_REGIONS": "me-south-1,af-south-1"}}})


def site_config(tmp_path, env):
    """The config.json the site deployment writes, with each deploy-time marker replaced by its CloudFormation value."""
    app = cdk.App(outdir=str(tmp_path))
    stack = FinderStack(app, "t", cfg=parse_context(lambda k: None), env=env)
    app.synth()
    files = list(tmp_path.glob("asset.*/config.json"))
    assert len(files) == 1
    text = files[0].read_text()
    deploy = list(Template.from_stack(stack).find_resources("Custom::CDKBucketDeployment").values())[0]
    markers = {k: v for m in deploy["Properties"]["SourceMarkers"] for k, v in m.items()}
    # Older aws-cdk-lib writes a marker bare (its value carries the JSON quotes); newer releases
    # (SourceMarkersConfig jsonEscape) write it inside a JSON string. Quote only the bare ones.
    pattern = re.compile("|".join(re.escape(k) for k in sorted(markers, key=len, reverse=True)))
    out, pos, in_string = [], 0, False
    for m in pattern.finditer(text):
        chunk = text[pos:m.start()]
        in_string ^= len(re.findall(r'(?<!\\)"', chunk)) % 2 == 1
        out += [chunk, m.group(0) if in_string else json.dumps(m.group(0))]
        pos = m.end()
    raw = json.loads("".join(out) + text[pos:])
    found = dict(markers)
    return {k: found.get(v, v) for k, v in raw.items()}, Template.from_stack(stack)


def test_site_config_names_the_user_pool_and_its_region(tmp_path):
    """The Users tab links to the pool's Users page in the Cognito console: config.json carries both values."""
    conf, t = site_config(tmp_path, cdk.Environment(account="123456789012", region="us-east-1"))
    assert set(conf) == {"apiUrl", "clientId", "cognitoDomain", "siteUrl", "userPoolId", "region"}
    pool_id = list(t.find_resources("AWS::Cognito::UserPool"))[0]
    # The quoted form is from older aws-cdk-lib releases, the bare Ref from jsonEscape markers.
    assert conf["userPoolId"] in ({"Fn::Join": ["", ['"', {"Ref": pool_id}, '"']]}, {"Ref": pool_id})
    assert conf["region"] == "us-east-1"


def test_site_config_region_is_the_stack_region_token_when_env_agnostic(tmp_path):
    conf, _ = site_config(tmp_path, None)
    assert "AWS::Region" in json.dumps(conf["region"])


def test_launch_and_cleanup_statements_name_every_region_with_their_conditions():
    """Test launches run in every enabled Region: every statement that launches, tags, creates, or
    terminates, and the probe image parameter, name any Region (ec2:*, ssm:*) with specific
    resource types; the tag and instance-type conditions stay (the other tests pin them)."""
    t = synth(excludeRegions="ap-northeast-1", enableNotifications="true")
    scoped = {"ec2:RunInstances", "ec2:TerminateInstances", "ec2:CreateSecurityGroup", "ec2:CreateTags", "ssm:GetParameter",
              "ec2:CreateFleet", "ec2:CreateLaunchTemplate", "ec2:DeleteLaunchTemplate", "ec2:DeleteSecurityGroup"}
    seen = set()
    for st in statements(t):
        acts = set(st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
        if not acts & scoped or st.get("Sid") == "ReadRegionNames":  # the stack Region only, pinned below
            continue
        seen |= acts & scoped
        resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        assert "*" not in resources
        text = json.dumps(resources)
        assert ":ec2:*:" in text or ":ssm:*:" in text
        assert "us-east-1" not in text and "ap-northeast-1" not in text
    assert seen == scoped
    cleanup = [f for f in t.find_resources("AWS::Lambda::Function").values() if f["Properties"]["Handler"] == "cleanup.handler"]
    assert cleanup[0]["Properties"]["Environment"]["Variables"] == {"EXCLUDE_REGIONS": "ap-northeast-1"}


def test_describe_regions_is_the_only_new_action_and_read_only():
    """ec2:DescribeRegions sits in read statements only: the API, notifier, score updater and cleanup."""
    t = synth(enableNotifications="true")
    holders = [st.get("Sid") for st in statements(t)
               if "ec2:DescribeRegions" in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])]
    assert sorted(holders, key=str) == ["FindProbeResources", None, "ReadAlertSignals", "ReadCapacitySignals",
                                        "ReadSpotScoreSignals"]


def test_region_names_read_is_public_parameters_in_the_stack_region_only():
    """ssm:GetParameter(s) on the AWS public Region parameters, in the stack's own Region; the only
    other SSM statement is the probe image one, unchanged."""
    t = synth(excludeRegions="ap-northeast-1")
    [names] = by_sid(t, "ReadRegionNames")
    assert names["Action"] == ["ssm:GetParameter", "ssm:GetParameters"] and "Condition" not in names
    assert names["Resource"] == {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"},
                                                   ":ssm:", {"Ref": "AWS::Region"},
                                                   "::parameter/aws/service/global-infrastructure/regions/*"]]}
    ssm = [s.get("Sid") for s in statements(t)
           if any(a.startswith("ssm:") for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]))]
    assert sorted(ssm) == ["ReadProbeImage", "ReadRegionNames"]
    assert by_sid(t, "ReadProbeImage")[0]["Action"] == "ssm:GetParameter"


def test_batch_get_item_is_only_on_the_cache_table():
    t = synth(enableNotifications="true")
    tables = t.find_resources("AWS::DynamoDB::Table")
    cache = [k for k, v in tables.items() if v["Properties"]["KeySchema"] == [{"AttributeName": "cacheKey", "KeyType": "HASH"}]][0]
    holders = [s for s in statements(t) if "dynamodb:BatchGetItem" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
    assert len(holders) == 3  # the API, notifier and score updater grants
    for s in holders:
        resources = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
        assert all(cache in json.dumps(r) for r in resources) and "*" not in json.dumps(resources)



# ------------------------------------------------- test launch (CreateFleet instant)
def test_test_launch_fleet_and_template_statements_carry_the_probe_tag_conditions():
    t = synth()
    tag = {"aws:RequestTag/gpu-capacity-finder-probe": "true"}
    fleet = by_sid(t, "CreateTaggedProbeFleet")[0]
    assert fleet["Action"] == "ec2:CreateFleet" and fleet["Condition"] == {"StringEquals": tag}
    assert _arn_suffixes(fleet["Resource"]) == {"fleet/*", "instance/*"}
    create = by_sid(t, "CreateProbeLaunchTemplate")[0]
    assert create["Action"] == "ec2:CreateLaunchTemplate" and create["Condition"] == {"StringEquals": tag}
    assert _arn_suffixes([create["Resource"]] if isinstance(create["Resource"], dict) else create["Resource"]) == {
        "launch-template/*"}
    use = by_sid(t, "UseProbeLaunchTemplate")[0]
    assert sorted(use["Action"]) == ["ec2:CreateFleet", "ec2:DeleteLaunchTemplate", "ec2:RunInstances"]
    assert use["Condition"] == {"StringEquals": {"aws:ResourceTag/gpu-capacity-finder-probe": "true"}}
    support = by_sid(t, "LaunchSupportingResources")[0]
    assert sorted(support["Action"]) == ["ec2:CreateFleet", "ec2:RunInstances"] and "Condition" not in support
    assert _arn_suffixes(support["Resource"]) == {"subnet/*", "network-interface/*", "volume/*",
                                                  "spot-instances-request/*", "snapshot/*"}
    # The image only when amazon owns it (the AL2023 public parameter's AMI); the security group only
    # when it is the probe group.
    image = by_sid(t, "LaunchAmazonImageOnly")[0]
    assert sorted(image["Action"]) == ["ec2:CreateFleet", "ec2:RunInstances"]
    assert image["Condition"] == {"StringEquals": {"ec2:Owner": "amazon"}}
    assert _arn_suffixes(image["Resource"] if isinstance(image["Resource"], list) else [image["Resource"]]) == {"image/*"}
    sg = by_sid(t, "LaunchInProbeSecurityGroupOnly")[0]
    assert sorted(sg["Action"]) == ["ec2:CreateFleet", "ec2:RunInstances"]
    assert sg["Condition"] == {"StringEquals": {"aws:ResourceTag/gpu-capacity-finder-probe-sg": "true"}}
    assert _arn_suffixes(sg["Resource"] if isinstance(sg["Resource"], list) else [sg["Resource"]]) == {"security-group/*"}
    # No other statement lets a launch use any image or any security group.
    for st in statements(t):
        acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        if {"ec2:RunInstances", "ec2:CreateFleet"} & set(acts) and st.get("Sid") not in (
                "LaunchAmazonImageOnly", "LaunchInProbeSecurityGroupOnly"):
            assert "image/" not in json.dumps(st["Resource"]) and "security-group/" not in json.dumps(st["Resource"]), st
    find = by_sid(t, "FindUnknownOutcomeProbe")[0]
    assert find["Action"] == "ec2:DescribeInstances" and find["Resource"] == "*"
    tags = by_sid(t, "TagOnLaunchOnly")[0]
    assert tags["Condition"] == {"StringEquals": {
        "ec2:CreateAction": ["RunInstances", "CreateFleet", "CreateLaunchTemplate"], **tag}}
    assert _arn_suffixes(tags["Resource"]) == {"instance/*", "volume/*", "network-interface/*", "fleet/*",
                                               "launch-template/*"}
    # The instance-type condition stays on RunInstances of the instance; no DeleteFleets anywhere.
    assert by_sid(t, "LaunchTaggedProbeInstance")[0]["Condition"]["StringLike"] == {"ec2:InstanceType": ["g*", "p*"]}
    assert not [s for s in statements(t) if "ec2:DeleteFleets" in str(s["Action"])]
    # Every launch template action outside these statements is scoped by the tag too.
    for st in statements(t):
        if "LaunchTemplate" in str(st["Action"]) and "Describe" not in str(st["Action"]):
            assert "aws:ResourceTag/gpu-capacity-finder-probe" in json.dumps(st.get("Condition")) or \
                "aws:RequestTag/gpu-capacity-finder-probe" in json.dumps(st.get("Condition")), st


def test_the_cleanup_deletes_only_probe_tagged_launch_templates():
    t = synth()
    role = [k for k in t.find_resources("AWS::IAM::Role") if k.startswith("CleanupRole")][0]
    sts = [st for p in t.find_resources("AWS::IAM::Policy").values() if {"Ref": role} in p["Properties"]["Roles"]
           for st in p["Properties"]["PolicyDocument"]["Statement"]]
    delete = [st for st in sts if st["Action"] == "ec2:DeleteLaunchTemplate"][0]
    assert delete["Condition"] == {"StringEquals": {"aws:ResourceTag/gpu-capacity-finder-probe": "true"}}
    assert _arn_suffixes([delete["Resource"]] if isinstance(delete["Resource"], dict) else delete["Resource"]) == {
        "launch-template/*"}
    assert any("ec2:DescribeLaunchTemplates" in st["Action"] for st in sts if isinstance(st["Action"], list))


def test_the_stack_delete_custom_resource_is_bounded_and_tag_scoped():
    t = synth(excludeRegions="me-south-1")
    crs = t.find_resources("Custom::ProbeTeardown")
    assert len(crs) == 1
    (cr_id, cr), = crs.items()
    fn_id = [k for k in t.find_resources("AWS::Lambda::Function") if k.startswith("ProbeTeardownFunction")][0]
    assert cr["Properties"]["ServiceToken"] == {"Fn::GetAtt": [fn_id, "Arn"]}
    # CloudFormation stops waiting after the service timeout, longer than the function's own timeout.
    assert int(cr["Properties"]["ServiceTimeout"]) == 600
    fn = t.find_resources("AWS::Lambda::Function")[fn_id]["Properties"]
    assert fn["Handler"] == "teardown.handler" and fn["Timeout"] == 300
    # The wait for a test launch already running: the API function's timeout plus a margin.
    api_fn = [v for k, v in t.find_resources("AWS::Lambda::Function").items() if k.startswith("ApiFunction")][0]
    assert fn["Environment"]["Variables"] == {
        "EXCLUDE_REGIONS": "me-south-1", "SETTLE_SECONDS": str(api_fn["Properties"]["Timeout"] + 5)}
    assert int(fn["Environment"]["Variables"]["SETTLE_SECONDS"]) < fn["Timeout"] - 30 - 60
    # The functions that create probe resources are deleted before the teardown runs; their roles
    # and policies after it, so a test launch still running during the wait keeps its permissions.
    for prefix in ("ApiFunction", "CleanupFunction"):
        res = [v for k, v in t.find_resources("AWS::Lambda::Function").items() if k.startswith(prefix)][0]
        assert cr_id in res.get("DependsOn", [])
    for prefix in ("ApiRole", "CleanupRole"):
        names = [k for k in {**t.find_resources("AWS::IAM::Role"), **t.find_resources("AWS::IAM::Policy")}
                 if k.startswith(prefix)]
        assert names and all(n in cr.get("DependsOn", []) for n in names), (prefix, names)
    t_sts = {st.get("Sid"): st for st in statements(t) if st.get("Sid") in (
        "FindProbeResources", "DeleteProbeLaunchTemplatesOnly", "DeleteProbeSecurityGroupsOnly")}
    assert t_sts["DeleteProbeLaunchTemplatesOnly"]["Condition"] == {
        "StringEquals": {"aws:ResourceTag/gpu-capacity-finder-probe": "true"}}
    assert t_sts["DeleteProbeSecurityGroupsOnly"]["Condition"] == {
        "StringEquals": {"aws:ResourceTag/gpu-capacity-finder-probe-sg": "true"}}
    assert t_sts["FindProbeResources"]["Resource"] == "*" and all(
        a.startswith("ec2:Describe") for a in t_sts["FindProbeResources"]["Action"])
    # The teardown reads only its own stack's status (Delete acts only during a stack delete).
    read = [st for st in statements(t) if st.get("Sid") == "ReadOwnStackStatus"]
    assert len(read) == 1 and read[0]["Action"] == "cloudformation:DescribeStacks"
    assert read[0]["Resource"] == {"Ref": "AWS::StackId"}
    # Only the teardown may delete a security group.
    assert [st.get("Sid") for st in statements(t) if "ec2:DeleteSecurityGroup" in str(st["Action"])] == [
        "DeleteProbeSecurityGroupsOnly"]
    assert not synth(enableTestLaunch="false").find_resources("Custom::ProbeTeardown")


def test_the_deployment_wide_cap_reaches_the_api_function():
    synth(maxTestLaunchesPerDay="0").has_resource_properties("AWS::Lambda::Function", {
        "Handler": "api.handler", "Environment": {"Variables": Match.object_like({"MAX_TEST_LAUNCHES_PER_DAY": "0"})}})


def test_a_failed_notifier_run_is_not_retried_asynchronously():
    """The next scheduled run is the retry; an asynchronous retry could overlap it and email twice."""
    t = synth()
    fn_id = [k for k in t.find_resources("AWS::Lambda::Function") if k.startswith("NotifierFunction")][0]
    configs = [c["Properties"] for c in t.find_resources("AWS::Lambda::EventInvokeConfig").values()
               if c["Properties"]["FunctionName"] == {"Ref": fn_id}]
    assert len(configs) == 1 and configs[0]["MaximumRetryAttempts"] == 0


def test_a_failed_score_updater_run_is_not_retried_asynchronously():
    """The next scheduled run is the retry; a retry would repeat the Region-wide describe calls."""
    t = synth()
    fn_id = [k for k in t.find_resources("AWS::Lambda::Function") if k.startswith("ScoreUpdaterFunction")][0]
    configs = [c["Properties"] for c in t.find_resources("AWS::Lambda::EventInvokeConfig").values()
               if c["Properties"]["FunctionName"] == {"Ref": fn_id}]
    assert len(configs) == 1 and configs[0]["MaximumRetryAttempts"] == 0
