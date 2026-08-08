"""The stack catalogue.

Each entry describes one piece of infrastructure a user can ask for: what it
depends on, which community module implements it, and what the tool needs to
ask before it can generate anything.

Modules are **referenced by version, never vendored**. Copying
`terraform-aws-modules` into this repository would mean inheriting their
maintenance burden, freezing their security fixes, and taking on their
licensing. A pinned source reference gets their upstream fixes for free and
keeps the provenance honest.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Question:
    """One thing to ask the user.

    ``when`` gates the question on answers already given, so the interview
    stays short: nobody is asked about node groups if they did not pick EKS.
    """

    key: str
    prompt: str
    kind: str  # choice | multichoice | int | text | confirm
    choices: tuple[str, ...] = ()
    default: object = None
    help: str = ""
    when: str | None = None  # key that must be truthy
    minimum: int | None = None
    maximum: int | None = None
    recommend: str = ""  # shown when the user has no preference


@dataclass(frozen=True, slots=True)
class Stack:
    id: str
    name: str
    summary: str
    category: str
    module_source: str
    module_version: str
    # The attribute this module names its primary resource with. It is not
    # `name` for several of them, and getting it wrong fails at `init` with an
    # "Unsupported argument" error rather than anything the schema would catch.
    name_attribute: str = "name"
    requires: tuple[str, ...] = ()  # stack ids that must also be present
    questions: tuple[Question, ...] = ()
    monthly_floor_usd: float = 0.0
    notes: str = ""
    # Whether `_module_block` emits enough arguments for `terraform apply` to
    # succeed. False means the block carries the module reference and a name and
    # nothing else, which `terraform validate` accepts because every unset
    # argument has a null or empty default upstream. The failure surfaces at the
    # AWS API, which is the most expensive place to find it.
    #
    # This flag is asserted against the generated output in the test suite, so
    # it cannot drift away from what the generator actually emits. Flipping it
    # to True without configuring the stack fails the build.
    configured: bool = False

    @property
    def module_ref(self) -> str:
        return f'source = "{self.module_source}"\n  version = "{self.module_version}"'


VPC = Stack(
    id="vpc",
    name="VPC and networking",
    summary="Subnets across availability zones, routing, NAT, and flow logs.",
    category="foundation",
    module_source="terraform-aws-modules/vpc/aws",
    module_version="~> 5.13",
    configured=True,
    questions=(
        Question(
            "cidr",
            "VPC CIDR block",
            "text",
            default="10.0.0.0/16",
            help="Must not overlap anything you plan to peer with.",
        ),
        Question(
            "az_count",
            "How many availability zones",
            "int",
            default=2,
            minimum=1,
            maximum=6,
            recommend="Two is enough for most workloads. Three only if you "
            "genuinely need to survive an AZ loss with no capacity dip.",
        ),
        Question(
            "nat_gateway_per_az",
            "One NAT gateway per AZ",
            "confirm",
            default=False,
            help="Higher availability, and roughly $32 per gateway per month "
            "before data charges. A single shared gateway is usually right "
            "outside production.",
        ),
        Question(
            "enable_flow_logs",
            "Enable VPC flow logs",
            "confirm",
            default=True,
            help="You will want these the first time you debug a connectivity "
            "problem, and you cannot enable them retroactively.",
        ),
    ),
)

EKS = Stack(
    id="eks",
    name="EKS (Kubernetes)",
    summary="Managed control plane, node groups, IRSA, and add-ons.",
    category="compute",
    module_source="terraform-aws-modules/eks/aws",
    module_version="~> 20.31",
    configured=True,
    name_attribute="cluster_name",
    requires=("vpc",),
    monthly_floor_usd=73.0,
    questions=(
        Question(
            "k8s_version",
            "Kubernetes version",
            "choice",
            choices=("1.31", "1.30", "1.29"),
            default="1.31",
            help="EKS supports roughly the latest four. Older versions enter "
            "extended support and cost more.",
        ),
        Question(
            "node_group_count",
            "How many node groups",
            "int",
            default=1,
            minimum=1,
            maximum=10,
            recommend="One general-purpose group to start. Add a second only "
            "when you have a real reason: GPUs, spot isolation, or a "
            "taint-based tenancy boundary.",
        ),
        Question(
            "node_instance_type",
            "Node instance type",
            "choice",
            choices=(
                "m6i.large",
                "m6i.xlarge",
                "c6i.large",
                "r6i.large",
                "t3.medium",
                "g5.xlarge",
                "suggest",
            ),
            default="suggest",
            recommend="m6i.large for mixed workloads. c6i for CPU-bound, r6i "
            "for memory-bound, g5 for GPU. t3 burstable only for dev, "
            "because CPU credit exhaustion under load looks exactly "
            "like a mysterious latency problem.",
        ),
        Question("node_min", "Minimum nodes per group", "int", default=2, minimum=0, maximum=100),
        Question("node_max", "Maximum nodes per group", "int", default=6, minimum=1, maximum=1000),
        Question(
            "spot",
            "Use spot instances for node groups",
            "confirm",
            default=False,
            help="Cheaper, and interrupted with two minutes notice. Fine for "
            "stateless and batch, not for anything holding state.",
        ),
        Question(
            "irsa",
            "Enable IRSA (IAM roles for service accounts)",
            "confirm",
            default=True,
            help="The alternative is node-wide credentials, which every pod on the node inherits.",
        ),
    ),
)

RDS = Stack(
    id="rds",
    name="RDS (managed relational database)",
    summary="PostgreSQL or MySQL, private subnets, encrypted, backed up.",
    category="data",
    module_source="terraform-aws-modules/rds/aws",
    module_version="~> 6.10",
    configured=True,
    name_attribute="identifier",
    requires=("vpc",),
    monthly_floor_usd=50.0,
    questions=(
        Question(
            "engine", "Database engine", "choice", choices=("postgres", "mysql"), default="postgres"
        ),
        Question(
            "instance_class",
            "Instance class",
            "choice",
            choices=("db.t4g.medium", "db.m6g.large", "db.r6g.large", "suggest"),
            default="suggest",
            recommend="db.t4g.medium for dev, db.m6g.large as a production "
            "starting point, db.r6g.large when the working set does "
            "not fit in memory.",
        ),
        Question(
            "multi_az",
            "Multi-AZ deployment",
            "confirm",
            default=False,
            help="Roughly doubles cost and removes the single-AZ failure mode. "
            "Recommended for production, wasteful below it.",
        ),
        Question(
            "storage_gb", "Allocated storage in GB", "int", default=50, minimum=20, maximum=65536
        ),
        Question(
            "backup_retention_days",
            "Backup retention in days",
            "int",
            default=7,
            minimum=1,
            maximum=35,
            help="Seven is the usual floor: a Friday failure found on Monday "
            "needs three days of history to recover from.",
        ),
    ),
    notes="The master password is never generated or committed. A sensitive "
    "variable is emitted and the value is expected from a secret manager.",
)

MSK = Stack(
    id="msk",
    name="MSK (managed Kafka)",
    summary="Kafka cluster with encryption in transit and at rest.",
    category="data",
    module_source="terraform-aws-modules/msk-kafka-cluster/aws",
    module_version="~> 2.9",
    requires=("vpc",),
    monthly_floor_usd=260.0,
    questions=(
        Question(
            "broker_count",
            "Number of brokers",
            "int",
            default=3,
            minimum=2,
            maximum=15,
            recommend="Three, matching your AZ count. Two cannot form a quorum "
            "that survives losing one.",
        ),
        Question(
            "broker_type",
            "Broker instance type",
            "choice",
            choices=("kafka.t3.small", "kafka.m5.large", "kafka.m5.xlarge", "suggest"),
            default="suggest",
            recommend="kafka.m5.large for production. kafka.t3.small is dev only; "
            "it will throttle under sustained load.",
        ),
    ),
)

REDSHIFT = Stack(
    id="redshift",
    name="Redshift (data warehouse)",
    summary="Columnar warehouse in private subnets, encrypted.",
    category="data",
    module_source="terraform-aws-modules/redshift/aws",
    module_version="~> 6.2",
    name_attribute="cluster_identifier",
    requires=("vpc",),
    monthly_floor_usd=790.0,
    questions=(
        Question(
            "node_type",
            "Node type",
            "choice",
            choices=("ra3.xlplus", "ra3.4xlarge", "suggest"),
            default="suggest",
            recommend="ra3.xlplus is the smallest RA3 and the usual starting "
            "point. Below a terabyte, question whether you need "
            "Redshift rather than Athena over S3.",
        ),
        Question("node_count", "Number of nodes", "int", default=2, minimum=1, maximum=128),
    ),
    notes="Expensive. Consider Athena over S3 first if the working set is small "
    "or the query pattern is intermittent.",
)

ELASTICACHE = Stack(
    id="elasticache",
    name="ElastiCache (Redis)",
    summary="Managed Redis in private subnets, encrypted, with auth.",
    category="data",
    module_source="terraform-aws-modules/elasticache/aws",
    module_version="~> 1.4",
    name_attribute="replication_group_id",
    requires=("vpc",),
    monthly_floor_usd=25.0,
    questions=(
        Question(
            "node_type",
            "Node type",
            "choice",
            choices=("cache.t4g.micro", "cache.r7g.large", "suggest"),
            default="suggest",
        ),
        Question("replicas", "Replica count", "int", default=1, minimum=0, maximum=5),
    ),
)

S3 = Stack(
    id="s3",
    name="S3 buckets",
    summary="Buckets with public access blocked, encryption, and versioning.",
    category="data",
    module_source="terraform-aws-modules/s3-bucket/aws",
    module_version="~> 4.2",
    name_attribute="bucket",
    questions=(
        Question("bucket_count", "How many buckets", "int", default=1, minimum=1, maximum=20),
        Question(
            "versioning",
            "Enable versioning",
            "confirm",
            default=True,
            help="The cheapest protection against an accidental delete or an encryption event.",
        ),
    ),
)

ALB = Stack(
    id="alb",
    name="Application Load Balancer",
    summary="ALB with TLS, access logs, and security groups.",
    category="networking",
    module_source="terraform-aws-modules/alb/aws",
    module_version="~> 9.13",
    requires=("vpc",),
    monthly_floor_usd=18.0,
    questions=(
        Question("certificate", "ACM certificate ARN, or blank to create one", "text", default=""),
    ),
)

OBSERVABILITY = Stack(
    id="observability",
    name="Observability",
    summary="Prometheus, Grafana, and Loki on the cluster, with retention set.",
    category="platform",
    module_source="terraform-aws-modules/eks/aws//modules/eks-managed-node-group",
    module_version="~> 20.31",
    requires=("eks",),
    questions=(
        Question(
            "retention_days", "Metric retention in days", "int", default=15, minimum=1, maximum=365
        ),
        Question(
            "managed_grafana",
            "Use Amazon Managed Grafana instead of self-hosted",
            "confirm",
            default=False,
        ),
    ),
    notes="Self-hosted is cheaper and yours to operate. Managed costs more per "
    "user and is one less thing to be paged about.",
)

ALL_STACKS: tuple[Stack, ...] = (
    VPC,
    EKS,
    RDS,
    MSK,
    REDSHIFT,
    ELASTICACHE,
    S3,
    ALB,
    OBSERVABILITY,
)

BY_ID: dict[str, Stack] = {s.id: s for s in ALL_STACKS}


def resolve_dependencies(selected: list[str]) -> list[str]:
    """Expand a selection to include everything it requires.

    Returns dependency-first order, so a generator can emit modules in an order
    that reads sensibly and so `depends_on` is rarely needed.
    """
    out: list[str] = []
    seen: set[str] = set()

    def visit(sid: str) -> None:
        if sid in seen:
            return
        if sid not in BY_ID:
            raise KeyError(f"unknown stack {sid!r}; known: {sorted(BY_ID)}")
        seen.add(sid)
        for dep in BY_ID[sid].requires:
            visit(dep)
        out.append(sid)

    for sid in selected:
        visit(sid)
    return out


def questions_for(selected: list[str]) -> list[tuple[str, Question]]:
    """Every question implied by a selection, in dependency order."""
    return [(sid, q) for sid in resolve_dependencies(selected) for q in BY_ID[sid].questions]


def monthly_floor(selected: list[str], environments: int = 1) -> float:
    """Rough fixed monthly cost before compute and storage."""
    return sum(BY_ID[s].monthly_floor_usd for s in resolve_dependencies(selected)) * environments
