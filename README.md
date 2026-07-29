# stackwright

**Answer some questions. Get a Terraform repository that is secure by default.**

![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-success)
![License](https://img.shields.io/badge/license-MIT-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)

## What it does

```
$ stackwright new acme --stack eks --stack rds -e dev,staging,prod

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
  environments/dev/      main.tf  versions.tf  backend.tf  variables.tf
  environments/staging/
  environments/prod/
  .github/workflows/terraform.yml
  ARCHITECTURE.md        why it looks like this
  DECISIONS.md           every value chosen, and why
  README.md
  .gitignore
```

One state file per environment. Providers pinned. CI that checks formatting and
scans for credentials. `terraform validate` passes against the real upstream
modules, which is verified in this repo's own tests.

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

And what it always emits:

- **No credential, anywhere.** Not a placeholder, not a generated one, not
  base64. Secrets are `sensitive` variables with **no default**, because a
  default is what ends up committed.
- Encryption at rest, deletion protection and final snapshots in production,
  backup retention, IMDSv2, public-access blocks, private EKS endpoints.
- A `validation` block that rejects `0.0.0.0/0` at plan time, so the rule
  survives after the generator is gone.

Run `stackwright plan` to see all of it without writing a file.

## Install

```bash
pip install stackwright        # no dependencies
```

## Use

```bash
stackwright stacks                          # what is available
stackwright plan acme -s eks -s rds         # dry run, writes nothing
stackwright new acme -s eks -s rds          # interactive
stackwright new acme --answers a.json --yes # non-interactive, for CI
```

`--yes` takes the suggestion where one exists, so the same command is
reproducible in a pipeline.

## Stacks

| Stack | What | From |
|---|---|---|
| `vpc` | Subnets, routing, NAT, flow logs | `terraform-aws-modules/vpc/aws` |
| `eks` | Control plane, node groups, IRSA | `terraform-aws-modules/eks/aws` |
| `rds` | PostgreSQL or MySQL, private, encrypted | `terraform-aws-modules/rds/aws` |
| `msk` | Kafka | `terraform-aws-modules/msk-kafka-cluster/aws` |
| `redshift` | Data warehouse | `terraform-aws-modules/redshift/aws` |
| `elasticache` | Redis | `terraform-aws-modules/elasticache/aws` |
| `s3` | Buckets, public access blocked | `terraform-aws-modules/s3-bucket/aws` |
| `alb` | Load balancer with TLS | `terraform-aws-modules/alb/aws` |
| `observability` | Prometheus, Grafana, Loki | on `eks` |

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
- **Alpha.** The stack catalogue is nine entries and the guardrails are the
  part that has had the most thought.

If you want a full platform with drift detection and a control plane, look at
Terragrunt, Atmos, or Cluster.dev. This does one thing: the first ninety
minutes of a new infrastructure repository, without the mistakes.

## Testing

```bash
pip install -e ".[dev]" && pytest
```

57 tests, 88% coverage, no network and no cloud credentials required.

Generated output is checked to be `terraform fmt` clean, so a generated repo
passes the CI it ships with on its first commit rather than failing it.

## License

MIT.
