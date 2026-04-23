import aws_cdk as cdk
from aws_cdk import (
    aws_certificatemanager as acm,
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
    aws_iam as iam,
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

        tls_enabled = bool(domain_name and hosted_zone_id)
        # zone_name defaults to domain_name for apex domains
        zone_name = zone_name or domain_name

        allowed_cidr_param = cdk.CfnParameter(
            self,
            "AllowedCidr",
            type="String",
            default="0.0.0.0/0",
            description="CIDR range allowed to reach the dashboard ALB",
        )

        # ECR repository (created by CI workflow, referenced here)
        repository = ecr.Repository.from_repository_name(
            self, "TradingStrandsRepo", "trading-strands",
        )

        # DynamoDB table
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

        trading_task_def = ecs.FargateTaskDefinition(
            self,
            "TradingTaskDef",
            cpu=512,
            memory_limit_mib=1024,
            task_role=trading_task_role,
        )
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
            },
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="trading",
                log_group=trading_log_group,
            ),
        )

        # max_healthy_percent=100 / min_healthy_percent=0 ensures at most one
        # task is running at any time, preventing duplicate order submission.
        ecs.FargateService(
            self,
            "TradingService",
            cluster=cluster,
            task_definition=trading_task_def,
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

        dashboard_task_def = ecs.FargateTaskDefinition(
            self,
            "DashboardTaskDef",
            cpu=256,
            memory_limit_mib=512,
            task_role=dashboard_task_role,
        )
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
