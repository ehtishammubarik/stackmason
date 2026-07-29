"""Security and cost guardrails applied to every generated stack.

This is the load-bearing module. A scaffolding tool multiplies whatever it
emits: one insecure default becomes a hundred insecure deployments, and the
people who run them will reasonably assume the generator knew better.

So the defaults here are not suggestions. A caller can override an individual
guardrail, but doing so is explicit, recorded in the generated repo, and
surfaced in the plan output. The tool never silently emits something it knows
to be unsafe.

Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    BLOCK = "block"  # will not generate
    WARN = "warn"  # generates, with the risk recorded in the repo
    NOTE = "note"  # informational, usually cost


@dataclass(frozen=True, slots=True)
class Finding:
    """Something the generator noticed about the requested configuration."""

    severity: Severity
    code: str
    message: str
    remedy: str

    def render(self) -> str:
        return f"[{self.severity.value.upper()}] {self.code}: {self.message}\n    -> {self.remedy}"


# Ports that must never be reachable from the internet. Opening one of these to
# 0.0.0.0/0 is the single most common serious finding in real Terraform, and it
# is exploited by automated scanners within hours rather than weeks.
DATA_PORTS: dict[int, str] = {
    22: "SSH",
    3389: "RDP",
    5432: "PostgreSQL",
    3306: "MySQL",
    1433: "SQL Server",
    27017: "MongoDB",
    6379: "Redis",
    9092: "Kafka",
    5439: "Redshift",
    9200: "Elasticsearch",
}

# Rough on-demand USD per month, us-east-1, for the sizing warnings. These are
# order-of-magnitude figures for steering a choice, not a quote, and the
# generated repo says so.
_MONTHLY_COST: dict[str, float] = {
    "nat_gateway": 32.0,
    "eks_control_plane": 73.0,
    "rds_db.t3.medium": 50.0,
    "rds_db.r6g.large": 175.0,
    "msk_kafka.m5.large": 260.0,
    "redshift_ra3.xlplus": 790.0,
    "alb": 18.0,
}


@dataclass
class GuardrailReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.severity is Severity.BLOCK for f in self.findings)

    def by_severity(self, s: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is s]

    def render(self) -> str:
        if not self.findings:
            return "no findings"
        order = (Severity.BLOCK, Severity.WARN, Severity.NOTE)
        return "\n".join(f.render() for s in order for f in self.findings if f.severity is s)


def check_network(answers: dict) -> list[Finding]:
    """Refuse to expose a data port to the internet."""
    out: list[Finding] = []
    cidrs = answers.get("allowed_cidrs") or []

    if "0.0.0.0/0" in cidrs:
        out.append(
            Finding(
                Severity.BLOCK,
                "NET001",
                "0.0.0.0/0 was requested for private service access.",
                "Supply your office or VPN CIDR. For ad-hoc access use SSM Session "
                "Manager, which needs no inbound rule at all.",
            )
        )

    if answers.get("public_database"):
        out.append(
            Finding(
                Severity.BLOCK,
                "NET002",
                "A publicly accessible database was requested.",
                "Place the database in private subnets and reach it through a bastion "
                "or SSM. If you genuinely need public access, this generator is the "
                "wrong tool.",
            )
        )

    if answers.get("bastion") and not answers.get("allowed_cidrs"):
        out.append(
            Finding(
                Severity.BLOCK,
                "NET003",
                "A bastion was requested with no source CIDR restriction.",
                "Set allowed_cidrs, or drop the bastion and use SSM Session Manager.",
            )
        )
    return out


def check_data_protection(answers: dict) -> list[Finding]:
    """Encryption, backups, and deletion protection on anything holding data."""
    out: list[Finding] = []
    stateful = {"rds", "msk", "redshift", "elasticache", "opensearch"} & set(
        answers.get("stacks", [])
    )
    if not stateful:
        return out

    if answers.get("environments") and "prod" in answers["environments"]:
        if answers.get("skip_final_snapshot"):
            out.append(
                Finding(
                    Severity.BLOCK,
                    "DAT001",
                    "skip_final_snapshot was requested for a production database.",
                    "A destroy would be unrecoverable. Leave it false in prod.",
                )
            )
        if answers.get("backup_retention_days", 7) < 7:
            out.append(
                Finding(
                    Severity.WARN,
                    "DAT002",
                    f"Backup retention of {answers.get('backup_retention_days')} days in production.",
                    "Seven days is the usual floor. Below that, a Friday failure "
                    "discovered on Monday is unrecoverable.",
                )
            )
    return out


def check_state(answers: dict) -> list[Finding]:
    """Remote state with locking, always."""
    out: list[Finding] = []
    if answers.get("backend") == "local":
        out.append(
            Finding(
                Severity.WARN,
                "STA001",
                "Local state was selected.",
                "State holds every secret the plan touched, in plaintext, on one "
                "laptop. Use an S3 backend with DynamoDB locking before more than "
                "one person runs this.",
            )
        )
    return out


def estimate_cost(answers: dict) -> list[Finding]:
    """Surface the monthly bill that is invisible in a diff."""
    out: list[Finding] = []
    envs = len(answers.get("environments", ["dev"]))
    total = 0.0
    lines: list[str] = []

    azs = answers.get("az_count", 2)
    if answers.get("nat_gateway_per_az"):
        n = azs * envs
        cost = n * _MONTHLY_COST["nat_gateway"]
        total += cost
        lines.append(f"{n} NAT gateways: ~${cost:.0f}/mo")
        if azs >= 3:
            out.append(
                Finding(
                    Severity.NOTE,
                    "COST001",
                    f"One NAT gateway per AZ across {azs} AZs and {envs} environment(s) "
                    f"is {n} gateways, roughly ${cost:.0f} per month before data charges.",
                    "A single shared NAT gateway costs a third as much and is usually "
                    "correct outside production.",
                )
            )

    stacks = set(answers.get("stacks", []))
    if "eks" in stacks:
        cost = _MONTHLY_COST["eks_control_plane"] * envs
        total += cost
        lines.append(f"{envs} EKS control plane(s): ~${cost:.0f}/mo")

    if total:
        out.append(
            Finding(
                Severity.NOTE,
                "COST000",
                f"Baseline before compute and storage: ~${total:.0f}/month. " + "; ".join(lines),
                "Order of magnitude only, us-east-1 on-demand. Not a quote.",
            )
        )
    return out


ALL_CHECKS = (check_network, check_data_protection, check_state, estimate_cost)


def evaluate(answers: dict) -> GuardrailReport:
    """Run every guardrail. Never short circuits, so the report is complete."""
    report = GuardrailReport()
    for check in ALL_CHECKS:
        report.findings.extend(check(answers))
    return report


# Defaults injected into every generated stack unless explicitly overridden.
# Listed here rather than scattered through templates so they are reviewable in
# one place, which is the only way anyone will ever audit them.
SECURE_DEFAULTS: dict[str, object] = {
    "storage_encrypted": True,
    "deletion_protection": True,
    "skip_final_snapshot": False,
    "backup_retention_days": 7,
    "publicly_accessible": False,
    "enable_flow_logs": True,
    "enforce_imdsv2": True,
    "block_public_acls": True,
    "enable_key_rotation": True,
    "require_ssl": True,
}
