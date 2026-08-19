"""Turn answers into a repository.

What comes out is not a snippet to paste. It is a repository laid out the way
an infrastructure repository should be: modules composed per environment,
remote state configured, providers pinned, CI that checks formatting and scans
for secrets, and an architecture document explaining why it looks like this.

Two rules govern the output:

1. **Secure by default, and the defaults are not negotiable silently.** A
   generator multiplies whatever it emits. Anything overridden away from the
   safe default is recorded in DECISIONS.md, so the deviation is visible to
   whoever inherits the repo.
2. **Never emit a credential.** Not a placeholder that looks real, not a
   generated one, not base64. Secrets are `sensitive` variables with no
   default, because a default is what ships.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .guardrails import SECURE_DEFAULTS, GuardrailReport, evaluate
from .stacks.registry import BY_ID, monthly_floor, resolve_dependencies

_ASSIGN = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<pad>\s*)=(?P<rest>\s.*)$")


def align_hcl(text: str) -> str:
    """Align `=` within each run of consecutive assignments, as terraform fmt does.

    Implemented here rather than shelling out to `terraform fmt`, because the
    generated repository ships CI that checks formatting. If output were
    unaligned, every generated repo would fail its own CI on the first commit,
    and the person who ran the generator would reasonably blame the generator.

    A run ends at a blank line, a comment, or a change of indent, which matches
    how terraform groups them.
    """
    lines = text.split("\n")
    out: list[str] = []
    run: list[tuple[str, str, str]] = []  # (indent, key, rest)
    run_indent: str | None = None

    def flush() -> None:
        nonlocal run, run_indent
        if run:
            width = max(len(k) for _, k, _ in run)
            out.extend(f"{i}{k.ljust(width)} ={r}" for i, k, r in run)
            run = []
            run_indent = None

    for line in lines:
        m = _ASSIGN.match(line)
        if m and not line.lstrip().startswith("#"):
            indent = m.group("indent")
            if run_indent is not None and indent != run_indent:
                flush()
            run_indent = indent
            run.append((indent, m.group("key"), m.group("rest")))
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


TERRAFORM_VERSION = "~> 1.9"
AWS_PROVIDER_VERSION = "~> 5.70"

# Used for both the provider and the state bucket when nothing else is given.
# A poor assumption for anyone outside North America, which is why `--region`
# exists and why the interview asks.
DEFAULT_REGION = "us-east-1"

# Stacks that actually consume `allowed_cidrs`. The variable is emitted only
# when one of these is selected, because a required variable nothing reads
# stops `terraform plan` to ask a question with no consequence.
CIDR_CONSUMERS = frozenset({"eks", "rds"})

# The port each engine listens on. Set explicitly on both the instance and its
# security group so the two cannot disagree: a security group opened on the
# wrong port fails as a timeout, which is the least diagnosable failure there
# is.
ENGINE_PORTS: dict[str, int] = {"postgres": 5432, "mysql": 3306}

# Everything the RDS module needs that cannot be derived from the engine name.
# `family` and `major_engine_version` feed the parameter group and the option
# group, and both are required arguments with no defaults. Omitting them fails
# at plan time, after 62 other resources have already planned successfully, with
# an error that names an upstream file rather than anything the user wrote.
ENGINE_DEFAULTS: dict[str, dict[str, str]] = {
    "postgres": {"version": "16.4", "family": "postgres16", "major_version": "16"},
    "mysql": {"version": "8.0.39", "family": "mysql8.0", "major_version": "8.0"},
}


@dataclass(frozen=True, slots=True)
class Variable:
    """One input variable, and everything needed to document it.

    `variables.tf` and `terraform.tfvars.example` are rendered from the same
    list, so a variable cannot exist in one and be missing from the other. That
    pairing used to be maintained by hand and was wrong: `allowed_cidrs` was
    required, absent from the example, and consumed by nothing, so copying the
    example and running `terraform plan` prompted for it.

    `default is None` means required. A required variable needs either an
    `example` value or, if it is a secret, an `example_note` saying where the
    value comes from. Secrets get a note rather than a value because an example
    credential is the credential that ships.
    """

    name: str
    description: str
    type: str
    default: str | None = None
    sensitive: bool = False
    validation: str = ""
    example: str | None = None
    example_note: str = ""

    @property
    def required(self) -> bool:
        return self.default is None

    def render(self) -> str:
        lines = [f'variable "{self.name}" {{']
        lines.append(f'  description = "{self.description}"')
        lines.append(f"  type        = {self.type}")
        if self.sensitive:
            lines.append("  sensitive   = true")
        if self.default is not None:
            lines.append(f"  default     = {self.default}")
        if self.validation:
            lines.append("")
            lines.extend(f"  {ln}" if ln else "" for ln in self.validation.strip().split("\n"))
        lines.append("}")
        return "\n".join(lines) + "\n"


@dataclass
class Plan:
    """What would be written, before anything is."""

    root: Path
    stacks: list[str]
    environments: list[str]
    answers: dict
    files: dict[str, str] = field(default_factory=dict)
    guardrails: GuardrailReport | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.guardrails and self.guardrails.blocked)

    def tree(self) -> str:
        return "\n".join(f"  {p}" for p in sorted(self.files))


def _versions_tf() -> str:
    return f'''terraform {{
  required_version = "{TERRAFORM_VERSION}"

  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "{AWS_PROVIDER_VERSION}"
    }}
  }}
}}
'''


def _providers_tf(project: str, env: str, region: str) -> str:
    """The provider block, with an explicit region and default tags.

    Without this file the provider takes its region from `AWS_REGION`,
    `AWS_DEFAULT_REGION`, or whichever profile happens to be active, while
    `backend.tf` hardcodes one. Nothing checks that the two agree, so
    `AWS_REGION=eu-west-1 terraform apply` writes state to Virginia and builds
    the VPC in Ireland, successfully and silently. The next person to run it
    from a different shell gets a plan that wants to destroy everything.

    `default_tags` is the other half. Untagged infrastructure is the most
    common reason a cloud bill cannot be explained, and the generator already
    knows both values: it puts them in every resource name.
    """
    return f'''# Region is explicit, not ambient.
#
# It defaults to the region backend.tf writes state to. Terraform does not
# allow variables in a backend block, so the two are generated together rather
# than derived from one another. Changing one means changing both.

provider "aws" {{
  region = var.aws_region

  default_tags {{
    tags = {{
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "{project}"
    }}
  }}
}}
'''


def _backend_tf(env: str, project: str, region: str) -> str:
    return f'''# Remote state with locking.
#
# State holds every secret the plan touched, in plaintext. Local state is one
# laptop away from unrecoverable and cannot be shared safely, so it is not an
# option this generator offers for a real environment.
#
# The region here must match var.aws_region in variables.tf. A backend block
# cannot reference a variable, so this is the one duplicated value in the
# repository, and it is checked by a test in the generator rather than left to
# whoever edits it next.
#
# Create the bucket and lock table once, before the first init:
#   aws s3api create-bucket --bucket {project}-tfstate --region {region}
#   aws s3api put-bucket-versioning --bucket {project}-tfstate \\
#     --versioning-configuration Status=Enabled
#   aws s3api put-bucket-encryption --bucket {project}-tfstate \\
#     --server-side-encryption-configuration \\
#     '{{"Rules":[{{"ApplyServerSideEncryptionByDefault":{{"SSEAlgorithm":"AES256"}}}}]}}'
#   aws dynamodb create-table --table-name {project}-tflock \\
#     --attribute-definitions AttributeName=LockID,AttributeType=S \\
#     --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST

terraform {{
  backend "s3" {{
    bucket         = "{project}-tfstate"
    key            = "{env}/terraform.tfstate"
    region         = "{region}"
    dynamodb_table = "{project}-tflock"
    encrypt        = true
  }}
}}
'''


def _module_block(sid: str, answers: dict, env: str) -> str:
    stack = BY_ID[sid]
    lines = [
        f"# {stack.name}",
        f"# {stack.summary}",
    ]
    if stack.notes:
        lines.append(f"# Note: {stack.notes}")
    lines.append(f'module "{sid}" {{')
    lines.append(f"  {stack.module_ref}")
    lines.append("")
    lines.append(f'  {stack.name_attribute} = "${{var.project}}-${{var.environment}}-{sid}"')

    if sid == "vpc":
        azs = answers.get("az_count", 2)
        lines += [
            "  cidr = local.vpc_cidr",
            f"  azs  = slice(data.aws_availability_zones.available.names, 0, {azs})",
            "",
            "  # Subnets are not optional. The module creates one per entry, and",
            "  # every list defaults to empty upstream, so omitting these produces",
            "  # a VPC with no subnets and an EKS cluster with nowhere to run.",
            "  #",
            "  # Private gets the largest blocks: nodes and pods are what exhaust a",
            "  # VPC. Public needs room only for NAT and load balancers, database",
            "  # almost none. See ARCHITECTURE.md before peering this VPC.",
            f"  private_subnets  = [for i in range({azs}) : cidrsubnet(local.vpc_cidr, 4, i)]",
            f"  public_subnets   = [for i in range({azs}) : cidrsubnet(local.vpc_cidr, 8, 128 + i)]",
            f"  database_subnets = [for i in range({azs}) : cidrsubnet(local.vpc_cidr, 8, 192 + i)]",
            "",
            "  enable_nat_gateway     = true",
            f"  single_nat_gateway     = {str(not answers.get('nat_gateway_per_az', False)).lower()}",
            f"  enable_flow_log        = {str(answers.get('enable_flow_logs', True)).lower()}",
            "  enable_dns_hostnames   = true",
        ]
    elif sid == "eks":
        lines += [
            f'  cluster_version = "{answers.get("k8s_version", "1.31")}"',
            "  vpc_id          = module.vpc.vpc_id",
            "  subnet_ids      = module.vpc.private_subnets",
            "",
            "  # Private endpoint by default. A public API server is an internet-facing",
            "  # control plane, and turning it on should be a deliberate act.",
            "  cluster_endpoint_public_access  = false",
            "  cluster_endpoint_private_access = true",
            f"  enable_irsa                     = {str(answers.get('irsa', True)).lower()}",
            "",
            "  # Reaching a private endpoint still requires a path to it. Without",
            "  # this rule the cluster is private and unreachable, which people",
            "  # fix by turning public access back on.",
            "  cluster_security_group_additional_rules = {",
            "    api_from_allowed_cidrs = {",
            '      description = "Kubernetes API from the CIDRs you control"',
            '      protocol    = "tcp"',
            "      from_port   = 443",
            "      to_port     = 443",
            '      type        = "ingress"',
            "      cidr_blocks = var.allowed_cidrs",
            "    }",
            "  }",
            "",
            "  eks_managed_node_groups = {",
        ]
        for i in range(answers.get("node_group_count", 1)):
            lines += [
                f"    ng{i + 1} = {{",
                f'      instance_types = ["{answers.get("node_instance_type", "m6i.large")}"]',
                f'      capacity_type  = "{"SPOT" if answers.get("spot") else "ON_DEMAND"}"',
                f"      min_size       = {answers.get('node_min', 2)}",
                f"      max_size       = {answers.get('node_max', 6)}",
                f"      desired_size   = {answers.get('node_min', 2)}",
                "    }",
            ]
        lines.append("  }")
    elif sid == "rds":
        engine = answers.get("engine", "postgres")
        meta = ENGINE_DEFAULTS.get(engine, ENGINE_DEFAULTS["postgres"])
        lines += [
            f'  engine         = "{engine}"',
            f'  engine_version = "{meta["version"]}"',
            f'  instance_class = "{answers.get("instance_class", "db.m6g.large")}"',
            f"  allocated_storage = {answers.get('storage_gb', 50)}",
            f"  port              = {ENGINE_PORTS.get(engine, 5432)}",
            "",
            "  # Required by the parameter group and option group the module",
            "  # creates. Neither has a default, and both must track engine_version.",
            f'  family               = "{meta["family"]}"',
            f'  major_engine_version = "{meta["major_version"]}"',
            "",
            "  # Private subnets only. See guardrail NET002.",
            "  #",
            "  # create_db_subnet_group must be true for subnet_ids to have any",
            "  # effect. The upstream module defaults it to false, and with no",
            "  # subnet group the instance is created in the default VPC, which",
            "  # is the opposite of what the line above claims.",
            "  publicly_accessible    = false",
            "  create_db_subnet_group = true",
            "  subnet_ids             = module.vpc.database_subnets",
            "  vpc_security_group_ids = [aws_security_group.rds.id]",
            "",
            f"  storage_encrypted       = {str(SECURE_DEFAULTS['storage_encrypted']).lower()}",
            f"  deletion_protection     = {str(env == 'prod').lower()}",
            f"  skip_final_snapshot     = {str(env != 'prod').lower()}",
            f"  backup_retention_period = {answers.get('backup_retention_days', 7)}",
            f"  multi_az                = {str(answers.get('multi_az', False)).lower()}",
            "",
            "  # The password is never generated or committed. Supply it at apply",
            "  # time from a secret manager. See variables.tf.",
            "  password = var.db_password",
        ]
    else:
        # Say what this is. The previous comment read like an optional next
        # step, so the block looked finished: it has a source, a version, and a
        # name, and `terraform validate` agrees. Every argument the module needs
        # defaults to null or [] upstream, so nothing objects until apply
        # reaches the AWS API. Tracked in issue #10.
        lines += [
            "  # INCOMPLETE. This block will NOT apply.",
            "  #",
            "  # stackmason does not yet configure this stack. What is above is a",
            "  # module reference and a name. Every required argument is missing,",
            "  # and each one defaults to null or [] upstream, which is why",
            "  # terraform validate reports success on a configuration that",
            "  # cannot create anything.",
            "  #",
            "  # Fill this in against the module documentation linked in",
            "  # ARCHITECTURE.md before applying.",
        ]

    lines.append("}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class Output:
    """One value a generated environment publishes.

    Outputs are the seam between this repository and everything downstream: a
    Helm values file, a deploy job, another configuration reading it through
    `terraform_remote_state`. Without them the generated repo builds
    infrastructure and then declines to say what it built.
    """

    name: str
    value: str
    description: str
    sensitive: bool = False

    def render(self) -> str:
        lines = [f'output "{self.name}" {{']
        lines.append(f"  value       = {self.value}")
        lines.append(f'  description = "{self.description}"')
        if self.sensitive:
            lines.append("  sensitive   = true")
        lines.append("}")
        return "\n".join(lines) + "\n"


# What each stack publishes. An explicit allowlist, never a forwarding of
# everything upstream exposes.
#
# The distinction matters more than it looks. `terraform-aws-modules/rds` also
# exposes `db_instance_master_user_secret`, and a generator that forwarded
# outputs wholesale would be one upstream release away from publishing a
# credential it never meant to. `terraform output` is not privileged: anyone who
# can read the state can read every output, `-json` ignores `sensitive`, and CI
# logs are not private. So the rule is not "mark the secret sensitive", it is
# never create an output that reaches it.
STACK_OUTPUTS: dict[str, tuple[Output, ...]] = {
    "vpc": (
        Output("vpc_id", "module.vpc.vpc_id", "VPC id, for anything that attaches to this network"),
        Output(
            "private_subnets",
            "module.vpc.private_subnets",
            "Private subnet ids. Where workloads belong",
        ),
        Output(
            "public_subnets",
            "module.vpc.public_subnets",
            "Public subnet ids. Load balancers and NAT only",
        ),
        Output(
            "database_subnets",
            "module.vpc.database_subnets",
            "Database subnet ids, used by the RDS subnet group",
        ),
    ),
    "eks": (
        Output(
            "cluster_name", "module.eks.cluster_name", "Cluster name, for aws eks update-kubeconfig"
        ),
        Output("cluster_endpoint", "module.eks.cluster_endpoint", "Kubernetes API endpoint"),
        Output(
            "cluster_certificate_authority_data",
            "module.eks.cluster_certificate_authority_data",
            "Cluster CA certificate. Not a secret, but a large blob that ruins a plan diff",
            sensitive=True,
        ),
    ),
    "rds": (
        Output(
            "db_instance_address",
            "module.rds.db_instance_address",
            "Database hostname. The value an application config needs",
        ),
        Output("db_instance_port", "module.rds.db_instance_port", "Database port"),
    ),
}


def _outputs_tf(stacks: list[str]) -> str:
    """Outputs for the selected stacks, and nothing else.

    A stack that was not selected contributes nothing, so a generated repo
    never references a module it does not have.
    """
    header = (
        "# What this environment publishes.\n"
        "#\n"
        "# Deliberately short. These are the values something downstream actually\n"
        "# consumes, not everything the upstream modules expose.\n"
        "#\n"
        "# There is no password output, and there is no output that reaches one.\n"
        "# `terraform output` is not a privileged operation, `-json` ignores the\n"
        "# sensitive flag, and CI logs are not private. Read the password from\n"
        "# wherever you supplied it, never from here.\n"
    )
    blocks = [o.render() for sid in stacks for o in STACK_OUTPUTS.get(sid, ())]
    if not blocks:
        return header + "\n# No stack in this environment publishes anything yet.\n"
    return header + "\n" + "\n".join(blocks)


def _locals(stacks: list[str], answers: dict) -> str:
    """Values referenced more than once, named once.

    `vpc_cidr` in particular: the subnet layout derives three lists from it, and
    having the base range appear four times as a literal is how a VPC ends up
    with subnets that do not sit inside it.
    """
    if "vpc" not in stacks:
        return ""
    return f'''locals {{
  vpc_cidr = "{answers.get("cidr", "10.0.0.0/16")}"
}}

'''


def _security_groups(stacks: list[str], answers: dict) -> str:
    """Security groups for anything that would otherwise land on the default one.

    An empty `vpc_security_group_ids` does not mean no access. It means the
    VPC's default security group, which permits everything from anything else
    carrying the same group. That is a wide-open lateral path presented as an
    absence of configuration.
    """
    if "rds" not in stacks:
        return ""
    engine = answers.get("engine", "postgres")
    port = ENGINE_PORTS.get(engine, 5432)
    return f'''
resource "aws_security_group" "rds" {{
  name        = "${{var.project}}-${{var.environment}}-rds"
  description = "Database ingress, restricted to var.allowed_cidrs"
  vpc_id      = module.vpc.vpc_id

  ingress {{
    description = "{engine} from the CIDRs you control"
    from_port   = {port}
    to_port     = {port}
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }}

  # No egress block, deliberately. Terraform revokes the default allow-all
  # egress rule when none is declared, and a database has no reason to open
  # connections outbound.

  lifecycle {{
    create_before_destroy = true
  }}
}}
'''


def build_plan(
    root: Path, project: str, stacks: list[str], environments: list[str], answers: dict
) -> Plan:
    """Compute every file that would be written. Writes nothing."""
    resolved = resolve_dependencies(stacks)
    region = str(answers.get("aws_region") or DEFAULT_REGION)
    merged = {**answers, "stacks": resolved, "environments": environments, "aws_region": region}
    plan = Plan(root, resolved, environments, merged, guardrails=evaluate(merged, resolved))

    for env in environments:
        base = f"environments/{env}"
        modules = "\n".join(_module_block(s, answers, env) for s in resolved)
        variables = _variables(resolved, project, env, region)
        plan.files[f"{base}/main.tf"] = (
            f"# {project} :: {env}\n"
            f"# Generated by stackmason. Edit freely; this is your repository now.\n\n"
            'data "aws_availability_zones" "available" {\n'
            '  state = "available"\n}\n\n'
            + _locals(resolved, answers)
            + modules
            + _security_groups(resolved, answers)
        )
        plan.files[f"{base}/versions.tf"] = _versions_tf()
        plan.files[f"{base}/providers.tf"] = _providers_tf(project, env, region)
        plan.files[f"{base}/backend.tf"] = _backend_tf(env, project, region)
        plan.files[f"{base}/outputs.tf"] = align_hcl(_outputs_tf(resolved))
        plan.files[f"{base}/variables.tf"] = _variables_tf(variables)
        plan.files[f"{base}/terraform.tfvars.example"] = _tfvars_example(variables)

    plan.files["README.md"] = _readme(project, resolved, environments, merged)
    plan.files["ARCHITECTURE.md"] = _architecture(project, resolved, environments, merged)
    plan.files["DECISIONS.md"] = _decisions(merged, plan.guardrails)
    plan.files[".gitignore"] = _gitignore()
    plan.files[".github/workflows/terraform.yml"] = _ci()

    # Align every .tf file so the generated repo passes the formatting check it
    # ships with, rather than failing CI on its first commit.
    for rel in list(plan.files):
        if rel.endswith(".tf"):
            plan.files[rel] = align_hcl(plan.files[rel])
    return plan


def _variables(stacks: list[str], project: str, env: str, region: str) -> list[Variable]:
    """Every variable this environment declares.

    Single source of truth for `variables.tf` and `terraform.tfvars.example`.
    """
    out = [
        Variable(
            "aws_region",
            "Region for every resource. Must match the region in backend.tf.",
            "string",
            default=f'"{region}"',
            example=f'"{region}"',
        ),
        Variable(
            "project",
            "Name prefix for every resource.",
            "string",
            default=f'"{project}"',
            example=f'"{project}"',
        ),
        Variable(
            "environment",
            "Environment name, used in resource names and tags.",
            "string",
            default=f'"{env}"',
            example=f'"{env}"',
        ),
    ]

    if CIDR_CONSUMERS & set(stacks):
        out.append(
            Variable(
                "allowed_cidrs",
                "CIDRs permitted to reach private services. Never 0.0.0.0/0.",
                "list(string)",
                validation="""validation {
  condition     = !contains(var.allowed_cidrs, "0.0.0.0/0")
  error_message = "A private service must not be reachable from the internet."
}""",
                # A documentation-only range from RFC 5737. Deliberately not a
                # working value: if it is left unedited, access fails closed
                # instead of quietly admitting something.
                example='["203.0.113.0/24"]',
            )
        )

    if "rds" in stacks:
        out.append(
            Variable(
                "db_password",
                "Database master password, supplied at apply time from a secret manager.",
                "string",
                sensitive=True,
                example_note=(
                    "db_password is deliberately absent. A default is what ends up "
                    "committed.\nSupply it at apply time:\n"
                    "  TF_VAR_db_password=$(aws secretsmanager get-secret-value \\\n"
                    f"    --secret-id {project}/db --query SecretString --output text)"
                ),
            )
        )
    return out


def _variables_tf(variables: list[Variable]) -> str:
    return "\n".join(v.render() for v in variables)


def _tfvars_example(variables: list[Variable]) -> str:
    """The example file, rendered so that copying it is enough to plan.

    Enforced rather than trusted: a required variable with neither an example
    value nor a note explaining where its value comes from raises here, at
    generation time, rather than surfacing as an interactive prompt in
    somebody's CI job.
    """
    lines = [
        "# Copy to terraform.tfvars and edit. terraform.tfvars is gitignored.",
        "# Every variable without a default appears below, so a copy of this file",
        "# is enough to run terraform plan without being prompted.",
        "",
    ]
    for v in variables:
        if v.example is not None:
            lines.append(f"# {v.description}")
            lines.append(f"{v.name} = {v.example}")
            lines.append("")
        elif v.required:
            if not v.example_note:
                raise RuntimeError(
                    f"variable {v.name!r} is required but has no example and no note; "
                    "terraform plan would prompt for it"
                )
            lines.extend(f"# {ln}" if ln else "#" for ln in v.example_note.split("\n"))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _gitignore() -> str:
    return """# State holds every secret the plan touched, in plaintext.
*.tfstate
*.tfstate.*
.terraform/

# Real values. Only .example is tracked.
*.tfvars
!*.tfvars.example

*.tfplan
crash.log
.env
*.pem
"""


def _ci() -> str:
    return """name: Terraform

on: [push, pull_request]

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "1.9.8"

      - run: terraform fmt -check -recursive -diff

      - name: validate every environment
        run: |
          set -e
          for d in environments/*/; do
            terraform -chdir="$d" init -backend=false -input=false
            terraform -chdir="$d" validate
          done

      - name: no credentials, no state
        run: |
          if grep -rInE '(password|secret|token)[[:space:]]*=[[:space:]]*"[^"$][^"]{5,}"' \\
               --include='*.tf' --include='*.tfvars' .; then
            echo "::error::hardcoded credential"; exit 1
          fi
          if git ls-files | grep -E '\\.tfstate|\\.tfvars$' | grep -v '\\.example$'; then
            echo "::error::state or tfvars is tracked"; exit 1
          fi
          echo ok
"""


def _readme(project: str, stacks: list[str], envs: list[str], answers: dict) -> str:
    rows = "\n".join(
        f"| `{s}` | {BY_ID[s].name} | [`{BY_ID[s].module_source}`]"
        f"(https://registry.terraform.io/modules/{BY_ID[s].module_source}) `{BY_ID[s].module_version}` |"
        for s in stacks
    )
    cost = monthly_floor(stacks, len(envs))
    env_list = ", ".join(f"`{e}`" for e in envs)

    # Built outside the f-string: it contains backslashes, which an f-string
    # expression cannot hold on Python 3.10.
    if "rds" in stacks:
        secrets = (
            "The database password is a `sensitive` variable with no default, "
            "supplied at apply time:\n\n"
            "```bash\n"
            "export TF_VAR_db_password=$(aws secretsmanager get-secret-value \\\n"
            f"  --secret-id {project}/db --query SecretString --output text)\n"
            "terraform apply\n"
            "```\n"
        )
    else:
        secrets = "No stack in this repository requires a credential.\n"

    readme_region = str(answers.get("aws_region") or DEFAULT_REGION)
    consume_lines = [
        "```bash",
        "terraform output            # everything this environment publishes",
    ]
    if "eks" in stacks:
        consume_lines += [
            "",
            "# Point kubectl at the cluster this repository built:",
            "aws eks update-kubeconfig \\",
            "  --name $(terraform output -raw cluster_name) \\",
            f"  --region {readme_region}",
        ]
    if "rds" in stacks:
        consume_lines += [
            "",
            "# Where the application connects. Never the password: that is not",
            "# an output, deliberately. Read it from wherever you supplied it.",
            "terraform output -raw db_instance_address",
            "terraform output -raw db_instance_port",
        ]
    consume_lines.append("```")
    consume = "\n".join(consume_lines)

    return f"""# {project}

Infrastructure for **{project}**, generated by
[stackmason](https://github.com/ehtishammubarik/stackmason).

This is your repository now. Edit it freely; nothing here regenerates itself.

## What is in it

| Stack | Purpose | Module |
|---|---|---|
{rows}

Environments: {env_list}

Rough fixed cost before compute and storage: **~${cost:.0f}/month**, us-east-1
on-demand. An order of magnitude for steering decisions, not a quote.

## First run

```bash
cd environments/{envs[0]}
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform plan
```

`terraform.tfvars` is gitignored. It stays that way.

## Consuming what it built

`outputs.tf` publishes the values something downstream needs. They are readable
after apply, and through `terraform_remote_state` from another configuration.

{consume}

There is no password output and no output that reaches one. `terraform output`
is not a privileged operation, `-json` ignores the `sensitive` flag, and CI logs
are not private.

## Secrets

No credential is committed anywhere in this repository, and none should be.

{secrets}
## Before you apply

Read the plan. The diff is not the change; the plan is the change. A one-line
edit can replace a database.

```bash
terraform plan -out=tfplan
terraform show tfplan | grep -E 'will be (destroyed|replaced)'
```

Anything in that output needs an answer before you continue.

## Why it looks like this

See [ARCHITECTURE.md](ARCHITECTURE.md). Deviations from the safe defaults are
recorded in [DECISIONS.md](DECISIONS.md).
"""


def _address_plan(stacks: list[str], answers: dict) -> str:
    """The subnet layout, written down.

    Whoever peers this VPC later needs to know what was reserved, and reading
    it back out of `cidrsubnet` calls is not a reasonable thing to ask of them.
    """
    if "vpc" not in stacks:
        return ""
    cidr = answers.get("cidr", "10.0.0.0/16")
    azs = answers.get("az_count", 2)
    return f"""
## Address plan

Base range `{cidr}`, divided across {azs} availability zone(s):

| Tier | Size | Index | Holds |
|---|---|---|---|
| private | `/20` per AZ | from 0 | Nodes and pods. The largest blocks, because pod density is what exhausts a VPC |
| public | `/24` per AZ | from 128 | NAT gateways and load balancers only |
| database | `/24` per AZ | from 192 | Subnet group members. Almost nothing |

The gap between the tiers is deliberate: private can grow to six AZs without
reaching the public range.

Ranges are computed with `cidrsubnet` from `local.vpc_cidr`, so changing the
base range moves all three together. Below roughly a `/20` base the split does
not fit and `cidrsubnet` fails at plan time, which is the correct outcome.
"""


def _architecture(project: str, stacks: list[str], envs: list[str], answers: dict) -> str:
    per_stack = "\n\n".join(
        f"### {BY_ID[s].name}\n\n{BY_ID[s].summary}\n\n"
        f"Module: `{BY_ID[s].module_source}` `{BY_ID[s].module_version}`, referenced by version "
        f"rather than vendored, so upstream security fixes arrive without a copy to maintain."
        + (f"\n\n{BY_ID[s].notes}" if BY_ID[s].notes else "")
        for s in stacks
    )
    layout = "\n".join(f"  {e}/    main.tf  versions.tf  backend.tf  variables.tf" for e in envs)
    return f"""# Architecture

## Layout

```
environments/
{layout}
```

One directory per environment, each with its own state. Environments do not
share state, so a mistake in dev cannot destroy prod, and `terraform apply` in
one cannot be run against the other by accident.
{_address_plan(stacks, answers)}
## Stacks

{per_stack}

## Decisions that are not obvious

**Remote state with locking, always.** State contains every secret the plan
touched in plaintext. Local state cannot be shared safely and is one laptop
away from unrecoverable.

**Providers are pinned.** An unpinned provider means a plan run today and a
plan run next month are not the same plan, which makes reviewing a plan
meaningless.

**The EKS API server is private by default.** A public endpoint is an
internet-facing control plane; enabling it should be a deliberate act with a
reason.

**Databases live in private subnets** and are never `publicly_accessible`. The
generator will not emit a configuration that opens a data port to `0.0.0.0/0`.

**Production gets deletion protection and final snapshots.** Non-production
does not, because the cost of a stuck destroy in dev exceeds the value of the
snapshot.

## What this does not do

- No secret management. It emits `sensitive` variables and expects you to
  supply values from a secret manager.
- No cost enforcement. It estimates and warns; it cannot stop you.
- No compliance guarantee. The defaults are good practice, not an audit.
"""


def _decisions(answers: dict, report: GuardrailReport | None) -> str:
    lines = [
        "# Decisions",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} by stackmason.",
        "",
        "Every value chosen during generation, so the reasoning survives the",
        "person who ran it.",
        "",
        "| Setting | Value |",
        "|---|---|",
    ]
    for k, v in sorted(answers.items()):
        if k in {"stacks", "environments"}:
            v = ", ".join(v)
        lines.append(f"| `{k}` | {v} |")

    if report and report.findings:
        lines += ["", "## Guardrail findings at generation time", ""]
        lines += [f"- {f.code}: {f.message}" for f in report.findings]
    return "\n".join(lines) + "\n"


def write(plan: Plan, *, force: bool = False) -> list[Path]:
    """Write the plan to disk. Refuses to overwrite unless forced."""
    if plan.blocked:
        raise RuntimeError("generation blocked by guardrails:\n" + plan.guardrails.render())
    written = []
    for rel, content in sorted(plan.files.items()):
        path = plan.root / rel
        if path.exists() and not force:
            raise FileExistsError(f"{path} exists; pass force=True to overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
