# stackwright

Interview-driven Terraform repositories, secure by default.

## The rule that governs every change

**A generator multiplies whatever it emits.** One insecure default becomes a
hundred insecure deployments, run by people who reasonably assumed the tool
knew better.

So the guardrails are not advisory, and loosening one is never a small change.
`guardrails.py` is the most important file here; treat a change to it the way
you would treat a change to an authentication path.

## Layout

| Path | Holds |
|---|---|
| `stackwright/guardrails.py` | What the tool refuses to emit, and why. Load-bearing |
| `stackwright/stacks/registry.py` | The catalogue: modules, versions, questions |
| `stackwright/interview.py` | The conversation. Transport-agnostic on purpose |
| `stackwright/generate.py` | Answers to files, plus HCL alignment |
| `stackwright/cli.py` | `new`, `plan`, `stacks` |

## Non-negotiables

1. **Zero runtime dependencies.** This runs on a laptop, in CI, and in a
   locked-down build box. Optional extras only, guarded at the import site.
2. **Never emit a credential.** Not a placeholder, not a generated one, not
   base64. Secrets are `sensitive` variables with no default. There is a test
   asserting this and it must never be relaxed.
3. **Never emit something the guardrails would block.** If a template can
   produce it, a guardrail must catch it.
4. **Generated Terraform must be `fmt` clean.** The generated repo ships CI
   that checks formatting. Unformatted output means every generated repo fails
   its own CI on the first commit.
5. **Generated Terraform must `validate` against the real modules.** Module
   APIs live on the registry, not in our schema. `name` versus `cluster_name`
   is the class of bug that only `terraform init` finds, which is why CI does
   exactly that.
6. **Every question that advertises a recommendation must accept `suggest`.**
   Offering advice the parser rejects is worse than offering none.
7. **Modules are referenced by version, never vendored.** Copying upstream
   means inheriting their maintenance and freezing their security fixes.

## Adding a stack

1. Add a `Stack` to `registry.py`: pinned `module_version`, correct
   `name_attribute`, questions with `recommend` where sizing is involved.
2. **Check `name_attribute` against the module's actual API.** It is not `name`
   for EKS, RDS, Redshift, ElastiCache, or S3.
3. Add a branch to `_module_block` in `generate.py`.
4. Add the stack to the CI generation matrix, so it is validated against real
   modules.
5. Add a guardrail if it can be configured dangerously.

## Before you commit

```bash
pytest && ruff check stackwright tests && ruff format --check stackwright tests
```

Then generate something and run real terraform against it. The test suite
cannot see a module API change; only `terraform init` can.

## Workflow

`skills/dev-pipeline` in the workspace harness. Terraform changes additionally
go to the `terraform-reviewer` subagent, which reads the plan rather than the
diff.
