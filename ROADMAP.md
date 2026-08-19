# Roadmap

Honest about status: `stackmason` is alpha. **Three of the nine advertised
stacks are configured.** The other six emit a module reference and a name, and
say so loudly, but they do not apply. Treat version numbers accordingly.

Each section maps to a GitHub milestone, so this file and the issue tracker
cannot drift apart without one of them being visibly wrong.

- [**0.2 — every advertised stack actually applies**](https://github.com/ehtishammubarik/stackmason/milestone/1)
- [**0.3 — beyond one region, one cloud, one run**](https://github.com/ehtishammubarik/stackmason/milestone/2)

## Vision

**The first ninety minutes of a new infrastructure repository, without the
mistakes that take six months to surface.**

Not a platform. Not a control plane. A generator that writes a first draft you
own outright and can delete this tool afterwards.

Four commitments follow, and the first is the one everything else serves.

**The defaults are the product, and they are not negotiable silently.** A
scaffolding tool multiplies whatever it emits. One insecure default becomes a
hundred insecure deployments, run by people who reasonably assumed the generator
knew better. So `stackmason` **refuses** to emit a data port open to
`0.0.0.0/0`, a publicly accessible database, a bastion with no source
restriction, or `skip_final_snapshot` in production.

**There will be no flag that turns a refusal into a warning.** This is the most
common request a scaffolding tool gets and the answer is permanently no. An
escape hatch on a security default is the same as not having the default,
because the hatch is what ends up in the example someone copies. If a refusal is
wrong, the argument to make is that it is wrong, on the issue, and then it stops
being a refusal for everyone.

**It never emits a credential.** Not a placeholder, not a generated one, not
base64. Secrets are `sensitive` variables with no default, because a default is
what gets committed.

**Every choice is explicable six months later.** Sizing questions accept
`suggest` and return a recommendation with its reasoning attached, written to
`DECISIONS.md`. The alternative is a user picking at random and blaming the
tool.

**Where it stops.** This writes the first draft. Reading the plan is still your
job, and the generated README says so. It is not a compliance guarantee, not a
secret manager, and not a drift detector. If you want a full platform, use
Terragrunt, Atmos, or Cluster.dev. Growing into one would mean giving up the
property that makes this worth having: the output is a plain Terraform
repository with no runtime dependency on the thing that generated it.

## Now (0.1.x)

Shipped, tested, and on PyPI.

| Capability | |
| :--- | :--- |
| Interview-driven generation | one directory and one state file per environment |
| **Three stacks fully configured** | `vpc`, `eks`, `rds` — these plan and apply |
| Six stacks emit a marked stub | `[stub]` in the CLI, `GEN001` warning, `INCOMPLETE` in the file ([#19]) |
| Guardrails that **refuse to generate** | open data port, public database, unrestricted bastion, `skip_final_snapshot` in prod |
| Cost warnings | NAT-per-AZ and control planes costed before you commit |
| `suggest` on every sizing question | reasoning written to `DECISIONS.md` |
| **Explicit provider region and `default_tags`** | region is never inherited from the ambient shell ([#15]) |
| Generated repo is `terraform fmt` clean | so it passes the CI it ships with on its first commit |
| **`outputs.tf` per environment** | subnet ids, cluster endpoint, database address. No password output and none that reaches one ([#29]) |
| A real `plan` verified | `eks` + `rds` plans 64 resources, 0 changes, 0 destroys |

## Next (0.2)

[Milestone](https://github.com/ehtishammubarik/stackmason/milestone/1).
**Six of nine advertised stacks do not apply. Until that closes, nothing else
matters more.** Each is self-contained and independently mergeable.

| Item | Note |
| :--- | :--- |
| [Configure `s3`](https://github.com/ehtishammubarik/stackmason/issues/21) | Best first one: no VPC dependency, and the secure defaults are the interesting part |
| [Configure `alb`](https://github.com/ehtishammubarik/stackmason/issues/22) | A load balancer with no listeners applies, costs money, and routes nothing |
| [Configure `elasticache`](https://github.com/ehtishammubarik/stackmason/issues/23) | Watch the security group; this is where #11 went wrong for RDS |
| [Configure `msk`](https://github.com/ehtishammubarik/stackmason/issues/24) | Answers are already collected and then discarded |
| [Configure `redshift`](https://github.com/ehtishammubarik/stackmason/issues/25) | The most expensive stub to discover at apply time |
| [Configure `observability`](https://github.com/ehtishammubarik/stackmason/issues/26) | Largest. Helm releases, not a module reference; needs providers no generated repo configures yet |
| [Stub tracking issue](https://github.com/ehtishammubarik/stackmason/issues/10) | Closes when all six are done |
| [Plan summary before writing](https://github.com/ehtishammubarik/stackmason/issues/1) | Cost and blast radius visible before a file is created |
| [The interview never asks for a region](https://github.com/ehtishammubarik/stackmason/issues/14) | Needs a notion of questions belonging to no stack |

## Later (0.3+)

[Milestone](https://github.com/ehtishammubarik/stackmason/milestone/2). None of
it blocks 0.2.

| Item | Note |
| :--- | :--- |
| [More than one region per environment](https://github.com/ehtishammubarik/stackmason/issues/2) | Needs the global-question mechanism from #14 first |
| [Regenerate without clobbering local edits](https://github.com/ehtishammubarik/stackmason/issues/4) | The output is your repository; a second run should respect that |
| [Azure and GCP stacks](https://github.com/ehtishammubarik/stackmason/issues/3) | The registry is structured for it. Nothing is implemented, and the README says so |

## Not planned

Saying no is part of a roadmap, and here it is most of the product.

- **A flag that downgrades a refusal to a warning.** Permanent. See Vision.
- **Emitting any credential**, in any form, for any reason.
- **Vendoring `terraform-aws-modules`.** Copying them here would mean inheriting
  their maintenance burden, freezing their security fixes, and taking on their
  licensing. A pinned reference gets upstream fixes for free.
- **Drift detection, a control plane, or a state backend of our own.** That is
  Terragrunt, Atmos, and Cluster.dev, and they are good at it.
- **A runtime dependency of generated repos on `stackmason`.** The output is
  plain Terraform. You should be able to delete this tool and lose nothing.
- **Dependencies in the core.** CI enforces it.

## How this file stays true

`websieve`'s roadmap went stale: two shipped features sat under **Next** for
weeks, including the one labelled "the biggest correctness gap"
([websieve#34](https://github.com/ehtishammubarik/websieve/issues/34)). Someone
evaluating it read a solved problem as a live limitation.

So: **a PR that closes a roadmap item moves its row into Now, in the same PR.**
Not afterwards. The row names the PR that delivered it, which is what makes the
claim checkable rather than assertable. Reviewers, this is fair game to block
on.

The stack count in the opening paragraph is the number most likely to rot. It is
asserted against generated output by the test suite, so it cannot be wrong here
and right in the code without CI failing.

## Influencing this list

The order is a guess, and a real use case beats a guess.

- **Open an issue** describing what you were building and where this got in the
  way. Concrete beats abstract: which stacks, which environments, what broke.
- **Email** [contact@eprecisio.com](mailto:contact@eprecisio.com) if an issue is
  not the right shape.
- **LinkedIn:** [Ehtisham Mubarik](https://www.linkedin.com/in/ehtisham-mubarik)
  or [Eprecisio Technologies](https://www.linkedin.com/company/eprecisio/)

Issues labelled [`good first issue`](https://github.com/ehtishammubarik/stackmason/labels/good%20first%20issue)
are scoped so a first contribution does not require reading the whole codebase.
[`CONTRIBUTING.md`](CONTRIBUTING.md) says how to claim one so two people do not
build it twice, and has the `terraform validate` section you should read before
touching what gets emitted.

[#15]: https://github.com/ehtishammubarik/stackmason/pull/15
[#19]: https://github.com/ehtishammubarik/stackmason/pull/19
[#29]: https://github.com/ehtishammubarik/stackmason/pull/29
