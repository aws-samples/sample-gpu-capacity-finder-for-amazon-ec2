import jsii
from aws_cdk import (
    Aspects,
    Aws,
    CfnOutput,
    CustomResource,
    Duration,
    Fn,
    IAspect,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_sns as sns,
)
from cdk_nag import NagSuppressions
from constructs import Construct, IConstruct

PROBE_TAG = "gpu-capacity-finder-probe"
PROBE_SG_TAG = "gpu-capacity-finder-probe-sg"
# Partition-key prefix of the per-user daily probe counter items (PROBE_COUNT_PREFIX in lambda/api.py).
PROBE_COUNT_PREFIX = "#probe-count#"
# Partition-key prefix of the deployment-wide daily counter items (ALL_COUNT_PREFIX in lambda/api.py).
ALL_COUNT_PREFIX = "#probes#"
# Lifetime of the ID and access tokens; the page renews them with the refresh token (sessionHours).
TOKEN_MINUTES = 60
# The HTTP API burst limit is this multiple of apiRateLimit.
API_BURST_FACTOR = 3
# The probe image per architecture (AMI_PARAMETERS in lambda/api.py).
AMI_PARAMETERS = (
    "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
)
# The Region long names (REGION_NAME_PARAMETER in lambda/api.py: .../regions/<code>/longName).
REGION_NAME_PARAMETERS = "/aws/service/global-infrastructure/regions/*"


@jsii.implements(IAspect)
class _SuppressOnRoleAfterSplit:
    """CDK moves statements past the inline policy size limit into OverflowPolicy managed policies
    from an aspect at synthesis, after the constructor ran; this applies the role's suppressions to
    them too, so they are not left to a stack-level suppression."""

    def __init__(self, role, suppressions):
        self._role = role
        self._suppressions = suppressions

    def visit(self, node: IConstruct) -> None:
        if node is self._role:
            NagSuppressions.add_resource_suppressions(node, self._suppressions, apply_to_children=True)


class FinderStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: dict, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ----------------------------------------------------- alarm action
        # Every alarm of this stack notifies the operator's own SNS topic when alarmTopicArn is set.
        # Without the setting the alarms exist with no action and show only in the CloudWatch
        # console. The alert topic of the email alerts is never used: it belongs to the users, and
        # it is encrypted with the AWS managed key, which CloudWatch cannot publish through.
        self._alarm_actions = []
        if cfg.get("alarm_topic_arn"):
            self._alarm_actions = [cloudwatch_actions.SnsAction(
                sns.Topic.from_topic_arn(self, "AlarmTopic", cfg["alarm_topic_arn"]))]

        # ------------------------------------------------------------ data
        table = dynamodb.Table(
            self,
            "Probes",
            partition_key=dynamodb.Attribute(name="region", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Shared cache: offering items (offerings#<Region>: types, zones, and the accelerator family
        # names, read by the matrix, the instance-type lists, validation, and the quota tips), the
        # Capacity Block types per Region (cbtypes#<Region>, asked again once a day), the Spot score
        # groups (#sps-groups, written by the score updater only), Spot placement scores per request
        # configuration (sps#<group>#<Regions hash>) and their history (spshist#<group>#regions),
        # the configuration registry and governor (spscfg#<id>, #sps-budget), quota rows
        # (quota#<Region>), prices (price-od#<type>#<Region>, price-spot#<Region>), the enabled
        # Region list (regions#enabled, asked again once per day), and the lock items (#lock#<item>).
        offering_cache = dynamodb.Table(
            self,
            "OfferingCache",
            partition_key=dynamodb.Attribute(name="cacheKey", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        subscriptions = topic = None
        if cfg["enable_notifications"]:
            # Alert items (sk = region#type#signal#value) and one "#profile" item per user.
            subscriptions = dynamodb.Table(
                self,
                "Subscriptions",
                partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                ),
                removal_policy=RemovalPolicy.DESTROY,
            )
            topic = sns.Topic(
                self,
                "Alerts",
                display_name="GPU Capacity Finder",
                # AWS managed key: no key to create or pay for; the publisher needs no KMS grant.
                master_key=kms.Alias.from_alias_name(self, "SnsKey", "alias/aws/sns"),
                enforce_ssl=True,
            )

        # ------------------------------------------------------------- web
        bucket = s3.Bucket(
            self,
            "Site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "Headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    override=True,
                    content_security_policy=(
                        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                        f"connect-src 'self' https://*.execute-api.{Aws.REGION}.amazonaws.com "
                        f"https://*.auth.{Aws.REGION}.amazoncognito.com; "
                        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                    ),
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    override=True, access_control_max_age=Duration.days(365), include_subdomains=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    override=True, frame_option=cloudfront.HeadersFrameOption.DENY
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    override=True, referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER
                ),
            ),
        )

        dist = cloudfront.Distribution(
            self,
            "Dist",
            comment="GPU Capacity Finder web page",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
            ),
        )
        site_url = f"https://{dist.distribution_domain_name}/"

        # ------------------------------------------------------------ auth
        pool = cognito.UserPool(
            self,
            "Users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(email=cognito.StandardAttribute(required=True)),
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_lowercase=True, require_uppercase=True, require_digits=True, require_symbols=True
            ),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        client = pool.add_client(
            "WebClient",
            generate_secret=False,
            prevent_user_existence_errors=True,
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            # Sign-in runs only through the hosted page (authorization code with PKCE); the client
            # allows no direct SRP, password, or custom auth flow, so no token carries the
            # aws.cognito.signin.user.admin scope. Only ALLOW_REFRESH_TOKEN_AUTH remains.
            auth_flows=cognito.AuthFlow(
                admin_user_password=False, custom=False, user=False, user_password=False, user_srp=False
            ),
            # The API needs the email and its verified flag from the ID token; users change nothing.
            read_attributes=cognito.ClientAttributes().with_standard_attributes(email=True, email_verified=True),
            write_attributes=cognito.ClientAttributes(),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[site_url],
                logout_urls=[site_url],
            ),
            id_token_validity=Duration.minutes(TOKEN_MINUTES),
            access_token_validity=Duration.minutes(TOKEN_MINUTES),
            # The page renews the ID token silently with the refresh token until this ends.
            refresh_token_validity=Duration.hours(cfg["session_hours"]),
            enable_token_revocation=True,
        )
        stack_guid = Fn.select(0, Fn.split("-", Fn.select(2, Fn.split("/", Aws.STACK_ID))))
        domain = pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=Fn.join("-", [cfg["stack_name"].lower(), stack_guid])
            ),
        )

        # ---------------------------------------------- Spot score updater
        # The only function that changes the Spot score groups or grants new Spot placement score
        # request configurations: the daily update (first run at or after 00:30 UTC) and the warm
        # run every 15 minutes (minutes 0, 15, 30 and 45). The API starts it once, asynchronously,
        # when the groups must be built (first deploy or a new spotScoreFamilies).
        updater_logs = logs.LogGroup(
            self, "ScoreUpdaterLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY
        )
        updater_role = iam.Role(self, "ScoreUpdaterRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        updater_logs.grant_write(updater_role)
        offering_cache.grant(updater_role, "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem")
        updater_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadSpotScoreSignals",
                actions=[
                    "ec2:DescribeRegions",
                    "ec2:DescribeInstanceTypeOfferings",
                    "ec2:DescribeInstanceTypes",
                    "ec2:GetSpotPlacementScores",
                    "ec2:DescribeSpotPriceHistory",
                ],
                resources=["*"],
            )
        )
        updater_timeout = Duration.minutes(5)
        updater_fn = lambda_.Function(
            self,
            "ScoreUpdaterFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="score_updater.handler",
            code=lambda_.Code.from_asset("lambda", exclude=["__pycache__", "*.pyc"]),
            role=updater_role,
            # Far shorter than the 15-minute schedule; a lock item keeps runs from overlapping.
            timeout=updater_timeout,
            # No asynchronous retry of a failed run (scheduled or API-started): the next scheduled
            # run is the retry, so a failed run does not repeat the Region-wide describe calls.
            retry_attempts=0,
            memory_size=256,
            log_group=updater_logs,
            environment={
                "EXCLUDE_REGIONS": ",".join(cfg["exclude_regions"]),
                "SPOT_SCORE_FAMILIES": ",".join(cfg["spot_score_families"]),
                "SPOT_SCORE_CONFIGS_PER_DAY": str(cfg["spot_score_configs_per_day"]),
                "SPOT_SCORE_CACHE_MINUTES": str(cfg["spot_score_cache_minutes"]),
                "OFFERING_CACHE_TABLE_NAME": offering_cache.table_name,
            },
        )
        events.Rule(
            self,
            "ScoreUpdaterSchedule",
            schedule=events.Schedule.cron(minute="0/15"),
            targets=[targets.LambdaFunction(updater_fn)],
        )
        self._function_alarms("ScoreUpdaterFunction", updater_fn, "score-updater", updater_timeout)

        # ------------------------------------------------------------- API
        env = {
            "EXCLUDE_REGIONS": ",".join(cfg["exclude_regions"]),
            "SPOT_SCORE_FAMILIES": ",".join(cfg["spot_score_families"]),
            "SCORE_UPDATER_FUNCTION": updater_fn.function_name,
            "TABLE_NAME": table.table_name,
            "ENABLE_PROBE": "true" if cfg["enable_probe"] else "false",
            "TEST_LAUNCH_ALLOW_SPOT": "true" if cfg["enable_spot_probe"] else "false",
            "COOLDOWN_MINUTES": str(cfg["cooldown_minutes"]),
            "ENABLE_NOTIFICATIONS": "true" if cfg["enable_notifications"] else "false",
            "NOTIFY_INTERVAL_MINUTES": str(cfg["notify_interval_minutes"]),
            "MAX_SUBSCRIPTIONS_PER_USER": str(cfg["max_subscriptions_per_user"]),
            "OFFERING_CACHE_TABLE_NAME": offering_cache.table_name,
            "OFFERING_CACHE_HOURS": str(cfg["offering_cache_hours"]),
            "SPOT_SCORE_CACHE_MINUTES": str(cfg["spot_score_cache_minutes"]),
            "SPOT_SCORE_CONFIGS_PER_DAY": str(cfg["spot_score_configs_per_day"]),
            "QUOTA_CACHE_MINUTES": str(cfg["quota_cache_minutes"]),
            "API_RATE_LIMIT": str(cfg["api_rate_limit"]),
            "MAX_PROBES_PER_USER_PER_DAY": str(cfg["max_probes_per_user_per_day"]),
            "MAX_TEST_LAUNCHES_PER_DAY": str(cfg["max_test_launches_per_day"]),
        }
        if subscriptions:
            env["SUBSCRIPTIONS_TABLE_NAME"] = subscriptions.table_name
            env["TOPIC_ARN"] = topic.topic_arn

        api_logs = logs.LogGroup(
            self, "ApiLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY
        )
        api_role = iam.Role(self, "ApiRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        api_logs.grant_write(api_role)
        table.grant(api_role, "dynamodb:Query")
        # Probe records and cooldown locks (partition: the Region code). PutItem never reaches the
        # daily counter partitions, so only the CountDailyProbes UpdateItem below can change a count.
        api_role.add_to_policy(
            iam.PolicyStatement(
                sid="StoreProbeRecords",
                actions=["dynamodb:PutItem"],
                resources=[table.table_arn],
                conditions={"ForAllValues:StringNotLike": {"dynamodb:LeadingKeys": [
                    f"{PROBE_COUNT_PREFIX}*", f"{ALL_COUNT_PREFIX}*"]}},
            )
        )
        # BatchGetItem reads several cache items (offerings, Spot scores, refresh locks) in one request.
        offering_cache.grant(api_role, "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem")

        # Test launches run in every enabled Region (minus excludeRegions, which the function
        # enforces), so the EC2 ARNs name every Region; the tag and instance-type conditions stay.
        def arns(resource, account=True):
            acct = Aws.ACCOUNT_ID if account else ""
            return [f"arn:{Aws.PARTITION}:ec2:*:{acct}:{resource}"]

        instance_arns = arns("instance/*")
        api_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCapacitySignals",
                actions=[
                    "ec2:DescribeCapacityBlockOfferings",
                    "ec2:DescribeInstanceTypeOfferings",
                    "ec2:DescribeSpotPriceHistory",
                    # Refresh repeats configurations AWS accepted in the last day (never new ones).
                    "ec2:GetSpotPlacementScores",
                    "ec2:DescribeAvailabilityZones",
                    "ec2:DescribeInstanceTypes",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    # The Regions the account has enabled, which the read routes cover.
                    "ec2:DescribeRegions",
                    "servicequotas:ListServiceQuotas",
                    "servicequotas:ListAWSDefaultServiceQuotas",
                    # On-Demand list prices (the Price List Query API, called in us-east-1).
                    "pricing:GetProducts",
                ],
                resources=["*"],
            )
        )
        # Quota usage of any enabled Region.
        api_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadQuotaUsage",
                actions=["cloudwatch:GetMetricData"],
                resources=["*"],
            )
        )
        api_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadProbeImage",
                actions=["ssm:GetParameter"],
                resources=[f"arn:{Aws.PARTITION}:ssm:*::parameter{p}" for p in AMI_PARAMETERS],
            )
        )
        # Starts the score updater once while the Spot score groups are being built.
        updater_fn.grant_invoke(api_role)
        # The Region long names (AWS public parameters), read in the stack's own Region only.
        api_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadRegionNames",
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[f"arn:{Aws.PARTITION}:ssm:{Aws.REGION}::parameter{REGION_NAME_PARAMETERS}"],
            )
        )
        if cfg["enable_probe"] and (cfg["max_probes_per_user_per_day"] > 0 or cfg["max_test_launches_per_day"] > 0):
            # The daily test launch counters (per user, and all users together): UpdateItem only on
            # the counter partitions (the counts are read with the table's Query grant).
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="CountDailyProbes",
                    actions=["dynamodb:UpdateItem"],
                    resources=[table.table_arn],
                    conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [
                        f"{PROBE_COUNT_PREFIX}*", f"{ALL_COUNT_PREFIX}*"]}},
                )
            )
        if cfg["enable_probe"]:
            # A test launch is one EC2 Fleet of type instant (CreateFleet) from a launch template made
            # for the test. EC2 also authorizes the instance launch as RunInstances for the caller, so
            # the instance-type condition sits on RunInstances, where ec2:InstanceType is defined.
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="LaunchTaggedProbeInstance",
                    actions=["ec2:RunInstances"],
                    resources=instance_arns,
                    conditions={
                        "StringEquals": {f"aws:RequestTag/{PROBE_TAG}": "true"},
                        # Wider than the naming rule: an IAM StringLike condition cannot express "a
                        # digit follows g or p" (g* also matches gr6), so the API's check_family
                        # refuses a type outside the rule before it launches.
                        "StringLike": {"ec2:InstanceType": cfg["probe_type_patterns"]},
                    },
                )
            )
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="CreateTaggedProbeFleet",
                    actions=["ec2:CreateFleet"],
                    resources=[*arns("fleet/*"), *instance_arns],
                    conditions={"StringEquals": {f"aws:RequestTag/{PROBE_TAG}": "true"}},
                )
            )
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="LaunchSupportingResources",
                    actions=["ec2:RunInstances", "ec2:CreateFleet"],
                    resources=[
                        *arns("subnet/*"),
                        *arns("network-interface/*"),
                        *arns("volume/*"),
                        *arns("spot-instances-request/*"),
                        *arns("snapshot/*", account=False),
                    ],
                )
            )
            # The probe image is an Amazon Linux 2023 AMI from an AWS public SSM parameter
            # (AMI_PARAMETERS): its owner is "amazon", so no other image can be launched.
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="LaunchAmazonImageOnly",
                    actions=["ec2:RunInstances", "ec2:CreateFleet"],
                    resources=arns("image/*", account=False),
                    conditions={"StringEquals": {"ec2:Owner": "amazon"}},
                )
            )
            # Only the probe security group (no inbound rules) can be attached to a probe.
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="LaunchInProbeSecurityGroupOnly",
                    actions=["ec2:RunInstances", "ec2:CreateFleet"],
                    resources=arns("security-group/*"),
                    conditions={"StringEquals": {f"aws:ResourceTag/{PROBE_SG_TAG}": "true"}},
                )
            )
            # After a lost CreateFleet answer the function looks the instance up by its per-launch
            # tag (DescribeInstances has no resource-level permissions).
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="FindUnknownOutcomeProbe",
                    actions=["ec2:DescribeInstances"],
                    resources=["*"],
                )
            )
            # The launch template of one test: created with the probe tag, used only when it carries
            # the tag, and deleted right after the fleet answer.
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="UseProbeLaunchTemplate",
                    actions=["ec2:RunInstances", "ec2:CreateFleet", "ec2:DeleteLaunchTemplate"],
                    resources=arns("launch-template/*"),
                    conditions={"StringEquals": {f"aws:ResourceTag/{PROBE_TAG}": "true"}},
                )
            )
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="CreateProbeLaunchTemplate",
                    actions=["ec2:CreateLaunchTemplate"],
                    resources=arns("launch-template/*"),
                    conditions={"StringEquals": {f"aws:RequestTag/{PROBE_TAG}": "true"}},
                )
            )
            # A dedicated security group with no inbound rules, created once per VPC on the first probe.
            # The request-tag condition applies to the new group only; the VPC is authorized in its own
            # statement, because the vpc resource is not where EC2 evaluates the request tags.
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="CreateProbeSecurityGroup",
                    actions=["ec2:CreateSecurityGroup"],
                    resources=arns("security-group/*"),
                    conditions={"StringEquals": {f"aws:RequestTag/{PROBE_SG_TAG}": "true"}},
                )
            )
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="CreateProbeSecurityGroupInVpc",
                    actions=["ec2:CreateSecurityGroup"],
                    resources=arns("vpc/*"),
                )
            )
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="TagProbeSecurityGroupOnCreate",
                    actions=["ec2:CreateTags"],
                    resources=arns("security-group/*"),
                    conditions={"StringEquals": {"ec2:CreateAction": "CreateSecurityGroup"}},
                )
            )
            # Tags only at creation, and only with the probe tag in the request: the instance, its
            # volume and network interface, the fleet, and the launch template. The instance also
            # gets the per-launch tag (its CreateFleet ClientToken) in the same request.
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="TagOnLaunchOnly",
                    actions=["ec2:CreateTags"],
                    resources=[*instance_arns, *arns("volume/*"), *arns("network-interface/*"), *arns("fleet/*"),
                               *arns("launch-template/*")],
                    conditions={"StringEquals": {
                        "ec2:CreateAction": ["RunInstances", "CreateFleet", "CreateLaunchTemplate"],
                        f"aws:RequestTag/{PROBE_TAG}": "true"}},
                )
            )
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="TerminateProbeInstancesOnly",
                    actions=["ec2:TerminateInstances"],
                    resources=instance_arns,
                    conditions={"StringEquals": {f"aws:ResourceTag/{PROBE_TAG}": "true"}},
                )
            )

        if subscriptions:
            subscriptions.grant(api_role, "dynamodb:Query", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem")
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="SubscribeEmailToAlertTopic",
                    actions=["sns:Subscribe"],
                    resources=[topic.topic_arn],
                    conditions={"StringEquals": {"sns:Protocol": "email"}},
                )
            )
            api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="UnsubscribeFromAlertTopic",
                    actions=["sns:Unsubscribe"],
                    resources=[topic.topic_arn, f"{topic.topic_arn}:*"],
                )
            )

        api_timeout = Duration.seconds(25)
        api_fn = lambda_.Function(
            self,
            "ApiFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="api.handler",
            code=lambda_.Code.from_asset("lambda", exclude=["__pycache__", "*.pyc"]),
            role=api_role,
            timeout=api_timeout,
            memory_size=1024,
            log_group=api_logs,
            environment=env,
        )
        self._function_alarms("ApiFunction", api_fn, "api", api_timeout)

        http_api = apigw.HttpApi(
            self,
            "Api",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[f"https://{dist.distribution_domain_name}"],
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.DELETE,
                ],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
            default_authorizer=authorizers.HttpJwtAuthorizer(
                "Jwt",
                f"https://cognito-idp.{Aws.REGION}.amazonaws.com/{pool.user_pool_id}",
                jwt_audience=[client.user_pool_client_id],
            ),
        )
        http_api.add_routes(
            path="/api/{proxy+}",
            methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST, apigw.HttpMethod.DELETE],
            integration=integrations.HttpLambdaIntegration("ApiIntegration", api_fn),
        )
        stage = http_api.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            # The defaults cover the page's parallel calls on one Region change; raise apiRateLimit for
            # more concurrent users. The burst follows the rate (API_BURST_FACTOR).
            throttling_burst_limit=API_BURST_FACTOR * cfg["api_rate_limit"], throttling_rate_limit=cfg["api_rate_limit"]
        )

        # ------------------------------------------------ probe safety net
        cleanup_role = cleanup_fn = teardown_role = teardown_fn = None
        if cfg["enable_probe"]:
            cleanup_logs = logs.LogGroup(
                self, "CleanupLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY
            )
            cleanup_role = iam.Role(self, "CleanupRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
            cleanup_logs.grant_write(cleanup_role)
            cleanup_role.add_to_policy(
                iam.PolicyStatement(actions=["ec2:DescribeInstances", "ec2:DescribeRegions", "ec2:DescribeLaunchTemplates"],
                                    resources=["*"])
            )
            cleanup_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["ec2:TerminateInstances"],
                    resources=instance_arns,
                    conditions={"StringEquals": {f"aws:ResourceTag/{PROBE_TAG}": "true"}},
                )
            )
            # Probe launch templates that a failed delete left behind (older than an hour).
            cleanup_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["ec2:DeleteLaunchTemplate"],
                    resources=arns("launch-template/*"),
                    conditions={"StringEquals": {f"aws:ResourceTag/{PROBE_TAG}": "true"}},
                )
            )
            cleanup_timeout = Duration.minutes(2)
            cleanup_fn = lambda_.Function(
                self,
                "CleanupFunction",
                runtime=lambda_.Runtime.PYTHON_3_13,
                architecture=lambda_.Architecture.ARM_64,
                handler="cleanup.handler",
                code=lambda_.Code.from_asset("lambda", exclude=["__pycache__", "*.pyc"]),
                role=cleanup_role,
                timeout=cleanup_timeout,
                memory_size=128,
                log_group=cleanup_logs,
                environment={"EXCLUDE_REGIONS": env["EXCLUDE_REGIONS"]},
            )
            events.Rule(
                self,
                "CleanupSchedule",
                schedule=events.Schedule.rate(Duration.minutes(15)),
                targets=[targets.LambdaFunction(cleanup_fn)],
            )
            self._cleanup_alarms(cleanup_fn, cleanup_logs)
            # The Errors alarm of this function is the cleanup one above (one scheduled run per period).
            self._function_alarms("CleanupFunction", cleanup_fn, "probe-cleanup", cleanup_timeout, errors=False)

            teardown_role, teardown_fn = self._probe_teardown(env["EXCLUDE_REGIONS"], arns, instance_arns,
                                                              [api_fn, cleanup_fn],
                                                              int(api_timeout.to_seconds()) + self.TEARDOWN_SETTLE_MARGIN)

        # --------------------------------------------------- email alerts
        notifier_role = notifier_fn = None
        if subscriptions:
            notifier_logs = logs.LogGroup(
                self, "NotifierLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY
            )
            notifier_role = iam.Role(self, "NotifierRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
            notifier_logs.grant_write(notifier_role)
            subscriptions.grant(notifier_role, "dynamodb:Scan", "dynamodb:UpdateItem")
            # Spot score alerts read the score updater's cache items (no Spot placement score call);
            # PutItem keeps the shared enabled-Region list and its lock.
            offering_cache.grant(notifier_role, "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem")
            notifier_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadAlertSignals",
                    # Alerts may target any enabled Region; DescribeRegions reads that list.
                    actions=["ec2:DescribeCapacityBlockOfferings", "ec2:DescribeRegions"],
                    resources=["*"],
                )
            )
            notifier_role.add_to_policy(
                iam.PolicyStatement(sid="PublishAlerts", actions=["sns:Publish"], resources=[topic.topic_arn])
            )
            notifier_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadAlertSubscriptionState",
                    actions=["sns:GetSubscriptionAttributes"],
                    resources=[topic.topic_arn, f"{topic.topic_arn}:*"],
                )
            )
            notifier_timeout = Duration.minutes(4)
            notifier_fn = lambda_.Function(
                self,
                "NotifierFunction",
                runtime=lambda_.Runtime.PYTHON_3_13,
                architecture=lambda_.Architecture.ARM_64,
                handler="notifier.handler",
                code=lambda_.Code.from_asset("lambda", exclude=["__pycache__", "*.pyc"]),
                role=notifier_role,
                # Shorter than the shortest schedule interval, so runs do not overlap.
                timeout=notifier_timeout,
                # No asynchronous retry of a failed or timed-out run: the next scheduled run is the
                # retry, and a retry could run at the same time as it and email the same change twice.
                retry_attempts=0,
                memory_size=256,
                log_group=notifier_logs,
                environment={
                    "EXCLUDE_REGIONS": env["EXCLUDE_REGIONS"],
                    "SPOT_SCORE_FAMILIES": env["SPOT_SCORE_FAMILIES"],
                    "SUBSCRIPTIONS_TABLE_NAME": subscriptions.table_name,
                    "TOPIC_ARN": topic.topic_arn,
                    "SITE_URL": site_url,
                    "OFFERING_CACHE_TABLE_NAME": offering_cache.table_name,
                },
            )
            events.Rule(
                self,
                "NotifierSchedule",
                schedule=events.Schedule.rate(Duration.minutes(cfg["notify_interval_minutes"])),
                targets=[targets.LambdaFunction(notifier_fn)],
            )
            self._function_alarms("NotifierFunction", notifier_fn, "notifier", notifier_timeout)

        # ------------------------------------------------- site contents
        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            destination_bucket=bucket,
            distribution=dist,
            distribution_paths=["/*"],
            sources=[
                s3deploy.Source.asset("web"),
                s3deploy.Source.json_data(
                    "config.json",
                    {
                        "apiUrl": http_api.api_endpoint,
                        "clientId": client.user_pool_client_id,
                        "cognitoDomain": domain.base_url(),
                        "siteUrl": site_url,
                        # For the Users tab's link to the user pool in the Amazon Cognito console.
                        "userPoolId": pool.user_pool_id,
                        "region": self.region,
                    },
                ),
            ],
        )

        CfnOutput(self, "SiteUrl", value=site_url)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)

        self._suppress(bucket, pool, dist, api_role, cleanup_role, notifier_role, updater_role, teardown_role,
                       [f for f in (api_fn, cleanup_fn, notifier_fn, updater_fn, teardown_fn) if f])

    # The teardown function's timeout; it stops its own work RESERVE_SECONDS (lambda/teardown.py) before.
    TEARDOWN_TIMEOUT = Duration.minutes(5)
    # CloudFormation stops waiting for the custom resource after this, even if the function crashed.
    TEARDOWN_SERVICE_TIMEOUT = Duration.minutes(10)

    # Seconds added to the API function's timeout for the teardown's wait (see _probe_teardown).
    TEARDOWN_SETTLE_MARGIN = 5

    def _probe_teardown(self, exclude_regions, arns, instance_arns, users, settle_seconds):
        """On stack delete only: a custom resource whose function removes the probe-tagged launch
        templates and the probe security groups (and terminates probe instances that still run) in
        every enabled Region minus excludeRegions. It always answers SUCCESS within a bounded time
        and logs what it could not remove, so it never blocks the stack delete. `users` (the
        functions that create probe resources) depend on it, so CloudFormation deletes them first.
        A deleted function does not stop a run already in progress, so the teardown first waits
        `settle_seconds` (the API function's timeout plus a margin) for such a test launch to end,
        and the custom resource depends on the users' roles, so their permissions stay until the
        teardown is done."""
        logs_group = logs.LogGroup(
            self, "ProbeTeardownLogs", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY
        )
        role = iam.Role(self, "ProbeTeardownRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        logs_group.grant_write(role)
        role.add_to_policy(iam.PolicyStatement(
            sid="FindProbeResources",
            actions=["ec2:DescribeRegions", "ec2:DescribeInstances", "ec2:DescribeLaunchTemplates",
                     "ec2:DescribeSecurityGroups"],
            resources=["*"]))
        # Acts only when the stack itself is being deleted: reads its own stack's status.
        role.add_to_policy(iam.PolicyStatement(
            sid="ReadOwnStackStatus", actions=["cloudformation:DescribeStacks"], resources=[Aws.STACK_ID]))
        role.add_to_policy(iam.PolicyStatement(
            sid="TerminateProbeInstancesOnly", actions=["ec2:TerminateInstances"], resources=instance_arns,
            conditions={"StringEquals": {f"aws:ResourceTag/{PROBE_TAG}": "true"}}))
        role.add_to_policy(iam.PolicyStatement(
            sid="DeleteProbeLaunchTemplatesOnly", actions=["ec2:DeleteLaunchTemplate"], resources=arns("launch-template/*"),
            conditions={"StringEquals": {f"aws:ResourceTag/{PROBE_TAG}": "true"}}))
        role.add_to_policy(iam.PolicyStatement(
            sid="DeleteProbeSecurityGroupsOnly", actions=["ec2:DeleteSecurityGroup"], resources=arns("security-group/*"),
            conditions={"StringEquals": {f"aws:ResourceTag/{PROBE_SG_TAG}": "true"}}))
        fn = lambda_.Function(
            self,
            "ProbeTeardownFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="teardown.handler",
            code=lambda_.Code.from_asset("lambda", exclude=["__pycache__", "*.pyc"]),
            role=role,
            timeout=self.TEARDOWN_TIMEOUT,
            memory_size=256,
            log_group=logs_group,
            environment={"EXCLUDE_REGIONS": exclude_regions, "SETTLE_SECONDS": str(settle_seconds)},
        )
        teardown = CustomResource(
            self,
            "ProbeTeardown",
            service_token=fn.function_arn,
            resource_type="Custom::ProbeTeardown",
            service_timeout=self.TEARDOWN_SERVICE_TIMEOUT,
        )
        # The role's policy must outlive the custom resource's delete.
        teardown.node.add_dependency(role)
        for user in users:
            user.node.add_dependency(teardown)
            # The role and its policy are deleted after the teardown: a test launch still running
            # during the wait can end its own instance and launch template.
            teardown.node.add_dependency(user.role)
        return role, fn

    # The Duration alarm of a function fires when one run comes this close to its timeout.
    DURATION_ALARM_SHARE = 0.8
    # One period for the per-function alarms: short enough to notice, long enough to stay cheap.
    FUNCTION_ALARM_PERIOD = Duration.minutes(5)

    def _alarm(self, construct_id, name, description, metric, threshold, period=None,
               operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD):
        """One alarm of this stack. It notifies the alarmTopicArn topic when the deployment set one,
        and otherwise has no action and shows only in the CloudWatch console. One data point of one
        period is enough to alarm, and a period with no data is not breaching: the functions run on
        a schedule or on a request, so "no data" means nothing happened, not that something broke."""
        alarm = cloudwatch.Alarm(
            self,
            construct_id,
            alarm_name=f"{self.stack_name}-{name}",
            alarm_description=description,
            metric=metric,
            threshold=threshold,
            evaluation_periods=1,
            comparison_operator=operator,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        if self._alarm_actions:
            alarm.add_alarm_action(*self._alarm_actions)
        return alarm

    def _function_alarms(self, construct_id, fn, label, timeout, errors=True):
        """The alarms of one Lambda function: Errors above 0, a Duration that comes close to the
        timeout (a timeout writes no log line of its own, so this is the warning before it), and
        Throttles above 0. They read the AWS/Lambda metrics the platform publishes, so they need no
        code and no new permission."""
        if errors:
            self._alarm(f"{construct_id}ErrorsAlarm", f"{label}-errors",
                        f"The {label} function returned an error, or the platform stopped a run at its timeout. "
                        f"Check the {construct_id} log group.",
                        fn.metric_errors(period=self.FUNCTION_ALARM_PERIOD, statistic="Sum"), 0)
        near = int(timeout.to_milliseconds() * self.DURATION_ALARM_SHARE)
        self._alarm(f"{construct_id}DurationAlarm", f"{label}-duration",
                    f"A run of the {label} function took longer than {near} ms, close to its "
                    f"{timeout.to_seconds()}-second timeout. A run the platform stops leaves no answer and no "
                    f"request line: look for a slow Region or a cold cache in the {construct_id} log group.",
                    fn.metric_duration(period=self.FUNCTION_ALARM_PERIOD, statistic="Maximum"), near)
        self._alarm(f"{construct_id}ThrottlesAlarm", f"{label}-throttles",
                    f"Lambda throttled the {label} function: its runs reached the account's concurrency limit, "
                    "so requests or scheduled checks were dropped.",
                    fn.metric_throttles(period=self.FUNCTION_ALARM_PERIOD, statistic="Sum"), 0)

    def _cleanup_alarms(self, cleanup_fn, cleanup_logs):
        """Alarms for a broken safety net. A run that fails in every Region raises, so it counts as a
        Lambda error; a run that fails in some Regions only logs "probe cleanup failed in <Region>",
        so a metric filter counts those. Both use one 15-minute period (one scheduled run) and one
        data point, so a single failed run alarms; a period with no data (no run or no failure line)
        is not breaching."""
        self._alarm(
            "CleanupErrorsAlarm",
            "probe-cleanup-errors",
            "The probe cleanup function failed (every Region failed, or the run crashed or timed out). "
            "Leftover test launch instances may keep running: check the CleanupFunction logs.",
            cleanup_fn.metric_errors(period=Duration.minutes(15), statistic="Sum"),
            1,
            operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        )
        region_failures = logs.MetricFilter(
            self,
            "CleanupRegionFailures",
            log_group=cleanup_logs,
            # The warning cleanup.py logs for each Region it could not clean, and for a failed
            # Region lookup ("probe cleanup failed in region-discovery").
            filter_pattern=logs.FilterPattern.literal('"probe cleanup failed in"'),
            # One namespace per stack, so two deployments in one account do not share the count.
            metric_namespace=f"GpuCapacityFinder/{self.stack_name}",
            metric_name="ProbeCleanupRegionFailures",
            metric_value="1",
            default_value=0,
        )
        self._alarm(
            "CleanupRegionFailuresAlarm",
            "probe-cleanup-region-failures",
            "The probe cleanup function could not clean one or more Regions. Leftover test launch instances "
            "there may keep running: check the CleanupFunction logs for the Region.",
            region_failures.metric(period=Duration.minutes(15), statistic="Sum"),
            1,
            operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        )

    def _suppress(self, bucket, pool, dist, api_role, cleanup_role, notifier_role, updater_role, teardown_role,
                  functions):
        NagSuppressions.add_resource_suppressions(
            bucket,
            [{"id": "AwsSolutions-S1", "reason": "Demo bucket serving a static page; access logging is out of scope for the sample."}],
        )
        NagSuppressions.add_resource_suppressions(
            dist,
            [
                {"id": "AwsSolutions-CFR1", "reason": "No geo restriction: the audience is the customer's own users."},
                {"id": "AwsSolutions-CFR2", "reason": "Sign-in is required for every API call; WAF is listed under Limitations in the README."},
                {"id": "AwsSolutions-CFR3", "reason": "Access logging is out of scope for the sample."},
                {"id": "AwsSolutions-CFR4", "reason": "The default CloudFront certificate fixes the viewer TLS floor; a custom domain with its own certificate lifts it."},
            ],
        )
        NagSuppressions.add_resource_suppressions(
            pool,
            [{"id": "AwsSolutions-COG8", "reason": "The Plus feature plan is a paid tier and out of scope for the sample."}],
        )
        NagSuppressions.add_resource_suppressions_by_path(
            self,
            f"/{self.node.id}/Api/DefaultStage/Resource",
            [{"id": "AwsSolutions-APIG1", "reason": "Access logging is out of scope; the Lambda function logs every request failure."}],
        )
        api_suppressions = [
            {
                "id": "AwsSolutions-IAM5",
                "reason": (
                    "Resource * is only for actions without resource-level permissions: the EC2 Describe and offering "
                    "reads (including DescribeInstanceTypes and DescribeRegions), GetSpotPlacementScores, the Service "
                    "Quotas list calls, pricing:GetProducts (Price List data is public list prices, not a resource; the API "
                    "has no resource-level permissions), and CloudWatch GetMetricData for quota usage. These reads cover "
                    "every Region the account has enabled. The EC2 ARN wildcards stand for resource IDs unknown in "
                    "advance, in any Region, because test launches run in every enabled Region (the function refuses an "
                    "excluded or not enabled Region before any call). A test launch is one EC2 Fleet of type instant: "
                    "RunInstances on instance/* only with the probe tag and the tracked instance-family patterns (an IAM "
                    "StringLike condition cannot express \"a digit follows g or p\", and g* also matches gr6, so the "
                    "function's check_family refuses a type outside the naming rule before it launches); CreateFleet "
                    "on fleet/* and instance/* only with the probe tag; RunInstances and CreateFleet "
                    "on snapshot/*, subnet/*, network-interface/*, volume/*, and spot-instances-request/* for the "
                    "resources a launch uses, on image/* only for images owned by amazon (ec2:Owner; the probe image is "
                    "an Amazon Linux 2023 AMI from an AWS public parameter), and on security-group/* only for the probe "
                    "security group (aws:ResourceTag); ec2:DescribeInstances (no resource-level permissions) finds the "
                    "instance of a lost CreateFleet answer by its per-launch tag; CreateLaunchTemplate on launch-template/* "
                    "only with the probe tag, and RunInstances, CreateFleet and DeleteLaunchTemplate on launch-template/* "
                    "only for templates that carry it; CreateSecurityGroup on security-group/* only with the probe "
                    "security group tag, and on vpc/* for the VPC the group is created in; CreateTags on instance/*, "
                    "volume/*, network-interface/*, fleet/*, launch-template/* and security-group/* only at creation "
                    "(ec2:CreateAction); TerminateInstances on instance/* only with the probe tag. The daily test launch "
                    "counters use dynamodb:LeadingKeys conditions (counter partitions only). Unsubscribe is "
                    "limited to the alert topic and its subscriptions, whose IDs are unknown before creation. "
                    "ssm:GetParameter and ssm:GetParameters on parameter/aws/service/global-infrastructure/regions/* "
                    "read the AWS public Region long-name parameters in the stack's own Region only; the wildcard stands "
                    "for the Region codes the page lists, which change as Regions are enabled. ssm:GetParameter on the "
                    "two Amazon Linux 2023 public image parameters names every Region (ssm:*) for the same reason. "
                    "lambda:InvokeFunction is limited to the score updater function (and its versions)."
                ),
            }
        ]
        NagSuppressions.add_resource_suppressions(api_role, api_suppressions, apply_to_children=True)
        # Statements CDK moves to OverflowPolicy managed policies at synthesis get the same reason.
        Aspects.of(api_role).add(_SuppressOnRoleAfterSplit(api_role, api_suppressions))
        if cleanup_role:
            NagSuppressions.add_resource_suppressions(
                cleanup_role,
                [
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": (
                            "ec2:DescribeInstances, ec2:DescribeLaunchTemplates and ec2:DescribeRegions do not support "
                            "resource-level permissions, so they need resource *. TerminateInstances is limited to "
                            "instance/* and DeleteLaunchTemplate to launch-template/* (any Region: test launches run in "
                            "every enabled Region), both only for resources that carry the probe tag (aws:ResourceTag "
                            "condition); the IDs are unknown in advance."
                        ),
                    }
                ],
                apply_to_children=True,
            )
        if teardown_role:
            NagSuppressions.add_resource_suppressions(
                teardown_role,
                [
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": (
                            "The stack-delete teardown finds the probe resources with ec2:DescribeRegions, "
                            "DescribeInstances, DescribeLaunchTemplates and DescribeSecurityGroups, which do not support "
                            "resource-level permissions, so they need resource *. It removes them in any enabled Region "
                            "(test launches run in every enabled Region), and the IDs are unknown in advance: "
                            "TerminateInstances on instance/* and DeleteLaunchTemplate on launch-template/* only with the "
                            "probe tag, DeleteSecurityGroup on security-group/* only with the probe security group tag "
                            "(aws:ResourceTag conditions)."
                        ),
                    }
                ],
                apply_to_children=True,
            )
        if notifier_role:
            NagSuppressions.add_resource_suppressions(
                notifier_role,
                [
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": (
                            "DescribeCapacityBlockOfferings and DescribeRegions are read-only and "
                            "do not support resource-level permissions, so they need resource *; alerts may target any "
                            "Region the account has enabled. "
                            "Publish is limited to the alert topic. GetSubscriptionAttributes is limited to the alert topic "
                            "and its subscriptions, whose IDs are unknown before creation."
                        ),
                    }
                ],
                apply_to_children=True,
            )
        NagSuppressions.add_resource_suppressions(
            updater_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "DescribeRegions, DescribeInstanceTypeOfferings, DescribeInstanceTypes, GetSpotPlacementScores, "
                        "and DescribeSpotPriceHistory are read-only and do not support resource-level permissions, so "
                        "they need resource *; the scores and prices cover every Region the account has enabled."
                    ),
                }
            ],
            apply_to_children=True,
        )
        for fn in functions:
            NagSuppressions.add_resource_suppressions(
                fn,
                [{"id": "AwsSolutions-L1", "reason": "The functions run on Python 3.13, the runtime the sample code and its tests target; cdk-nag flags it because a newer Python runtime exists."}],
            )
        # The BucketDeployment helper (a CDK singleton function and its role) is generated by CDK.
        for helper in self.node.children:
            if helper.node.id.startswith("Custom::CDKBucketDeployment"):
                NagSuppressions.add_resource_suppressions(
                    helper,
                    [
                        {"id": "AwsSolutions-IAM4", "reason": "The CDK-generated BucketDeployment helper function uses the AWSLambdaBasicExecutionRole managed policy."},
                        {"id": "AwsSolutions-IAM5", "reason": "The CDK-generated BucketDeployment helper reads the CDK asset bucket and writes the site bucket; CDK sets its permissions."},
                        {"id": "AwsSolutions-L1", "reason": "The CDK-generated BucketDeployment helper function picks its own runtime."},
                    ],
                    apply_to_children=True,
                )
