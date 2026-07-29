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


def _backend_tf(env: str, project: str) -> str:
    return f'''# Remote state with locking.
#
# State holds every secret the plan touched, in plaintext. Local state is one
# laptop away from unrecoverable and cannot be shared safely, so it is not an
# option this generator offers for a real environment.
#
# Create the bucket and lock table once, before the first init:
#   aws s3api create-bucket --bucket {project}-tfstate --region us-east-1
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
    region         = "us-east-1"
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
        lines += [
            f'  cidr = "{answers.get("cidr", "10.0.0.0/16")}"',
            f"  azs  = slice(data.aws_availability_zones.available.names, 0, {answers.get('az_count', 2)})",
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
        lines += [
            f'  engine         = "{answers.get("engine", "postgres")}"',
            f'  instance_class = "{answers.get("instance_class", "db.m6g.large")}"',
            f"  allocated_storage = {answers.get('storage_gb', 50)}",
            "",
            "  # Private subnets only. See guardrail NET002.",
            "  publicly_accessible = false",
            "  subnet_ids          = module.vpc.database_subnets",
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
        lines.append("  # Configure per the module documentation linked in ARCHITECTURE.md")

    lines.append("}")
    return "\n".join(lines) + "\n"


def build_plan(
    root: Path, project: str, stacks: list[str], environments: list[str], answers: dict
) -> Plan:
    """Compute every file that would be written. Writes nothing."""
    resolved = resolve_dependencies(stacks)
    merged = {**answers, "stacks": resolved, "environments": environments}
    plan = Plan(root, resolved, environments, merged, guardrails=evaluate(merged))

    for env in environments:
        base = f"environments/{env}"
        modules = "\n".join(_module_block(s, answers, env) for s in resolved)
        plan.files[f"{base}/main.tf"] = (
            f"# {project} :: {env}\n"
            f"# Generated by stackwright. Edit freely; this is your repository now.\n\n"
            'data "aws_availability_zones" "available" {\n'
            '  state = "available"\n}\n\n' + modules
        )
        plan.files[f"{base}/versions.tf"] = _versions_tf()
        plan.files[f"{base}/backend.tf"] = _backend_tf(env, project)
        plan.files[f"{base}/variables.tf"] = _variables_tf(resolved, project, env)
        plan.files[f"{base}/terraform.tfvars.example"] = (
            f'project     = "{project}"\n'
            f'environment = "{env}"\n'
            "# db_password is deliberately absent. Supply it from a secret manager:\n"
            "#   TF_VAR_db_password=$(aws secretsmanager get-secret-value ...) terraform apply\n"
        )

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


def _variables_tf(stacks: list[str], project: str, env: str) -> str:
    out = f'''variable "project" {{
  description = "Name prefix for every resource."
  type        = string
  default     = "{project}"
}}

variable "environment" {{
  description = "Environment name, used in resource names and tags."
  type        = string
  default     = "{env}"
}}

variable "allowed_cidrs" {{
  description = "CIDRs permitted to reach private services. Never 0.0.0.0/0."
  type        = list(string)

  validation {{
    condition     = !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "A private service must not be reachable from the internet."
  }}
}}
'''
    if "rds" in stacks:
        out += """
variable "db_password" {
  description = "Database master password, supplied at apply time from a secret manager."
  type        = string
  sensitive   = true
  # No default. A default is what ends up committed.
}
"""
    return out


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

    return f"""# {project}

Infrastructure for **{project}**, generated by
[stackwright](https://github.com/ehtishammubarik/stackwright).

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
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} by stackwright.",
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
