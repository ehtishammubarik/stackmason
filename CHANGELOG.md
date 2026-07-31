# Changelog

## [Unreleased]

### Added

- **A `provider "aws"` block in every environment, with an explicit region**
  ([#8]). There was none. The provider took its region from `AWS_REGION`, an
  active profile, or nothing, while `backend.tf` hardcoded `us-east-1` and
  nothing checked that the two agreed. `AWS_REGION=eu-west-1 terraform apply`
  wrote state to Virginia and built the VPC in Ireland, successfully and
  silently, until the next person ran it from a different shell and got a plan
  that wanted to destroy everything.
- `default_tags` carrying `Project`, `Environment`, `ManagedBy`, and
  `Repository`. Untagged infrastructure is the most common reason a cloud bill
  cannot be explained, and the generator already knew every one of these values:
  it puts them in each resource name.
- `--region` on `new` and `plan`, and `aws_region` in an answers file. Both set
  the provider and the state bucket together, since they are generated from one
  value and a test asserts they match.

### Fixed

Four defects that shared one cause: the generated Terraform was checked with
`terraform validate`, which cannot see an empty list or a null argument. All
four were type-correct and semantically wrong, and the first real
`terraform plan` against an AWS account found them in sequence.

- **The generated VPC created no subnets** ([#12]). `private_subnets`,
  `public_subnets`, and `database_subnets` were never set, all three default to
  `[]` upstream, and both EKS and RDS consumed them. Every repository generated
  before this failed at apply. Subnet ranges are now computed from the VPC CIDR
  with a documented address plan.
- **The database was created in the default VPC** ([#11]). `subnet_ids` is
  inert unless `create_db_subnet_group` is true, which defaults to false, so the
  comment claiming private subnets was not true of the configuration beneath it.
- **`allowed_cidrs` was required, unused, and absent from the example tfvars**
  ([#7]). `terraform plan` stopped to prompt for it, and its `0.0.0.0/0`
  validation block guarded nothing. It is now emitted only when EKS or RDS is
  selected, and both consume it: EKS for API server access, RDS through a
  dedicated security group instead of the VPC default.
- **RDS omitted `family` and `major_engine_version`**, which its parameter group
  and option group require and which have no upstream defaults.

### Changed

- `variables.tf` and `terraform.tfvars.example` are rendered from one list, so a
  variable cannot exist in one and be missing from the other. Generation now
  fails if a required variable has neither an example value nor a note saying
  where the value comes from. Secrets get the note, never a value.
- Generated `ARCHITECTURE.md` documents the address plan.

### Testing

Twelve tests asserting properties of the emitted Terraform rather than of the
Python that emits it: every consumed subnet list is defined, every required
variable is reachable without a prompt, the database security group port matches
the engine, subnet ranges do not overlap at any supported AZ count.

A generated `eks` + `rds` repository now plans cleanly: **64 resources to add,
0 to change, 0 to destroy**.

[#7]: https://github.com/ehtishammubarik/stackmason/issues/7
[#8]: https://github.com/ehtishammubarik/stackmason/issues/8
[#11]: https://github.com/ehtishammubarik/stackmason/issues/11
[#12]: https://github.com/ehtishammubarik/stackmason/issues/12

## [0.1.0] - 2026-07-29

Initial release.

### Added

- Interview-driven generation of a Terraform repository: one directory and one
  state file per environment, providers pinned, remote state configured, CI,
  and an architecture document.
- Nine stacks: VPC, EKS, RDS, MSK, Redshift, ElastiCache, S3, ALB,
  observability. Dependencies resolve transitively.
- Guardrails that **refuse to generate** a data port open to `0.0.0.0/0`, a
  publicly accessible database, a bastion with no source restriction, or
  `skip_final_snapshot` in production. Local state and per-AZ NAT gateways warn
  and are costed.
- `suggest` on every sizing question, returning a recommendation with its
  reasoning, written to `DECISIONS.md` so the choice outlives whoever ran it.
- `stackmason plan` for a dry run that writes nothing.

### Known limitations

Stated because a limitation you find yourself is worse than one you were told
about.

- AWS only. The registry is structured for other providers; none are
  implemented.
- Nine stacks is a small catalogue.
- No secret management. It emits `sensitive` variables and expects values from
  yours.
- No compliance guarantee. The defaults are good practice, not an audit.
- Generated repositories are a first draft. Reading the plan is still your job.

[0.1.0]: https://github.com/ehtishammubarik/stackmason/releases/tag/v0.1.0
