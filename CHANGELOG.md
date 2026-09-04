# Changelog

## [Unreleased]

## [0.1.1] - 2026-09-04

Eight changes since `v0.1.0`. Three of them are the reason this release exists
at all: a generated repository now applies, six of the nine advertised stacks
admit they are stubs, and a contributor arriving from a `good first issue` label
is no longer handed nothing.

### Added

- **`outputs.tf` in every generated environment** ([#9]). A generated repo built
  infrastructure and then declined to say what it built: no cluster endpoint, no
  database address, no subnet ids, so the only way to find them was
  `terraform state show`. That is a workaround, not a workflow, and it does not
  survive being put in a pipeline. Outputs are an explicit per-stack allowlist,
  never a forwarding of everything the upstream modules expose, and **there is no
  password output and no output that reaches one**: `terraform output` is not a
  privileged operation, `-json` ignores `sensitive`, and CI logs are not private.
  A test fails on any output whose name or value expression is password-shaped.
  The generated README gains a "Consuming what it built" section with the
  `aws eks update-kubeconfig` line.
- **Stub stacks now say so, everywhere someone might look** ([#10]). Six of the
  nine advertised stacks emit a module reference and a name and nothing else.
  `terraform validate` accepts that, because every argument they omit defaults
  to null or `[]` upstream, so the generated CI passed and apply failed at the
  AWS API. Nothing said which three were real. Now `stackmason stacks` marks
  them `[stub]`, selecting one raises a `GEN001` warning in the plan output and
  in `DECISIONS.md`, and the emitted block opens with `INCOMPLETE. This block
  will NOT apply.` instead of a comment that read like an optional next step.
  A `configured` flag on each registry entry records which is which, and the
  test suite asserts it against the generated output, so it cannot be set on a
  stack that has not actually been implemented. The six stacks are still stubs;
  this change is about not pretending otherwise.

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

- **Production guardrails that warn rather than block** ([#5], [#6]). The
  environment name asserts an intent the generator can already check, and it was
  saying nothing. A single-AZ database, a NAT gateway shared across AZs, a node
  group minimum below two, and spot instances now each raise a finding in any
  environment named `prod`, `production`, `prd`, or `live`. WARN and not BLOCK
  deliberately: plenty of people have exactly one environment, call it prod, and
  are right to run it cheaply. Blocking that would be wrong; staying silent when
  the mismatch is visible is also wrong. Backup retention is deliberately not
  checked here even though it fits the theme, because `check_data_protection`
  already reports it as `DAT002`, and two findings for one problem trains people
  to skim the report. A test asserts no duplicate codes, and another asserts the
  remedy text is substantive, because "consider hardening production" is advice
  nobody can act on.

### Documentation

- **A contributing guide** ([#18]). The repo labelled issues `good first issue`
  and `help wanted` and then gave arrivals nothing: no setup beyond the README
  install line, no review expectations, and no statement of the one trap that has
  caused every real bug here. The section that matters most is the
  `terraform validate` trap, which is satisfied by empty lists and null
  arguments, so it passed on every CI run while [#7], [#11], and [#12] each made
  generated repositories impossible to apply. Three rules follow from that: test
  the emitted HCL rather than the Python emitting it, run a real plan when you
  change output, and write the test so you have watched it fail. It also states
  one issue per PR, claiming an issue before building, and that a
  first-contribution workflow run is held, so "no checks reported" is not a pass.
- **A roadmap and a vision, with the stack count enforced by CI** ([#28]). There
  were 13 open issues and two milestones and nothing saying which mattered. The
  advertised stack count in `ROADMAP.md` is now asserted against the registry by a
  test, which fails with the message "update the roadmap in the same PR that
  changes the code", so the roadmap cannot drift from the catalogue the tool
  ships. A second test requires every unconfigured stack to link an issue, so a
  stub is never a dead end for a contributor who picked it.
- **The stale test figures are gone rather than corrected** ([#17], [#20]). The
  README claimed 57 tests and 88% coverage; the suite was 96 and coverage 90%,
  so both were false in the file people use to judge the project. Rewriting the
  numbers would only reset the clock, because they drifted for want of anything
  keeping them true. The count is gone entirely and the coverage figure is now a
  CI floor of 88% across Python 3.10, 3.11, and 3.12. Verified the floor is real
  and not decorative: `pytest` exits 1 at `--cov-fail-under=95` and 0 at 88.

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

The suite now asserts properties of the emitted Terraform rather than of the
Python that emits it: every consumed subnet list is defined, every required
variable is reachable without a prompt, the database security group port matches
the engine, and subnet ranges do not overlap at any supported AZ count.

No test count is quoted here, and the previous entry's "twelve" has been removed.
It was [#20] recurring in the one file that change did not cover: a number in a
released entry is a dated snapshot and may stand, but a number in `Unreleased` is
not yet history and was simply wrong. What is quoted instead is the gate: CI enforces a coverage floor
of 88% on Python 3.10, 3.11, and 3.12, and the release workflow re-runs the suite
against the installed wheel, then asserts on the published artifact itself that
the guardrails still refuse an `0.0.0.0/0` database and that the generated output
contains no literal credential. A generator whose guardrails silently stopped
working would be worse than no generator, so that is checked on the thing users
install rather than on the tree it was built from.

There is no `terraform plan` in the release workflow. The `eks` + `rds` plan was
run by hand against a real AWS account during 0.1.1 development, and a figure
from a manual run is exactly the kind of number this entry declines to quote.
Automating it is [#31].

[#5]: https://github.com/ehtishammubarik/stackmason/issues/5
[#6]: https://github.com/ehtishammubarik/stackmason/issues/6
[#7]: https://github.com/ehtishammubarik/stackmason/issues/7
[#8]: https://github.com/ehtishammubarik/stackmason/issues/8
[#9]: https://github.com/ehtishammubarik/stackmason/issues/9
[#10]: https://github.com/ehtishammubarik/stackmason/issues/10
[#11]: https://github.com/ehtishammubarik/stackmason/issues/11
[#12]: https://github.com/ehtishammubarik/stackmason/issues/12
[#17]: https://github.com/ehtishammubarik/stackmason/issues/17
[#18]: https://github.com/ehtishammubarik/stackmason/issues/18
[#20]: https://github.com/ehtishammubarik/stackmason/issues/20
[#28]: https://github.com/ehtishammubarik/stackmason/issues/28
[#31]: https://github.com/ehtishammubarik/stackmason/issues/31

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
[0.1.1]: https://github.com/ehtishammubarik/stackmason/releases/tag/v0.1.1
