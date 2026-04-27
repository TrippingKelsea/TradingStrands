import aws_cdk as cdk
from aws_cdk import (
    aws_certificatemanager as acm,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecr as ecr,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_ecs_patterns as ecs_patterns,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as events_targets,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_lambda_event_sources as lambda_event_sources,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_route53 as route53,
)
from aws_cdk import (
    aws_route53_targets as targets,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class TradingStrandsStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        domain_name: str = "",
        zone_name: str = "",
        hosted_zone_id: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Cost allocation tags ──────────────────────────────────────────
        cdk.Tags.of(self).add("Project", "TradingStrands")
        cdk.Tags.of(self).add("Environment", "production")
        cdk.Tags.of(self).add("ManagedBy", "CDK")

        tls_enabled = bool(domain_name and hosted_zone_id)
        # zone_name defaults to domain_name for apex domains
        zone_name = zone_name or domain_name

        # AllowedCidr is REQUIRED — no default. Publicly-open ALB listeners
        # (0.0.0.0/0) on AWS-internal accounts trigger the Epoxy
        # ELBListenerDelete mitigation which deletes listeners after ~18 min.
        allowed_cidr_param = cdk.CfnParameter(
            self,
            "AllowedCidr",
            type="String",
            description=(
                "CIDR range allowed to reach the dashboard ALB "
                "(must not be 0.0.0.0/0 on internal accounts)"
            ),
        )

        # ECR repository (created by CI workflow, referenced here)
        repository = ecr.Repository.from_repository_name(
            self, "TradingStrandsRepo", "trading-strands",
        )

        # DynamoDB table — tagged for cost tracking.
        # Streams enabled with NEW_AND_OLD_IMAGES so the StrategySupervisor
        # can diff old vs new status on MODIFY events (needed to tell a
        # markdown edit from a pause transition).
        table = dynamodb.Table(
            self,
            "TradingStrandsState",
            table_name="trading-strands-state",
            partition_key=dynamodb.Attribute(
                name="pk",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl",  # used by EVENT#, TOKENEVENT#, LEDGER_EVENT#
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
        )

        # Agent memory bucket (v0: single shared bucket, prefix-isolated per
        # Agent; v1: replaced by per-Agent buckets provisioned by
        # BotProvisioner — see docs/SPEC/deployment.md). Bucket name is
        # account-suffixed so re-deploys into a clean account don't collide
        # with a retained bucket from a prior tenant.
        agent_memory_bucket = s3.Bucket(
            self,
            "AgentMemoryBucket",
            bucket_name=f"trading-strands-agent-memory-{self.account}",
            versioned=True,  # recoverable from accidental overwrites
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,  # dev; production would RETAIN
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="raw-daily-to-glacier",
                    prefix="",  # applies to all objects
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=cdk.Duration.days(90),
                        ),
                    ],
                ),
            ],
        )

        # Secrets Manager secret (seeded manually by operator)
        alpaca_secret = secretsmanager.Secret(
            self,
            "AlpacaSecret",
            secret_name="trading-strands/alpaca",
            description="Alpaca API credentials - seed manually after stack deploy",
        )

        # Cognito client secret — created here so ECS can reference it on
        # first deploy; CI overwrites the value after stack deploy.
        cognito_client_secret = secretsmanager.Secret(
            self,
            "CognitoClientSecret",
            secret_name="trading-strands/cognito-client-secret",
            description="Cognito app client secret - seeded by CI after deploy",
        )

        # Cognito user pool for dashboard authentication
        user_pool = cognito.UserPool(
            self,
            "DashboardUserPool",
            user_pool_name="trading-strands-dashboard",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            custom_attributes={
                "role": cognito.StringAttribute(
                    min_len=1, max_len=20, mutable=True,
                ),
                "org_id": cognito.StringAttribute(
                    min_len=0, max_len=40, mutable=True,
                ),
            },
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        user_pool_client = user_pool.add_client(
            "DashboardAppClient",
            user_pool_client_name="dashboard",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(
                user_password=True,
            ),
        )

        # ECS cluster on default VPC
        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)
        cluster = ecs.Cluster(self, "TradingStrandsCluster", vpc=vpc)

        # -- Trading service --------------------------------------------------

        trading_task_role = iam.Role(
            self,
            "TradingTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        trading_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=["*"],
            )
        )
        alpaca_secret.grant_read(trading_task_role)
        table.grant_read_write_data(trading_task_role)
        # Trading service reads per-org Alpaca secrets at runtime
        # (trading-strands/org/{org_id}/alpaca). Scoped to those paths so
        # the trading service cannot touch the Cognito client secret or
        # any other unrelated secret.
        trading_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:"
                    "secret:trading-strands/org/*",
                ],
            ),
        )
        # Agent memory bucket — read/write scoped to this one bucket.
        agent_memory_bucket.grant_read_write(trading_task_role)

        trading_task_def = ecs.FargateTaskDefinition(
            self,
            "TradingTaskDef",
            cpu=512,
            memory_limit_mib=1024,
            task_role=trading_task_role,
        )
        cdk.Tags.of(trading_task_def).add("Component", "trading-service")
        trading_log_group = logs.LogGroup(
            self,
            "TradingLogGroup",
            log_group_name="/ecs/trading-strands/trading",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        trading_task_def.add_container(
            "TradingContainer",
            image=ecs.ContainerImage.from_ecr_repository(repository, tag="latest"),
            environment={
                "DYNAMODB_TABLE": table.table_name,
                "SECRETS_MANAGER_SECRET_NAME": alpaca_secret.secret_name,
                "ALPACA_PAPER": "true",
                "AGENT_MEMORY_BUCKET": agent_memory_bucket.bucket_name,
            },
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="trading",
                log_group=trading_log_group,
            ),
        )

        # max_healthy_percent=100 / min_healthy_percent=0 ensures at most one
        # task is running at any time, preventing duplicate order submission.
        trading_service = ecs.FargateService(
            self,
            "TradingService",
            cluster=cluster,
            task_definition=trading_task_def,
            desired_count=1,
            min_healthy_percent=0,
            max_healthy_percent=100,
            assign_public_ip=True,
        )

        # -- Market Data Subscriber service ----------------------------------
        #
        # Pulls quotes for the union of symbols across active strategies and
        # writes to MarketDataStore. Runs 24/7 (no weekend scale-down) so
        # premarket + extended-hours data is captured regardless of whether
        # any trading task is up. Uses the superwoman org's paper Alpaca
        # creds — same secret the market-data fetch side of the trading
        # service already reads.
        subscriber_task_role = iam.Role(
            self,
            "MarketDataSubscriberTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        table.grant_read_write_data(subscriber_task_role)
        alpaca_secret.grant_read(subscriber_task_role)

        subscriber_task_def = ecs.FargateTaskDefinition(
            self,
            "MarketDataSubscriberTaskDef",
            cpu=256,
            memory_limit_mib=512,
            task_role=subscriber_task_role,
        )
        cdk.Tags.of(subscriber_task_def).add(
            "Component", "marketdata-subscriber",
        )
        subscriber_log_group = logs.LogGroup(
            self,
            "MarketDataSubscriberLogGroup",
            log_group_name="/ecs/trading-strands/marketdata-subscriber",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        subscriber_task_def.add_container(
            "SubscriberContainer",
            image=ecs.ContainerImage.from_ecr_repository(
                repository, tag="latest",
            ),
            environment={
                "DYNAMODB_TABLE": table.table_name,
                "SECRETS_MANAGER_SECRET_NAME": alpaca_secret.secret_name,
            },
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="marketdata-subscriber",
                log_group=subscriber_log_group,
            ),
            entry_point=["uv", "run", "python", "-m"],
            command=["trading_strands.marketdata_subscriber.serve"],
        )
        ecs.FargateService(
            self,
            "MarketDataSubscriberService",
            cluster=cluster,
            task_definition=subscriber_task_def,
            desired_count=1,
            min_healthy_percent=0,
            max_healthy_percent=100,
            assign_public_ip=True,
        )

        # -- Dashboard service ------------------------------------------------

        dashboard_task_role = iam.Role(
            self,
            "DashboardTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        # Dashboard needs read/write: reads snapshots+events, writes strategies
        table.grant_read_write_data(dashboard_task_role)
        # Dashboard needs Cognito admin access for user management.
        # AdminGetUser is needed by /api/admin/users to read enabled/status
        # for each user on the admin page — without it the UI shows every
        # user as disabled.
        dashboard_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:ListUsers",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminDeleteUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminUpdateUserAttributes",
                    "cognito-idp:AdminEnableUser",
                    "cognito-idp:AdminDisableUser",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )
        # Dashboard needs Cost Explorer access for cost tracking
        dashboard_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ce:GetCostAndUsage"],
                resources=["*"],
            )
        )
        # Dashboard reads self-critique lessons.md out of the agent-memory
        # bucket. Read-only — operators view, they don't edit. (Writes are
        # the Self-Critique Lambda's job.)
        agent_memory_bucket.grant_read(dashboard_task_role)
        # Dashboard reads per-bot Fargate service state to render status
        # indicators. DescribeServices only — nothing that can change state.
        # Resource scope is the cluster's services pattern; ECS requires
        # resources="*" for DescribeServices in practice, but we add a
        # condition to scope to this cluster.
        dashboard_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices"],
                resources=["*"],
                conditions={
                    "ArnEquals": {"ecs:cluster": cluster.cluster_arn},
                },
            )
        )
        # Dashboard writes per-org Alpaca credentials to Secrets Manager.
        # Deliberately scoped to /org/*/alpaca — the dashboard never needs
        # to touch the global trading-strands/alpaca secret, which remains
        # readable only by the trading service.
        dashboard_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:"
                    "secret:trading-strands/org/*",
                ],
            )
        )

        dashboard_task_def = ecs.FargateTaskDefinition(
            self,
            "DashboardTaskDef",
            cpu=256,
            memory_limit_mib=512,
            task_role=dashboard_task_role,
        )
        cdk.Tags.of(dashboard_task_def).add("Component", "dashboard-service")
        dashboard_log_group = logs.LogGroup(
            self,
            "DashboardLogGroup",
            log_group_name="/ecs/trading-strands/dashboard",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        dashboard_task_def.add_container(
            "DashboardContainer",
            image=ecs.ContainerImage.from_ecr_repository(repository, tag="dashboard"),
            environment={
                "DYNAMODB_TABLE": table.table_name,
                "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
                "AGENT_MEMORY_BUCKET": agent_memory_bucket.bucket_name,
                "ECS_CLUSTER": cluster.cluster_name,
            },
            secrets={
                "COGNITO_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                    cognito_client_secret,
                ),
            },
            port_mappings=[ecs.PortMapping(container_port=8080)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="dashboard",
                log_group=dashboard_log_group,
            ),
            entry_point=["uv", "run", "uvicorn"],
            command=[
                "trading_strands.dashboard.serve:app",
                "--host", "0.0.0.0",
                "--port", "8080",
            ],
        )

        # ALB security group - restrict inbound to operator CIDR
        alb_sg = ec2.SecurityGroup(
            self,
            "DashboardAlbSg",
            vpc=vpc,
            description="Dashboard ALB - inbound restricted to operator IP",
        )
        alb_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(allowed_cidr_param.value_as_string),
            connection=ec2.Port.tcp(80),
            description="Operator HTTP access to dashboard",
        )
        if tls_enabled:
            alb_sg.add_ingress_rule(
                peer=ec2.Peer.ipv4(allowed_cidr_param.value_as_string),
                connection=ec2.Port.tcp(443),
                description="Operator HTTPS access to dashboard",
            )

        # TLS: look up hosted zone and create ACM certificate
        certificate = None
        if tls_enabled:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                self, "HostedZone",
                hosted_zone_id=hosted_zone_id,
                zone_name=zone_name,
            )
            certificate = acm.Certificate(
                self,
                "DashboardCert",
                domain_name=domain_name,
                validation=acm.CertificateValidation.from_dns(zone),
            )

        dashboard_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "DashboardService",
            cluster=cluster,
            task_definition=dashboard_task_def,
            desired_count=1,
            listener_port=443 if tls_enabled else 80,
            protocol=(
                elbv2.ApplicationProtocol.HTTPS if tls_enabled
                else elbv2.ApplicationProtocol.HTTP
            ),
            certificate=certificate,
            redirect_http=tls_enabled,
            target_protocol=elbv2.ApplicationProtocol.HTTP,
            assign_public_ip=True,
            # open_listener=False prevents the pattern from adding 0.0.0.0/0
            # on the auto-created LB SG. Our restrictive alb_sg is attached
            # below so inbound is limited to the operator CIDR only. This
            # avoids tripping Epoxy's ELBListenerDelete mitigation.
            open_listener=False,
        )
        dashboard_service.load_balancer.add_security_group(alb_sg)

        dashboard_service.target_group.configure_health_check(
            path="/health",
            healthy_http_codes="200",
        )

        # Route 53 alias record
        if tls_enabled:
            route53.ARecord(
                self,
                "DashboardAliasRecord",
                zone=zone,
                record_name=domain_name,
                target=route53.RecordTarget.from_alias(
                    targets.LoadBalancerTarget(dashboard_service.load_balancer),
                ),
            )

        # -- Cost control scheduler ------------------------------------------
        #
        # Trading service runs only during market hours (weekdays 6am-9pm ET).
        # Dashboard stays up 24/7 so sysadmins can log in anytime.
        #
        # EventBridge cron expressions are UTC-anchored. We use EST offsets
        # (UTC-5) rather than DST-aware because EventBridge can't track DST —
        # during summer, wake is effectively 7am local / sleep 10pm local,
        # which still covers US market hours (9:30am-4pm ET). Premarket
        # activity before the wake window is captured by the market data
        # subscriber (commit 10) which runs 24/7 on a separate service.
        #
        # Cron format: "minute hour day-of-month month day-of-week year"
        # 6am ET (EST) = 11:00 UTC
        # 9pm ET (EST) = 02:00 UTC next day — so the sleep rule runs
        # Tue-Sat at 02:00 UTC to cover "Mon-Fri 9pm ET".
        wake_rule = events.Rule(
            self,
            "TradingServiceWakeRule",
            description="Wake trading service at 6am ET on weekdays",
            schedule=events.Schedule.cron(
                minute="0", hour="11",
                week_day="MON-FRI",
            ),
        )
        sleep_rule = events.Rule(
            self,
            "TradingServiceSleepRule",
            description="Sleep trading service at 9pm ET on weekdays",
            schedule=events.Schedule.cron(
                minute="0", hour="2",
                week_day="TUE-SAT",
            ),
        )

        # Use the AwsApi target so we can call UpdateService directly.
        # EventBridge's built-in EcsTask target launches new tasks on a
        # schedule; UpdateService is what we need to change desired_count.
        wake_rule.add_target(events_targets.AwsApi(
            service="ECS",
            action="updateService",
            parameters={
                "cluster": cluster.cluster_name,
                "service": trading_service.service_name,
                "desiredCount": 1,
            },
            policy_statement=iam.PolicyStatement(
                actions=["ecs:UpdateService"],
                resources=[trading_service.service_arn],
            ),
        ))
        sleep_rule.add_target(events_targets.AwsApi(
            service="ECS",
            action="updateService",
            parameters={
                "cluster": cluster.cluster_name,
                "service": trading_service.service_name,
                "desiredCount": 0,
            },
            policy_statement=iam.PolicyStatement(
                actions=["ecs:UpdateService"],
                resources=[trading_service.service_arn],
            ),
        ))

        # -- Self-Critique Agent (weekend Lambda) ----------------------------
        #
        # Runs one invocation per active Strategy Agent; the BotProvisioner
        # Lambda below enumerates strategies and calls this function per bot.
        self_critique_fn = lambda_.DockerImageFunction(
            self,
            "SelfCritiqueFunction",
            function_name="trading-strands-self-critique",
            code=lambda_.DockerImageCode.from_ecr(
                repository=repository,
                tag_or_digest="latest",
                cmd=["trading_strands.self_critique.lambda_handler.handler"],
            ),
            memory_size=1024,
            timeout=cdk.Duration.minutes(5),
            environment={
                "DYNAMODB_TABLE": table.table_name,
                "AGENT_MEMORY_BUCKET": agent_memory_bucket.bucket_name,
                # Model ID can be overridden per deploy. Defaults to Sonnet
                # because this is a reflective workload where quality matters
                # more than latency, but cheaper than Opus.
                "SELF_CRITIQUE_MODEL_ID": "us.anthropic.claude-sonnet-4-6",
            },
        )
        cdk.Tags.of(self_critique_fn).add("Component", "self-critique-agent")
        table.grant_read_write_data(self_critique_fn)
        agent_memory_bucket.grant_read_write(self_critique_fn)
        self_critique_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            resources=["*"],
        ))

        # -- Periodic review agents (Risk / Compliance / Auditor) ------------
        #
        # All three share the same Lambda shape: per-org invocation, reads
        # DDB, reads+writes the agent-memory bucket, calls Bedrock, appends
        # a recommendation. Factored into a helper so the three stay in
        # lockstep.
        #
        # An org-fanout Lambda (defined further down) is wired as the
        # target of each schedule, passing the review-agent's function
        # name as payload. All three schedules remain enabled=False on
        # first deploy; flip them individually once confident.
        def _build_review_agent(
            construct_id: str,
            function_name: str,
            handler_path: str,
            model_env_var: str,
            component_tag: str,
            schedule_id: str,
            schedule_description: str,
            schedule_cron: events.Schedule,
        ) -> tuple[lambda_.DockerImageFunction, events.Rule]:
            fn = lambda_.DockerImageFunction(
                self,
                construct_id,
                function_name=function_name,
                code=lambda_.DockerImageCode.from_ecr(
                    repository=repository,
                    tag_or_digest="latest",
                    cmd=[handler_path],
                ),
                memory_size=1024,
                timeout=cdk.Duration.minutes(5),
                environment={
                    "DYNAMODB_TABLE": table.table_name,
                    "AGENT_MEMORY_BUCKET": agent_memory_bucket.bucket_name,
                    model_env_var: "us.anthropic.claude-sonnet-4-6",
                },
            )
            cdk.Tags.of(fn).add("Component", component_tag)
            table.grant_read_data(fn)
            agent_memory_bucket.grant_read_write(fn)
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=["*"],
            ))
            rule = events.Rule(
                self,
                schedule_id,
                description=schedule_description,
                schedule=schedule_cron,
                enabled=False,
            )
            return fn, rule

        risk_agent_fn, risk_rule = _build_review_agent(
            construct_id="RiskAgentFunction",
            function_name="trading-strands-risk-agent",
            handler_path="trading_strands.risk_agent.lambda_handler.handler",
            model_env_var="RISK_AGENT_MODEL_ID",
            component_tag="risk-agent",
            schedule_id="RiskAgentWeeklySchedule",
            schedule_description=(
                "Sunday 10:00 UTC — per-org Risk Agent reviewer."
            ),
            schedule_cron=events.Schedule.cron(
                minute="0", hour="10", week_day="SUN",
            ),
        )

        compliance_agent_fn, compliance_rule = _build_review_agent(
            construct_id="ComplianceAgentFunction",
            function_name="trading-strands-compliance-agent",
            handler_path=(
                "trading_strands.compliance_agent.lambda_handler.handler"
            ),
            model_env_var="COMPLIANCE_AGENT_MODEL_ID",
            component_tag="compliance-agent",
            schedule_id="ComplianceAgentWeeklySchedule",
            schedule_description=(
                "Sunday 11:00 UTC — per-org Compliance Agent reviewer. "
                "Runs an hour after Risk so recommendations accumulate "
                "in a predictable order for the operator."
            ),
            schedule_cron=events.Schedule.cron(
                minute="0", hour="11", week_day="SUN",
            ),
        )

        # -- Auditor Agent (per-org, has halt authority) ---------------------
        #
        # Not built via _build_review_agent because its IAM is
        # qualitatively different: it needs Secrets Manager read for the
        # org's Alpaca creds AND write access to the CONTROL row to halt
        # the desk on drift. Keeping this out of the shared helper makes
        # the "wait, why does this Lambda have DDB write?" answer
        # immediate rather than buried in a helper kwarg.
        auditor_agent_fn = lambda_.DockerImageFunction(
            self,
            "AuditorAgentFunction",
            function_name="trading-strands-auditor-agent",
            code=lambda_.DockerImageCode.from_ecr(
                repository=repository,
                tag_or_digest="latest",
                cmd=[
                    "trading_strands.auditor_agent.lambda_handler.handler",
                ],
            ),
            memory_size=1024,
            timeout=cdk.Duration.minutes(5),
            environment={
                "DYNAMODB_TABLE": table.table_name,
                "AGENT_MEMORY_BUCKET": agent_memory_bucket.bucket_name,
                "AUDITOR_AGENT_MODEL_ID": "us.anthropic.claude-sonnet-4-6",
            },
        )
        cdk.Tags.of(auditor_agent_fn).add("Component", "auditor-agent")
        # Read + Write: the auditor writes the CONTROL row when it halts.
        table.grant_read_write_data(auditor_agent_fn)
        agent_memory_bucket.grant_read_write(auditor_agent_fn)
        auditor_agent_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            resources=["*"],
        ))
        # Per-org Alpaca creds — same scoping as the trading task role.
        auditor_agent_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue"],
            resources=[
                f"arn:aws:secretsmanager:{self.region}:{self.account}:"
                "secret:trading-strands/org/*",
            ],
        ))

        # Daily-ish cadence lines up with the spec's "periodic pulls"
        # language. Disabled on first deploy.
        auditor_rule = events.Rule(
            self,
            "AuditorAgentDailySchedule",
            description=(
                "Weekdays 23:00 UTC — per-org Auditor Agent. Runs after "
                "US close so broker positions are settled for the day."
            ),
            schedule=events.Schedule.cron(
                minute="0", hour="23", week_day="MON-FRI",
            ),
            enabled=False,
        )

        # -- Org-fanout Lambda (review-agent scheduler target) ---------------
        #
        # One Lambda, invoked by each review-agent schedule with the target
        # review-agent's function name in the payload. Walks TenancyStore,
        # async-invokes the target once per org.
        #
        # Shared target rather than three parallel fanout Lambdas because
        # the enumeration code is identical and we'd rather have one place
        # to audit IAM/logs for "which agents run against which orgs".
        org_fanout_fn = lambda_.DockerImageFunction(
            self,
            "OrgFanoutFunction",
            function_name="trading-strands-org-fanout",
            code=lambda_.DockerImageCode.from_ecr(
                repository=repository,
                tag_or_digest="latest",
                cmd=["trading_strands.org_fanout.fanout.handler"],
            ),
            memory_size=256,
            timeout=cdk.Duration.minutes(2),
            environment={"DYNAMODB_TABLE": table.table_name},
        )
        cdk.Tags.of(org_fanout_fn).add("Component", "org-fanout")
        table.grant_read_data(org_fanout_fn)
        # Invoke authority scoped to exactly the three review agents.
        # Nothing else this Lambda can ever invoke, regardless of payload.
        for review_fn in (
            risk_agent_fn, compliance_agent_fn, auditor_agent_fn,
        ):
            review_fn.grant_invoke(org_fanout_fn)

        # Attach the fanout to each review-agent rule with the per-rule
        # target function in the payload. `from_object` serializes the
        # dict as the event body the Lambda handler reads.
        risk_rule.add_target(events_targets.LambdaFunction(
            org_fanout_fn,
            event=events.RuleTargetInput.from_object({
                "target_function": risk_agent_fn.function_name,
            }),
        ))
        compliance_rule.add_target(events_targets.LambdaFunction(
            org_fanout_fn,
            event=events.RuleTargetInput.from_object({
                "target_function": compliance_agent_fn.function_name,
            }),
        ))
        auditor_rule.add_target(events_targets.LambdaFunction(
            org_fanout_fn,
            event=events.RuleTargetInput.from_object({
                "target_function": auditor_agent_fn.function_name,
            }),
        ))

        # -- BotProvisioner (weekend fan-out) --------------------------------
        #
        # Enumerates active strategies and invokes the Self-Critique function
        # once per bot. Async (Event) invocations, so one slow critique
        # doesn't starve the rest of the fleet.
        bot_provisioner_fn = lambda_.DockerImageFunction(
            self,
            "BotProvisionerFunction",
            function_name="trading-strands-bot-provisioner",
            code=lambda_.DockerImageCode.from_ecr(
                repository=repository,
                tag_or_digest="latest",
                cmd=["trading_strands.provisioner.bot_provisioner.handler"],
            ),
            memory_size=256,
            timeout=cdk.Duration.minutes(2),
            environment={
                "DYNAMODB_TABLE": table.table_name,
                "SELF_CRITIQUE_FUNCTION_NAME": self_critique_fn.function_name,
            },
        )
        cdk.Tags.of(bot_provisioner_fn).add("Component", "bot-provisioner")
        table.grant_read_data(bot_provisioner_fn)
        self_critique_fn.grant_invoke(bot_provisioner_fn)

        # EventBridge schedule: Saturday 10:00 UTC = 6am ET. Markets are
        # closed on Saturday; strategies have finished their week; the
        # current week's memory is available for review.
        weekend_rule = events.Rule(
            self,
            "SelfCritiqueWeekendSchedule",
            description=(
                "Saturday 10:00 UTC — BotProvisioner enumerates active "
                "strategies and fans out Self-Critique invocations."
            ),
            schedule=events.Schedule.cron(
                minute="0", hour="10",
                week_day="SAT",
            ),
            # Disabled in this deploy. Enable once we've seen at least one
            # successful manual run in prod and verified the reflection
            # lands in the agent-memory bucket.
            enabled=False,
        )
        weekend_rule.add_target(
            events_targets.LambdaFunction(bot_provisioner_fn),
        )

        # -- StrategySupervisor (per-bot Fargate lifecycle) ------------------
        #
        # Reads DDB Streams (NEW_AND_OLD_IMAGES) and reconciles per-bot ECS
        # services. Only component with create/update/delete on those
        # services — concentrates the blast radius of a bug.
        #
        # Bot task execution role: the per-bot Fargate tasks the supervisor
        # creates reuse the existing trading_task_def's execution role.
        # trading_task_def exposes `.execution_role` only after a container
        # with logConfiguration is added — which has already happened above.
        #
        # Bot task security group: egress-only (bots only make outbound
        # calls — Bedrock, Alpaca, DDB, S3). No inbound.
        bot_task_sg = ec2.SecurityGroup(
            self,
            "BotTaskSecurityGroup",
            vpc=vpc,
            description="Per-bot Fargate tasks; egress only",
            allow_all_outbound=True,
        )

        # The supervisor needs references to roles + log group it didn't
        # create. trading_task_def.execution_role is auto-created by CDK
        # when add_container is called; we extract its ARN to pass to the
        # supervisor at runtime.
        bot_exec_role = trading_task_def.execution_role
        assert bot_exec_role is not None, (
            "TradingTaskDef should have an execution role by this point"
        )

        # Env the supervisor and the one-shot reconciler both need to
        # create/manage per-bot Fargate services. Defined once so the
        # two Lambdas stay in lockstep.
        supervisor_env = {
            "ECS_CLUSTER": cluster.cluster_name,
            "TASK_DEFINITION_FAMILY": "ts-bot",
            "CONTAINER_IMAGE": f"{repository.repository_uri}:latest",
            "TASK_ROLE_ARN": trading_task_role.role_arn,
            "EXECUTION_ROLE_ARN": bot_exec_role.role_arn,
            "SUBNET_IDS": ",".join(
                s.subnet_id for s in vpc.public_subnets
            ),
            "SECURITY_GROUP_IDS": bot_task_sg.security_group_id,
            "LOG_GROUP_NAME": trading_log_group.log_group_name,
            "DYNAMODB_TABLE": table.table_name,
            "AGENT_MEMORY_BUCKET": agent_memory_bucket.bucket_name,
            "SECRETS_MANAGER_SECRET_NAME": alpaca_secret.secret_name,
        }

        strategy_supervisor_fn = lambda_.DockerImageFunction(
            self,
            "StrategySupervisorFunction",
            function_name="trading-strands-strategy-supervisor",
            code=lambda_.DockerImageCode.from_ecr(
                repository=repository,
                tag_or_digest="latest",
                cmd=[
                    "trading_strands.supervisor.strategy_supervisor.handler",
                ],
            ),
            memory_size=256,
            timeout=cdk.Duration.minutes(2),
            environment=supervisor_env,
        )
        cdk.Tags.of(strategy_supervisor_fn).add(
            "Component", "strategy-supervisor",
        )

        # Shared IAM for supervisor-family Lambdas: ECS admin on the
        # cluster's services + RegisterTaskDefinition + PassRole scoped
        # to the two roles per-bot tasks need. Factored so the one-shot
        # reconciler can get exactly the same grants.
        def _grant_supervisor_iam(fn: lambda_.IFunction) -> None:
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=[
                    "ecs:DescribeServices",
                    "ecs:CreateService",
                    "ecs:UpdateService",
                    "ecs:DeleteService",
                    "ecs:RegisterTaskDefinition",
                    "ecs:DescribeTaskDefinition",
                ],
                resources=["*"],
            ))
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    trading_task_role.role_arn,
                    bot_exec_role.role_arn,
                ],
            ))

        _grant_supervisor_iam(strategy_supervisor_fn)

        # Hook the supervisor to DDB Streams. Batch size 10 so we reconcile
        # quickly without overwhelming ECS API limits. TRIM_HORIZON on
        # first-deploy would replay the whole history; start LATEST so we
        # only see new changes from the cutover point onward (operators
        # can manually reconcile existing strategies via a one-shot).
        strategy_supervisor_fn.add_event_source(
            lambda_event_sources.DynamoEventSource(
                table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                bisect_batch_on_error=True,
                retry_attempts=3,
            ),
        )

        # -- One-shot reconciler ---------------------------------------------
        #
        # Manually invoked at cutover to bring every active strategy onto
        # per-bot Fargate (the streams handler starts at LATEST, so
        # pre-existing strategies don't auto-spawn services). Reuses the
        # supervisor's reconcile path — one code flow for service creation.
        # Run dry-run first:
        #     aws lambda invoke --function-name trading-strands-reconcile-all \
        #         --payload '{"dry_run": true}' /tmp/out.json
        reconcile_all_fn = lambda_.DockerImageFunction(
            self,
            "ReconcileAllFunction",
            function_name="trading-strands-reconcile-all",
            code=lambda_.DockerImageCode.from_ecr(
                repository=repository,
                tag_or_digest="latest",
                cmd=[
                    "trading_strands.supervisor.reconcile_all.handler",
                ],
            ),
            memory_size=256,
            timeout=cdk.Duration.minutes(5),
            environment=supervisor_env,
        )
        cdk.Tags.of(reconcile_all_fn).add(
            "Component", "strategy-supervisor",
        )
        table.grant_read_data(reconcile_all_fn)
        _grant_supervisor_iam(reconcile_all_fn)

        # -- Platform Supervisor (v1 health monitor) -------------------------
        #
        # Scans the heartbeat table on a 1-minute cron, classifies each
        # agent as healthy/stale/missing, emits EMF metrics so a CloudWatch
        # alarm can fire on any missing count > 0 without log scraping.
        # Distinct from StrategySupervisor — this one only *observes*
        # agent liveness, doesn't touch ECS.
        platform_supervisor_fn = lambda_.DockerImageFunction(
            self,
            "PlatformSupervisorFunction",
            function_name="trading-strands-platform-supervisor",
            code=lambda_.DockerImageCode.from_ecr(
                repository=repository,
                tag_or_digest="latest",
                cmd=[
                    "trading_strands.platform_supervisor.supervisor.handler",
                ],
            ),
            memory_size=256,
            timeout=cdk.Duration.seconds(30),
            environment={
                "DYNAMODB_TABLE": table.table_name,
                "SUPERVISOR_STALE_AFTER_SECONDS": "60",
                "SUPERVISOR_MISSING_AFTER_SECONDS": "300",
            },
        )
        cdk.Tags.of(platform_supervisor_fn).add(
            "Component", "platform-supervisor",
        )
        table.grant_read_data(platform_supervisor_fn)

        # 1-minute cron. Minute-level resolution on missing agents is
        # fine for this workload — the trade loop's 5s tick cadence
        # means a missing agent is genuinely concerning after a full
        # minute of no heartbeats.
        events.Rule(
            self,
            "PlatformSupervisorSchedule",
            description=(
                "Every minute — scan heartbeats, emit EMF health metrics."
            ),
            schedule=events.Schedule.rate(cdk.Duration.minutes(1)),
            targets=[
                events_targets.LambdaFunction(platform_supervisor_fn),
            ],
        )

        # -- Halt-transition alarms ------------------------------------------
        #
        # halt.transition.count is emitted by HaltStore ONLY on actual
        # state changes (see src/trading_strands/halt/store.py). Any
        # breach of the alarm below = a real halt just happened.
        #
        # No alarm action wired yet — no SNS target exists in the account.
        # Alarms show in the CloudWatch console; once an ops target
        # (email/Slack via SNS) is chosen, wire it via add_alarm_action().
        # Treating missing data as notBreaching is load-bearing: the
        # metric emits nothing during normal operation and CloudWatch
        # would otherwise evaluate missing data as breach.
        system_halt_alarm = cloudwatch.Alarm(
            self,
            "SystemHaltAlarm",
            alarm_name="trading-strands-system-halt",
            alarm_description=(
                "Sysadmin emergency stop fired. All orgs halted. "
                "Investigate via dashboard /api/halt state + reason."
            ),
            metric=cloudwatch.Metric(
                namespace="TradingStrands",
                metric_name="halt.transition.count",
                dimensions_map={"scope": "system", "halted": "true"},
                period=cdk.Duration.minutes(1),
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator
                .GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            # Disabled until an SNS notification target exists.
            # Flip actions_enabled=True after wiring add_alarm_action().
            actions_enabled=False,
        )
        cdk.Tags.of(system_halt_alarm).add("Component", "halt-alarms")

        org_halt_alarm = cloudwatch.Alarm(
            self,
            "OrgHaltAlarm",
            alarm_name="trading-strands-org-halt",
            alarm_description=(
                "One or more orgs halted (Auditor drift or orgadmin "
                "action). Dimensions identify which org from logs."
            ),
            metric=cloudwatch.Metric(
                namespace="TradingStrands",
                metric_name="halt.transition.count",
                dimensions_map={"scope": "org", "halted": "true"},
                # 5-min window smooths short halt/resume sequences
                # during drift-confirmation testing.
                period=cdk.Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator
                .GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            actions_enabled=False,
        )
        cdk.Tags.of(org_halt_alarm).add("Component", "halt-alarms")

        # -- Outputs ----------------------------------------------------------

        dashboard_url = (
            f"https://{domain_name}"
            if tls_enabled
            else f"http://{dashboard_service.load_balancer.load_balancer_dns_name}"
        )
        cdk.CfnOutput(
            self,
            "DashboardUrl",
            value=dashboard_url,
            description="Dashboard URL",
        )
        cdk.CfnOutput(
            self,
            "EcrRepositoryUri",
            value=repository.repository_uri,
            description="ECR repository URI for docker push",
        )
        cdk.CfnOutput(
            self,
            "DynamoDbTableName",
            value=table.table_name,
            description="DynamoDB state table name",
        )
        cdk.CfnOutput(
            self,
            "CognitoUserPoolId",
            value=user_pool.user_pool_id,
            description="Cognito User Pool ID for dashboard auth",
        )
        cdk.CfnOutput(
            self,
            "CognitoClientId",
            value=user_pool_client.user_pool_client_id,
            description="Cognito App Client ID for dashboard auth",
        )
