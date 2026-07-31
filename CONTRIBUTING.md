# Contributing to stackmason

This tool writes infrastructure that other people apply to real cloud accounts.
A bad default here does not produce one bad deployment, it produces every
deployment generated after it, run by people who reasonably assumed the
generator knew better. The rules below exist because of that, not out of
ceremony.

## Before you start

**Open an issue first** for anything beyond a typo or an obvious bug. A
paragraph agreeing on the approach is cheaper than a review of the wrong
implementation. Issues labelled `good first issue` are pre-scoped.

**Say on the issue that you are taking it, before you write code.** One comment
is enough. This costs you ten seconds and it is the only thing standing between
you and building something someone else finished yesterday. If an issue already
has a recent claim, pick another or ask the claimer whether they want help.

If you claim something and then drop it, say so. Nobody minds, and it frees the
issue for the next person.

**One issue per pull request.** Exactly one `Closes #N`, or `Refs #N` if you are
deliberately doing part of it. A PR closing several issues cannot be reviewed,
reverted, or released per issue: reverting one fix drags the others out with it,
the changelog cannot attribute a change to a ticket, and a reviewer has to hold
several unrelated arguments at once, which is how a bad change gets waved
through alongside two good ones.

Finding a second defect while working on the first is normal and welcome. File
it as its own issue, finish what you claimed, then open the next PR. **Do not
widen the branch in flight**, even when the two share a root cause. A common
cause is an argument for a common explanation, not a common commit.

## Setup

```bash
git clone https://github.com/ehtishammubarik/stackmason
cd stackmason
pip install -e ".[dev]"
pytest
```

No dependencies, no network, no cloud credentials required.

## The trap that has caused every real bug in this repo

**`terraform validate` is satisfied by an empty list and a null argument.**

It checks that your HCL is well formed and type correct. It does not check that
it means anything. Issues #7, #11, and #12 all passed `validate` on every run of
CI and all three made the generated repository impossible to apply:

- The VPC set no subnet lists, so `private_subnets` and `database_subnets` were
  both `[]`, and EKS and RDS both consumed them.
- `create_db_subnet_group` was left at its `false` default, making `subnet_ids`
  inert and putting the database in the default VPC, underneath a comment
  claiming otherwise.
- A required variable was never read and was absent from the example tfvars, so
  `plan` stopped and waited for a human.

All three were found by running a real `terraform plan` against an account, one
behind the other. None of them could have been found by `validate`.

So:

1. **Test the emitted Terraform, not the Python that emits it.** An assertion
   that `generate()` put a string in a file proves nothing. Assert the property
   that would have caught the bug: every subnet list consumed is also defined,
   every required variable is reachable without a prompt, subnet ranges do not
   overlap at any supported AZ count.
2. **If you change what gets emitted, run a real `plan`.** State the resource
   count in your PR. CI cannot do this for you: it has no credentials, which is
   exactly why these bugs survived.
3. **Write the test so you have seen it fail.** Break the fix, watch the test go
   red, put the fix back. A test never observed failing is not yet a test.

## Generated output must be `terraform fmt` clean

A generated repository ships with CI that runs `terraform fmt -check`. If the
generator emits misaligned HCL, every user's first commit fails their own
pipeline. `align_hcl` in the generator exists for this. Use it rather than
hand-aligning `=` in a string literal, which drifts the moment a longer key is
added next to it.

## What the guardrails are for

The refusals are the product. `stackmason` will not generate a data port open to
`0.0.0.0/0`, a publicly accessible database, a bastion with no source
restriction, or `skip_final_snapshot` in production.

**Do not add a flag that turns a refusal into a warning.** If a refusal is
wrong, the argument to make is that it is wrong, on the issue, before the code.
An escape hatch on a security default is the same as not having the default,
because the hatch is what ends up in the example someone copies.

Never emit a credential. Not a placeholder, not a generated one, not base64.
Secrets are `sensitive` variables with no default, because a default is what
gets committed.

## Tests

```bash
pytest                              # the suite
pytest --cov=stackmason             # with coverage
ruff check stackmason tests         # lint, as CI runs it
ruff format --check stackmason tests
```

Test behaviour, not implementation. A test asserting "the file contains this
exact string" breaks when a neighbouring argument changes alignment, which is
noise, not a regression. Assert the property.

## Commit messages

```
type(scope): imperative summary under 72 chars

Why, not what. The diff says what.

Closes #123
```

Types: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci`,
`chore`. One logical change per commit. If the summary needs "and", split it.

If you deliberately left part of the issue undone, say so in the commit and file
the remainder as an issue. A boundary stated is a boundary; a boundary implied
is a surprise for the next reader.

## A note on CI

If this is your first contribution here, GitHub holds your workflow runs until a
maintainer approves them. Until that happens the checks show as **"no checks
reported"**, which looks like a pass and is not one. Ping the PR if it sits
unapproved, and do not treat a local test run as a substitute for the matrix.

CI runs `lint`, `test` on Python 3.10, 3.11, and 3.12, and
`generated-terraform-is-valid`, which generates a repository with every stack
and runs `terraform validate` against the real upstream modules. That last one
catches syntax and types. Re-read the trap section above for what it misses.

## Security

Never commit credentials. Report vulnerabilities to
[contact@eprecisio.com](mailto:contact@eprecisio.com) rather than in a public
issue.

## If this is useful to you

Star the repo. It is the main way anyone else finds this, and a project that
looks unused gets treated as unmaintained regardless of the state of its tests.

Entirely optional, and it has nothing to do with whether your PR gets merged.
Code is reviewed on the code.

## Questions

Open a discussion, or email [contact@eprecisio.com](mailto:contact@eprecisio.com).
