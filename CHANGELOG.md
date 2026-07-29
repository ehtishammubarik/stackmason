# Changelog

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
