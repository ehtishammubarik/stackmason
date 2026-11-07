# stackmason

**Answer some questions. Get a Terraform repository that is secure by default.**

![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-success)
![License](https://img.shields.io/badge/license-MIT-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)

## What it does

```
$ stackmason new acme --stack eks --stack rds -e dev,staging,prod

Kubernetes version
  options: 1.31, 1.30, 1.29
  default: 1.31
> k8s_version: 

Node instance type
  suggestion: m6i.large for mixed workloads. c6i for CPU-bound, r6i for
  memory-bound, g5 for GPU. t3 burstable only for dev, because CPU credit
  exhaustion under load looks exactly like a mysterious latency problem.
> node_instance_type [suggest]: suggest

[NOTE] COST000: Baseline before compute and storage: ~$369/month.
    -> Order of magnitude only, us-east-1 on-demand. Not a quote.

wrote 20 files to acme/
```

Out comes a repository, not a snippet:

```
acme/
  environments/dev/      main.tf  versions.tf  providers.tf  backend.tf  variables.tf
  environments/staging/
  environments/prod/
  .github/workflows/terraform.yml
  ARCHITECTURE.md        why it looks like this
  DECISIONS.md           every value chosen, and why
  README.md
  .gitignore
```

One state file per environment. Providers pinned and configured with an explicit
region, never one inherited from whichever shell happens to run it. Every
resource tagged with its project and environment. CI that checks formatting and
scans for credentials. `terraform validate` passes against the real upstream
modules, which is verified in this repo's own tests.

Pass `--region` to put both the resources and the state bucket somewhere other
than `us-east-1`.

## What it is trying to be

**The first ninety minutes of a new infrastructure repository, without the
mistakes that take six months to surface.**

Not a platform. Not a control plane. A generator that writes a first draft you
own outright, in plain Terraform, with no runtime dependency on this tool. You
should be able to delete `stackmason` afterwards and lose nothing.

- **The defaults are the product**, and they are not negotiable silently.
- **There will never be a flag that turns a refusal into a warning.** It is the
  most common request a scaffolding tool gets and the answer is permanently no:
  an escape hatch on a security default is the same as not having the default,
  because the hatch is what ends up in the example someone copies.
- **It never emits a credential**, in any form, for any reason.
- **Every choice is explicable six months later**, via `DECISIONS.md`.

Full detail, plus what is deliberately **not** planned, in the
[roadmap](ROADMAP.md). Work in flight is tracked in the
[0.2](https://github.com/ehtishammubarik/stackmason/milestone/1) and
[0.3](https://github.com/ehtishammubarik/stackmason/milestone/2) milestones.

## Why not just copy a blog post

Because the defaults are the product.

A scaffolding tool multiplies whatever it emits. One insecure default becomes a
hundred insecure deployments, run by people who reasonably assumed the
generator knew better. So these are enforced, not suggested:

| Guardrail | Behaviour |
|---|---|
| `0.0.0.0/0` on a data port | **Refuses to generate.** Ports 22, 3389, 5432, 3306, 1433, 27017, 6379, 9092, 5439, 9200 |
| Publicly accessible database | **Refuses to generate** |
| Bastion with no source CIDR | **Refuses to generate** |
| `skip_final_snapshot` in production | **Refuses to generate** |
| Local state | Warns. State holds every secret the plan touched, in plaintext |
| NAT gateway per AZ | Costs it out before you commit |
| An environment named `prod` configured like a dev one | Warns, naming the specific attribute and what it costs |

And what it always emits:

- **No credential, anywhere.** Not a placeholder, not a generated one, not
  base64. Secrets are `sensitive` variables with **no default**, because a
  default is what ends up committed.
- Encryption at rest, deletion protection and final snapshots in production,
  backup retention, IMDSv2, public-access blocks, private EKS endpoints.
- A `validation` block that rejects `0.0.0.0/0` at plan time, so the rule
  survives after the generator is gone.

Run `stackmason plan` to see all of it without writing a file.

## Install

```bash
pip install stackmason        # no dependencies
```

## Use

```bash
stackmason stacks                          # what is available
stackmason plan acme -s eks -s rds         # dry run, writes nothing
stackmason new acme -s eks -s rds          # interactive
stackmason new acme --answers a.json --yes # non-interactive, for CI
```

`--yes` takes the suggestion where one exists, so the same command is
reproducible in a pipeline.

## Stacks

**Three are configured. Six are stubs.** Saying so here rather than letting you
find out at `apply`:

| Stack | What | From | |
|---|---|---|---|
| `vpc` | Subnets, routing, NAT, flow logs | `terraform-aws-modules/vpc/aws` | ready |
| `eks` | Control plane, node groups, IRSA | `terraform-aws-modules/eks/aws` | ready |
| `rds` | PostgreSQL or MySQL, private, encrypted | `terraform-aws-modules/rds/aws` | ready |
| `msk` | Kafka | `terraform-aws-modules/msk-kafka-cluster/aws` | **stub** |
| `redshift` | Data warehouse | `terraform-aws-modules/redshift/aws` | **stub** |
| `elasticache` | Redis | `terraform-aws-modules/elasticache/aws` | **stub** |
| `s3` | Buckets, public access blocked | `terraform-aws-modules/s3-bucket/aws` | **stub** |
| `alb` | Load balancer with TLS | `terraform-aws-modules/alb/aws` | **stub** |
| `observability` | Prometheus, Grafana, Loki | on `eks` | **stub** |

A stub emits the module reference and a name and nothing else. `terraform
validate` passes on it, because every argument it omits defaults to null or `[]`
upstream, and `apply` then fails at the AWS API. Selecting one warns, the
generated block opens with `INCOMPLETE. This block will NOT apply.`, and
`stackmason stacks` marks it. Filling them in is
[#10](https://github.com/ehtishammubarik/stackmason/issues/10), and each one is
a self-contained contribution.

Dependencies resolve automatically: ask for `observability` and you get `vpc`,
`eks`, `observability`, in that order.

**Modules are referenced by version, never vendored.** Copying
`terraform-aws-modules` here would mean inheriting their maintenance burden,
freezing their security fixes, and taking on their licensing. A pinned
reference gets upstream fixes for free and keeps the provenance honest.

## "I do not know" is a valid answer

Every sizing question accepts `suggest` and returns a recommendation **with the
reasoning attached**. The reasoning is printed and written to `DECISIONS.md`,
so six months later the choice is explicable rather than archaeological.

The alternative is a user picking at random and blaming the tool, which is what
most scaffolding tools produce.

## What it is not

- **Not a replacement for understanding Terraform.** It writes the first draft.
  Reading the plan is still your job, and the generated README says so.
- **Not a compliance guarantee.** The defaults are good practice, not an audit.
- **Not a secret manager.** It emits `sensitive` variables and expects values
  from yours.
- **Not multi-cloud yet.** AWS only. The registry is structured for more.
- **Alpha.** The stack catalogue is nine entries, three of them configured, and
  the guardrails are the part that has had the most thought.

If you want a full platform with drift detection and a control plane, look at
Terragrunt, Atmos, or Cluster.dev. This does one thing: the first ninety
minutes of a new infrastructure repository, without the mistakes.

## FAQ

**Why does `terraform plan` succeed but `apply` fail?**

`plan` reads your configuration and tells you what it *would* do; it does not
call the AWS API. It succeeds even when a value is semantically empty or wrong,
because the configuration is type-correct. The failure happens later, at
`apply`, when the API rejects it. stackmason exists to close that gap — see
[#12](https://github.com/ehtishammubarik/stackmason/issues/12) for a concrete
example of a repo that validated and then could not apply.

**How do I stop a public S3 bucket with Terraform?**

Block public access on the bucket: set `block_public_acls`, `block_public_policy`,
`ignore_public_acls`, and `restrict_public_buckets` to `true`. stackmason's
`s3` stack emits these by default, and its guardrails refuse to generate an
open bucket in the first place.

**Why does stackmason refuse `skip_final_snapshot` in production?**

A final snapshot is the last copy of your database before it is destroyed.
Skipping it in production means a single bad `terraform destroy` permanently
loses the data. stackmason refuses to emit it for a `prod` environment, and
warns elsewhere.

**Why is stackmason AWS-only?**

The registry is built around AWS modules and their real behaviors. Azure and
GCP support is planned but not implemented yet — see
[#3](https://github.com/ehtishammubarik/stackmason/issues/3).

**What does "zero dependencies" mean in practice?**

`pip install stackmason` pulls in *nothing else* — no SDK, no runtime the tool
depends on. It writes plain Terraform, so after generation you can delete
stackmason and the repository keeps working.

**What happens if I pick a stub stack?**

You get a module reference and a name and nothing else. `terraform validate`
passes, because every omitted argument defaults to null or empty, but `apply`
fails at the AWS API. The tool marks these stacks `[stub]`, warns when you
select them, and writes an `INCOMPLETE. This block will NOT apply.` banner into
the generated block. Filling the six stubs out is
[#10](https://github.com/ehtishammubarik/stackmason/issues/10).

**Does CI prove the generated repo actually plans?**

Not yet. [#31](https://github.com/ehtishammubarik/stackmason/issues/31) tracks
a CI-level plan gate; until it lands, `terraform validate` is the automated
check, and the README asks you to run a real `terraform plan` and read it
before applying.

## Releases

Published on tag. The pipeline installs the built wheel and sdist into clean
environments and runs the suite against the **installed** package, generates a
repository with every stack and runs `terraform validate` against the real
upstream modules, then publishes to TestPyPI and verifies there before touching
PyPI. Package index versions cannot be reused, so a bad publish is permanent.

`scripts/verify-published.sh` verifies a release afterwards in Docker on stock
`python:3.x-slim` images, testing what `pip install stackmason` actually
delivers rather than an artifact CI just built.

## Testing

```bash
pip install -e ".[dev]" && pytest
```

No network and no cloud credentials required. CI enforces a coverage floor of
88% on Python 3.10, 3.11, and 3.12, so that figure is a gate rather than a
claim: it cannot be true here and false in the repository.

There is deliberately no test count in this file. The last one said 57 when the
suite was 96, and a number nobody maintains is worse than no number, because it
gives the reader no way to tell which of the other claims here are also stale.

Generated output is checked to be `terraform fmt` clean, so a generated repo
passes the CI it ships with on its first commit rather than failing it.

## License

MIT.

---

If `stackmason` saved you the first ninety minutes of an infrastructure repo, a
star helps the next person find it. Contributions welcome, and the
[roadmap](ROADMAP.md) says which issues are load-bearing:
[`good first issue`](https://github.com/ehtishammubarik/stackmason/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
is pre-scoped, and [CONTRIBUTING.md](CONTRIBUTING.md) says how to claim one so
two people do not build it twice. Read the `terraform validate` section first if
you are touching what gets emitted.
