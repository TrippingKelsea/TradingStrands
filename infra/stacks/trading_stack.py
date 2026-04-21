import aws_cdk as cdk
from aws_cdk import (
    aws_certificatemanager as acm,
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
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class TradingStrandsStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        allowed_cidr_param = cdk.CfnParameter(
            self,
            "AllowedCidr",
            type="String",
            default="0.0.0.0/0",
            description="CIDR range allowed to reach the dashboard ALB",
        )

        domain_name_param = cdk.CfnParameter(
            self,
            "DomainName",
            type="String",
            default="",
            description="Domain name for TLS (e.g. tradingstrands.io). Leave empty to skip TLS.",
        )

        hosted_zone_id_param = cdk.CfnParameter(
            self,
            "HostedZoneId",
            type="String",
            default="",
            description="Route 53 hosted zone ID for the domain. Required if DomainName is set.",
        )

        # Condition: TLS is enabled when domain name is provided
        tls_enabled = cdk.CfnCondition(
            self,
            "TlsEnabled",
            expression=cdk.Fn.condition_not(
                cdk.Fn.condition_equals(domain_name_param.value_as_string, ""),
            ),
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
        alb_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(allowed_cidr_param.value_as_string),
            connection=ec2.Port.tcp(443),
            description="Operator HTTPS access to dashboard",
        )

        dashboard_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "DashboardService",
            cluster=cluster,
            task_definition=dashboard_task_def,
            desired_count=1,
            listener_port=80,
            target_protocol=elbv2.ApplicationProtocol.HTTP,
            assign_public_ip=True,
        )
        dashboard_service.load_balancer.add_security_group(alb_sg)

        dashboard_service.target_group.configure_health_check(
            path="/health",
            healthy_http_codes="200",
        )

        # -- TLS (conditional on DomainName parameter) ------------------------

        # ACM certificate - DNS validated against the hosted zone
        certificate = acm.Certificate(
            self,
            "DashboardCert",
            domain_name=domain_name_param.value_as_string,
            validation=acm.CertificateValidation.from_dns(),
        )
        # Only create cert when TLS is enabled
        certificate.node.default_child.cfn_options.condition = tls_enabled

        # HTTPS listener on port 443
        https_listener = dashboard_service.load_balancer.add_listener(
            "HttpsListener",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[certificate],
            default_target_groups=[dashboard_service.target_group],
        )
        https_listener.node.default_child.cfn_options.condition = tls_enabled

        # Redirect HTTP to HTTPS (modify the existing port-80 listener)
        redirect_action = elbv2.CfnListenerRule(
            self,
            "HttpToHttpsRedirect",
            listener_arn=dashboard_service.listener.listener_arn,
            priority=1,
            conditions=[
                elbv2.CfnListenerRule.RuleConditionProperty(
                    field="path-pattern",
                    values=["/*"],
                ),
            ],
            actions=[
                elbv2.CfnListenerRule.ActionProperty(
                    type="redirect",
                    redirect_config=elbv2.CfnListenerRule.RedirectConfigProperty(
                        protocol="HTTPS",
                        port="443",
                        status_code="HTTP_301",
                    ),
                ),
            ],
        )
        redirect_action.cfn_options.condition = tls_enabled

        # Route 53 alias record pointing domain to the ALB
        alias_record = route53.CfnRecordSet(
            self,
            "DashboardAliasRecord",
            name=domain_name_param.value_as_string,
            type="A",
            hosted_zone_id=hosted_zone_id_param.value_as_string,
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                dns_name=dashboard_service.load_balancer.load_balancer_dns_name,
                hosted_zone_id=dashboard_service.load_balancer.load_balancer_canonical_hosted_zone_id,
            ),
        )
        alias_record.cfn_options.condition = tls_enabled

        # -- Outputs ----------------------------------------------------------

        cdk.CfnOutput(
            self,
            "DashboardUrl",
            value=cdk.Fn.condition_if(
                tls_enabled.logical_id,
                f"https://{domain_name_param.value_as_string}",
                f"http://{dashboard_service.load_balancer.load_balancer_dns_name}",
            ).to_string(),
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
