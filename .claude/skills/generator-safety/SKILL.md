---
name: generator-safety
description: Rules for changing what stackwright emits. Use before touching guardrails, templates, the stack registry, or anything affecting generated output. The failure mode here is not a crash, it is insecure infrastructure at other people's companies.
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

# Generator safety

Normal code hurts its user when it breaks. A scaffolding tool hurts everyone
who ran it, months later, in an incident they cannot trace back to you.

That asymmetry sets the standard: **be more conservative than you would be in
application code**, and treat guardrail changes the way you would treat changes
to an authentication path.

## Before loosening any guardrail

Answer all four, in the PR:

1. What real configuration does this currently block that should be allowed?
2. What does the person who hits this block do today instead?
3. If someone generates the newly-allowed configuration and is compromised
   through it, was it reasonable for them to assume this tool would have
   stopped them?
4. Can it be a warning rather than a removal?

"It is annoying" is not an answer to any of these. The block is the product.

## Adding a template

Every new template output must be checked against three things:

- **Does a guardrail cover the dangerous configurations of it?** If a template
  can emit something unsafe, a guardrail must catch it. Templates and
  guardrails ship together or not at all.
- **Is it `terraform fmt` clean?** `align_hcl` handles alignment, but only for
  simple assignments. Nested blocks and heredocs need checking by generating
  and running `terraform fmt -check`.
- **Does it `terraform validate` against the real module?** Module APIs live on
  the registry. `name` versus `cluster_name` fails at `init` and nowhere else,
  and no unit test will find it.

## Adding a stack

The `name_attribute` field exists because these are not `name`:

| Stack | Attribute |
|---|---|
| eks | `cluster_name` |
| rds | `identifier` |
| redshift | `cluster_identifier` |
| elasticache | `replication_group_id` |
| s3 | `bucket` |

Check the module's actual variables before guessing. Then add the stack to the
CI generation matrix so it is validated against real modules on every push.

## Never

- **Emit a credential.** Not a placeholder that looks real, not a generated
  one, not base64. Someone will ship it.
- **Emit a default for a secret variable.** A default is what ends up
  committed.
- **Emit `0.0.0.0/0` on a data port**, whatever the user asked for.
- **Widen a default quietly.** Any change to `SECURE_DEFAULTS` is a breaking
  change for everyone who regenerates, and belongs in the changelog under
  Changed with the reasoning.

## Suggestions carry reasoning

A recommendation without a reason is an assertion, and the user cannot evaluate
it. Every entry in `SUGGESTIONS` explains the trade, and the reason travels
into `DECISIONS.md` so the choice is explicable after the person who made it
has moved on.

If you cannot articulate why a value is right, it is not a suggestion. Leave it
to the user.
