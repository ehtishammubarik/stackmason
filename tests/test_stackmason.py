import ipaddress
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stackmason.cli import main
from stackmason.generate import DEFAULT_REGION, align_hcl, build_plan, write
from stackmason.guardrails import DATA_PORTS, Severity, evaluate
from stackmason.interview import MAX_ATTEMPTS, Interview, ValidationError
from stackmason.stacks.registry import (
    ALL_STACKS,
    BY_ID,
    monthly_floor,
    questions_for,
    resolve_dependencies,
)

BASE = {
    "cidr": "10.0.0.0/16",
    "az_count": 2,
    "k8s_version": "1.31",
    "node_group_count": 1,
    "node_instance_type": "m6i.large",
    "node_min": 2,
    "node_max": 6,
    "engine": "postgres",
    "instance_class": "db.m6g.large",
    "storage_gb": 50,
    "allowed_cidrs": ["10.1.0.0/16"],
}


# -- registry --------------------------------------------------------------


def test_dependencies_resolve_transitively():
    # observability needs eks, which needs vpc.
    assert resolve_dependencies(["observability"]) == ["vpc", "eks", "observability"]


def test_dependencies_are_deduplicated_and_ordered():
    got = resolve_dependencies(["rds", "eks"])
    assert got.index("vpc") == 0
    assert got.count("vpc") == 1


def test_unknown_stack_names_the_known_ones():
    with pytest.raises(KeyError, match="unknown stack"):
        resolve_dependencies(["nonsense"])


def test_cost_scales_with_environments():
    assert monthly_floor(["eks"], 3) == pytest.approx(monthly_floor(["eks"], 1) * 3)


def test_every_stack_pins_a_module_version():
    # An unpinned module makes a generated plan irreproducible.
    for s in ALL_STACKS:
        assert s.module_version.startswith("~>"), s.id
        assert s.module_source.count("/") >= 2, s.id


def test_every_stack_requirement_exists():
    for s in ALL_STACKS:
        for dep in s.requires:
            assert dep in BY_ID, f"{s.id} requires unknown {dep}"


# -- interview -------------------------------------------------------------


def test_every_recommendation_is_answerable_with_suggest():
    # A question that advertises advice the parser then rejects is worse than
    # one with no advice: the user types what the prompt implied and is told it
    # is invalid. This is the invariant that bug violated.
    iv = Interview([s.id for s in ALL_STACKS])
    for _, q in questions_for(iv.stacks):
        if q.recommend:
            assert iv.suggestion_for(q) is not None, f"{q.key} recommends but cannot suggest"


def test_suggest_records_the_reason():
    iv = Interview(["eks"])
    q = next(q for _, q in questions_for(["eks"]) if q.key == "node_instance_type")
    iv.answer(q, "suggest")
    assert iv.answers["node_instance_type"] == "m6i.large"
    assert "measured" in iv.reasons["node_instance_type"]


def test_questions_are_skipped_once_answered():
    iv = Interview(["vpc"])
    before = len(iv.pending())
    iv.answers["cidr"] = "10.9.0.0/16"
    assert len(iv.pending()) == before - 1


def test_int_bounds_are_enforced():
    iv = Interview(["vpc"])
    q = next(q for _, q in questions_for(["vpc"]) if q.key == "az_count")
    with pytest.raises(ValidationError, match="at most"):
        iv.validate(q, 99)
    with pytest.raises(ValidationError, match="whole number"):
        iv.validate(q, "two")


def test_choice_rejects_unknown_values():
    iv = Interview(["eks"])
    q = next(q for _, q in questions_for(["eks"]) if q.key == "k8s_version")
    with pytest.raises(ValidationError, match="not one of"):
        iv.validate(q, "0.1")


def test_confirm_accepts_common_spellings():
    iv = Interview(["vpc"])
    q = next(q for _, q in questions_for(["vpc"]) if q.key == "enable_flow_logs")
    assert iv.validate(q, "yes") is True
    assert iv.validate(q, "n") is False


def test_a_driver_returning_garbage_fails_rather_than_hanging():
    # An unbounded retry loop meant a config file or an agent could spin forever.
    with pytest.raises(ValidationError, match=f"gave up after {MAX_ATTEMPTS}"):
        Interview(["vpc"]).run(lambda q: "garbage", on_error=lambda m: None)


def test_non_interactive_run_answers_everything():
    iv = Interview(["eks", "rds"])
    answers = iv.run(
        lambda q: "suggest" if iv.suggestion_for(q) else q.default, on_error=lambda m: None
    )
    assert len(answers) == len(questions_for(iv.stacks))


# -- guardrails ------------------------------------------------------------


def test_public_cidr_on_private_service_is_blocked():
    r = evaluate({"stacks": ["rds"], "allowed_cidrs": ["0.0.0.0/0"]})
    assert r.blocked
    assert any(f.code == "NET001" for f in r.findings)


def test_public_database_is_blocked():
    r = evaluate({"stacks": ["rds"], "public_database": True})
    assert r.blocked and any(f.code == "NET002" for f in r.findings)


def test_bastion_without_cidr_restriction_is_blocked():
    assert evaluate({"stacks": ["vpc"], "bastion": True, "allowed_cidrs": []}).blocked


def test_skip_final_snapshot_in_production_is_blocked():
    r = evaluate({"stacks": ["rds"], "environments": ["prod"], "skip_final_snapshot": True})
    assert r.blocked and any(f.code == "DAT001" for f in r.findings)


def test_skip_final_snapshot_outside_production_is_allowed():
    r = evaluate({"stacks": ["rds"], "environments": ["dev"], "skip_final_snapshot": True})
    assert not r.blocked


def test_local_state_warns_but_does_not_block():
    r = evaluate({"stacks": ["vpc"], "backend": "local"})
    assert not r.blocked
    assert r.by_severity(Severity.WARN)


def test_nat_gateway_cost_is_surfaced():
    r = evaluate(
        {
            "stacks": ["vpc"],
            "az_count": 3,
            "nat_gateway_per_az": True,
            "environments": ["dev", "prod"],
        }
    )
    note = next(f for f in r.findings if f.code == "COST001")
    assert "6" in note.message  # 3 AZs x 2 environments


def test_a_safe_configuration_produces_no_blocking_findings():
    assert not evaluate({**BASE, "stacks": ["eks", "rds"], "environments": ["dev"]}).blocked


def test_every_check_runs_even_after_one_blocks():
    # The full picture matters; short circuiting would hide the cost note.
    r = evaluate(
        {
            "stacks": ["eks", "rds"],
            "allowed_cidrs": ["0.0.0.0/0"],
            "environments": ["prod"],
            "skip_final_snapshot": True,
            "az_count": 3,
            "nat_gateway_per_az": True,
        }
    )
    codes = {f.code for f in r.findings}
    assert {"NET001", "DAT001", "COST001"} <= codes


def test_data_ports_cover_the_common_databases():
    for port in (22, 3389, 5432, 3306, 6379):
        assert port in DATA_PORTS


# -- generation ------------------------------------------------------------


def test_generates_one_directory_per_environment(tmp_path):
    plan = build_plan(tmp_path, "acme", ["eks"], ["dev", "staging", "prod"], BASE)
    for env in ("dev", "staging", "prod"):
        assert f"environments/{env}/main.tf" in plan.files


def test_generated_terraform_is_already_formatted(tmp_path):
    # The generated repo ships CI that checks formatting. Unformatted output
    # would fail its own CI on the first commit.
    plan = build_plan(tmp_path, "acme", ["eks", "rds"], ["prod"], BASE)
    for rel, content in plan.files.items():
        if rel.endswith(".tf"):
            assert align_hcl(content) == content, rel


def test_no_credential_is_ever_emitted(tmp_path):
    plan = build_plan(tmp_path, "acme", ["rds"], ["prod"], BASE)
    joined = "\n".join(plan.files.values())
    assert "var.db_password" in joined  # referenced
    assert 'password = "' not in joined  # never a literal


def test_database_password_variable_has_no_default(tmp_path):
    plan = build_plan(tmp_path, "acme", ["rds"], ["prod"], BASE)
    variables = plan.files["environments/prod/variables.tf"]
    assert "sensitive   = true" in variables
    block = variables[variables.index('variable "db_password"') :].split("}")[0]
    # Check for the assignment, not the word: the block deliberately contains a
    # comment explaining why there is no default.
    assert not re.search(r"^\s*default\s*=", block, re.M), block


def test_production_gets_deletion_protection_and_dev_does_not(tmp_path):
    plan = build_plan(tmp_path, "acme", ["rds"], ["dev", "prod"], BASE)
    assert "deletion_protection     = true" in plan.files["environments/prod/main.tf"]
    assert "deletion_protection     = false" in plan.files["environments/dev/main.tf"]


def test_database_is_never_publicly_accessible(tmp_path):
    plan = build_plan(tmp_path, "acme", ["rds"], ["dev", "prod"], BASE)
    for env in ("dev", "prod"):
        # Matched on the assignment rather than an exact string: `=` alignment
        # shifts with the longest key in the surrounding run, so any new
        # neighbouring argument silently changes the spacing.
        assert re.search(
            r"^\s+publicly_accessible\s+= false$", plan.files[f"environments/{env}/main.tf"], re.M
        )


def test_eks_endpoint_is_private_by_default(tmp_path):
    plan = build_plan(tmp_path, "acme", ["eks"], ["prod"], BASE)
    assert "cluster_endpoint_public_access  = false" in plan.files["environments/prod/main.tf"]


def test_provider_and_terraform_versions_are_pinned(tmp_path):
    plan = build_plan(tmp_path, "acme", ["vpc"], ["dev"], BASE)
    versions = plan.files["environments/dev/versions.tf"]
    assert "required_version" in versions and "hashicorp/aws" in versions


def test_gitignore_covers_state_and_tfvars(tmp_path):
    plan = build_plan(tmp_path, "acme", ["vpc"], ["dev"], BASE)
    ignore = plan.files[".gitignore"]
    for pattern in ("*.tfstate", ".terraform/", "*.tfvars"):
        assert pattern in ignore


def test_cidr_validation_block_is_emitted(tmp_path):
    # Emitted alongside a stack that consumes the variable. A VPC on its own no
    # longer declares allowed_cidrs, because nothing in it reads one, and a
    # required variable nobody reads stops `plan` to ask a question with no
    # consequence. See #7.
    plan = build_plan(tmp_path, "acme", ["eks"], ["dev"], BASE)
    assert 'contains(var.allowed_cidrs, "0.0.0.0/0")' in plan.files["environments/dev/variables.tf"]


def test_write_refuses_a_blocked_plan(tmp_path):
    plan = build_plan(tmp_path, "acme", ["rds"], ["prod"], {**BASE, "allowed_cidrs": ["0.0.0.0/0"]})
    assert plan.blocked
    with pytest.raises(RuntimeError, match="blocked by guardrails"):
        write(plan)
    assert not list(tmp_path.iterdir())


def test_write_refuses_to_overwrite_without_force(tmp_path):
    plan = build_plan(tmp_path, "acme", ["vpc"], ["dev"], BASE)
    write(plan)
    with pytest.raises(FileExistsError):
        write(plan)
    write(plan, force=True)


def test_node_group_count_is_honoured(tmp_path):
    plan = build_plan(tmp_path, "acme", ["eks"], ["dev"], {**BASE, "node_group_count": 3})
    assert plan.files["environments/dev/main.tf"].count("instance_types") == 3


def test_decisions_file_records_every_answer(tmp_path):
    plan = build_plan(tmp_path, "acme", ["eks"], ["dev"], BASE)
    decisions = plan.files["DECISIONS.md"]
    assert "node_instance_type" in decisions and "m6i.large" in decisions


# -- hcl alignment ---------------------------------------------------------


def test_alignment_matches_terraform_style():
    assert align_hcl("a = 1\nbbb = 2\n") == "a   = 1\nbbb = 2\n"


def test_alignment_groups_break_on_blank_lines():
    assert align_hcl("a = 1\n\nbbbbb = 2\n") == "a = 1\n\nbbbbb = 2\n"


def test_alignment_leaves_comments_alone():
    assert align_hcl("# a = 1\nbb = 2\n") == "# a = 1\nbb = 2\n"


def test_alignment_is_idempotent():
    once = align_hcl("a = 1\nbbb = 2\n")
    assert align_hcl(once) == once


# -- cli -------------------------------------------------------------------


def test_stacks_command_lists_everything(capsys):
    assert main(["stacks"]) == 0
    out = capsys.readouterr().out
    for s in ALL_STACKS:
        assert s.id in out


def test_plan_writes_nothing(tmp_path):
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({**BASE, "stacks": ["eks"], "environments": ["dev"]}))
    out = tmp_path / "proj"
    assert main(["plan", "acme", "--answers", str(answers), "--yes", "-o", str(out)]) == 0
    assert not out.exists()


def test_new_generates_from_an_answers_file(tmp_path):
    answers = tmp_path / "a.json"
    answers.write_text(
        json.dumps({**BASE, "stacks": ["eks", "rds"], "environments": ["dev", "prod"]})
    )
    out = tmp_path / "proj"
    assert main(["new", "acme", "--answers", str(answers), "--yes", "-o", str(out)]) == 0
    assert (out / "environments/prod/main.tf").exists()
    assert (out / "ARCHITECTURE.md").exists()


def test_new_exits_nonzero_when_blocked(tmp_path):
    answers = tmp_path / "a.json"
    answers.write_text(
        json.dumps(
            {**BASE, "stacks": ["rds"], "environments": ["prod"], "allowed_cidrs": ["0.0.0.0/0"]}
        )
    )
    out = tmp_path / "proj"
    assert main(["new", "acme", "--answers", str(answers), "--yes", "-o", str(out)]) == 1
    assert not out.exists()


def test_module_is_executable():
    r = subprocess.run(
        [sys.executable, "-m", "stackmason.cli", "--help"], capture_output=True, text=True
    )
    assert r.returncode == 0 and "stackmason" in r.stdout


# -- module API correctness -------------------------------------------------

# These are not `name` in the upstream modules. Getting one wrong fails at
# `terraform init` with "Unsupported argument", which no schema check catches
# and no unit test found: it took running real terraform against real modules.
EXPECTED_NAME_ATTRIBUTES = {
    "vpc": "name",
    "eks": "cluster_name",
    "rds": "identifier",
    "redshift": "cluster_identifier",
    "elasticache": "replication_group_id",
    "s3": "bucket",
    "alb": "name",
    "msk": "name",
}


@pytest.mark.parametrize("sid,attr", sorted(EXPECTED_NAME_ATTRIBUTES.items()))
def test_module_name_attribute_matches_upstream(sid, attr):
    assert BY_ID[sid].name_attribute == attr


def test_every_stack_declares_a_name_attribute():
    for s in ALL_STACKS:
        assert s.name_attribute, s.id


def test_generated_eks_uses_cluster_name_not_name(tmp_path):
    plan = build_plan(tmp_path, "acme", ["eks"], ["dev"], BASE)
    main_tf = plan.files["environments/dev/main.tf"]
    eks_block = main_tf[main_tf.index('module "eks"') :]
    assert "cluster_name" in eks_block.split("}")[0]


# -- production intent ------------------------------------------------------

PROD_SLOPPY = {
    "stacks": ["eks", "rds"],
    "environments": ["dev", "prod"],
    "allowed_cidrs": ["10.1.0.0/16"],
    "multi_az": False,
    "az_count": 2,
    "nat_gateway_per_az": False,
    "node_min": 1,
    "spot": True,
    "backup_retention_days": 7,
}


def test_production_named_environment_warns_on_single_az_database():
    codes = {f.code for f in evaluate(PROD_SLOPPY).findings}
    assert "ENV001" in codes


def test_production_warns_on_shared_nat_single_node_and_spot():
    codes = {f.code for f in evaluate(PROD_SLOPPY).findings}
    assert {"ENV003", "ENV004", "ENV005"} <= codes


def test_production_intent_warns_but_never_blocks():
    # Plenty of people have one environment, call it prod, and run it cheaply.
    # Blocking that would be wrong; saying nothing would also be wrong.
    assert not evaluate(PROD_SLOPPY).blocked


@pytest.mark.parametrize("name", ["prod", "production", "PROD", "Live", "prd"])
def test_production_names_are_recognised(name):
    answers = {**PROD_SLOPPY, "environments": ["dev", name]}
    assert any(f.code.startswith("ENV") for f in evaluate(answers).findings)


@pytest.mark.parametrize("name", ["dev", "staging", "test", "sandbox"])
def test_non_production_names_are_left_alone(name):
    answers = {**PROD_SLOPPY, "environments": [name]}
    assert not any(f.code.startswith("ENV") for f in evaluate(answers).findings)


def test_a_well_configured_production_environment_is_quiet():
    answers = {
        "stacks": ["eks", "rds"],
        "environments": ["prod"],
        "allowed_cidrs": ["10.1.0.0/16"],
        "multi_az": True,
        "az_count": 2,
        "nat_gateway_per_az": True,
        "node_min": 3,
        "spot": False,
        "backup_retention_days": 14,
    }
    assert not [f for f in evaluate(answers).findings if f.code.startswith("ENV")]


def test_backup_retention_is_not_reported_twice():
    # check_data_protection already covers it. Two findings for one problem
    # trains people to skim the report.
    codes = [f.code for f in evaluate({**PROD_SLOPPY, "backup_retention_days": 1}).findings]
    assert len(codes) == len(set(codes))
    assert "DAT002" in codes


def test_every_production_finding_names_a_cost_or_a_consequence():
    for f in evaluate(PROD_SLOPPY).findings:
        if f.code.startswith("ENV"):
            assert len(f.remedy) > 40, f.code  # not "consider hardening production"


# -- generated repository invariants ----------------------------------------
#
# Everything below asserts a property of the emitted Terraform rather than of
# the Python that emitted it. `terraform validate` cannot see any of these:
# an empty subnet list, an inert subnet group, and a variable nobody reads are
# all type-correct. Three separate defects of that shape shipped before these
# existed (#7, #11, #12), so the gate is the point, not the individual asserts.


def _env(stacks, answers=None, env="prod"):
    plan = build_plan(Path("/tmp/x"), "acme", stacks, [env], answers or BASE)
    return (
        plan.files[f"environments/{env}/main.tf"],
        plan.files[f"environments/{env}/variables.tf"],
        plan.files[f"environments/{env}/terraform.tfvars.example"],
    )


def _declared_variables(variables_tf):
    """Variable names, and whether each has a default."""
    out = {}
    for block in re.finditer(r'variable "(\w+)" \{(.*?)\n\}', variables_tf, re.S):
        out[block.group(1)] = "default" in block.group(2)
    return out


def test_copying_the_example_is_enough_to_plan_without_a_prompt():
    # The defect in #7: a required variable absent from the example turns
    # `terraform plan -input=false` into a hard failure in CI.
    for stacks in (["vpc"], ["eks"], ["rds"], ["eks", "rds"], ["s3"]):
        _, variables_tf, example = _env(stacks)
        for name, has_default in _declared_variables(variables_tf).items():
            if has_default:
                continue
            assigned = re.search(rf"^{name}\s*=", example, re.M)
            mentioned = name in example
            assert assigned or mentioned, (
                f"{stacks}: {name} is required but the example neither sets it "
                "nor says where its value comes from"
            )


def test_no_secret_is_assigned_a_value_in_the_example():
    # A required secret must be mentioned, never assigned. An example
    # credential is the credential that ships.
    _, _, example = _env(["rds"])
    assert "db_password" in example
    assert not re.search(r"^db_password\s*=", example, re.M)


def test_a_required_variable_with_no_example_and_no_note_is_refused():
    from stackmason.generate import Variable, _tfvars_example

    with pytest.raises(RuntimeError, match="terraform plan would prompt"):
        _tfvars_example([Variable("orphan", "no example, no note", "string")])


def test_allowed_cidrs_is_emitted_only_when_something_consumes_it():
    _, vars_without, _ = _env(["s3"])
    assert "allowed_cidrs" not in vars_without

    for stacks in (["eks"], ["rds"]):
        _, vars_with, _ = _env(stacks)
        assert "allowed_cidrs" in vars_with, stacks


def test_allowed_cidrs_is_actually_referenced_by_a_resource():
    # #7: the variable carried a validation block forbidding 0.0.0.0/0 while
    # reaching nothing. A guardrail that cannot fire is worse than an absent
    # one, because it gets counted in a review.
    for stacks in (["eks"], ["rds"]):
        main_tf, variables_tf, _ = _env(stacks)
        assert "allowed_cidrs" in variables_tf, stacks
        assert "var.allowed_cidrs" in main_tf, stacks


def test_every_subnet_list_consumed_is_also_defined():
    # #12: private_subnets and database_subnets were read twice and never set,
    # so both resolved to [] and every generated repo failed at apply.
    main_tf, _, _ = _env(["eks", "rds"])
    consumed = set(re.findall(r"module\.vpc\.(\w*subnets)", main_tf))
    assert consumed, "expected the generated repo to consume subnet outputs"
    for name in consumed:
        assert re.search(rf"^\s+{name}\s*=", main_tf, re.M), (
            f"module.vpc.{name} is consumed but never defined on the vpc module"
        )


@pytest.mark.parametrize("az_count", [1, 2, 3, 6])
def test_subnet_ranges_do_not_overlap(az_count):
    # Computed here rather than trusted from the template, because an
    # overlapping plan fails at apply with a message about the second subnet,
    # not about the layout.
    base = ipaddress.ip_network("10.0.0.0/16")
    private = [list(base.subnets(prefixlen_diff=4))[i] for i in range(az_count)]
    public = [list(base.subnets(prefixlen_diff=8))[128 + i] for i in range(az_count)]
    database = [list(base.subnets(prefixlen_diff=8))[192 + i] for i in range(az_count)]

    everything = private + public + database
    for a, b in itertools.combinations(everything, 2):
        assert not a.overlaps(b), f"{a} overlaps {b} at az_count={az_count}"
    for net in everything:
        assert net.subnet_of(base)


def test_database_lands_in_the_vpc_not_the_default_one():
    # #11: subnet_ids is inert unless create_db_subnet_group is true, and
    # without a subnet group the instance is created in the default VPC.
    main_tf, _, _ = _env(["rds"])
    rds = main_tf[main_tf.index('module "rds"') :].split("\n}")[0]
    assert "create_db_subnet_group = true" in rds
    assert "module.vpc.database_subnets" in rds


def test_database_gets_its_own_security_group_not_the_default():
    # An empty vpc_security_group_ids means the VPC default group, which
    # permits everything from anything else carrying it.
    main_tf, _, _ = _env(["rds"])
    assert 'resource "aws_security_group" "rds"' in main_tf
    assert "vpc_security_group_ids = [aws_security_group.rds.id]" in main_tf

    sg = main_tf[main_tf.index('resource "aws_security_group" "rds"') :]
    assert "cidr_blocks = var.allowed_cidrs" in sg
    assert "0.0.0.0/0" not in sg


@pytest.mark.parametrize(("engine", "port"), [("postgres", 5432), ("mysql", 3306)])
def test_the_security_group_port_matches_the_engine(engine, port):
    # A security group opened on the wrong port fails as a timeout, which is
    # the least diagnosable failure there is.
    main_tf, _, _ = _env(["rds"], {**BASE, "engine": engine})
    assert f"port              = {port}" in main_tf
    sg = main_tf[main_tf.index('resource "aws_security_group" "rds"') :]
    assert f"from_port   = {port}" in sg
    assert f"to_port     = {port}" in sg


def test_rds_sets_the_arguments_its_parameter_group_requires():
    # family and major_engine_version have no defaults upstream. Omitting them
    # planned 62 resources successfully and then failed on the 63rd.
    main_tf, _, _ = _env(["rds"])
    for arg in ("engine_version", "family", "major_engine_version"):
        assert re.search(rf"^\s+{arg}\s*=", main_tf, re.M), arg


def test_no_generated_terraform_leaks_a_credential():
    main_tf, variables_tf, example = _env(["eks", "rds"])
    for blob in (main_tf, variables_tf, example):
        assert not re.search(r'password\s*=\s*"[^"$]', blob)


# -- provider, region, and tags ---------------------------------------------


def test_every_environment_configures_its_provider():
    # Without a provider block the region comes from AWS_REGION, an active
    # profile, or nothing, while backend.tf hardcodes one. See #8.
    plan = build_plan(Path("/tmp/x"), "acme", ["vpc"], ["dev", "prod"], BASE)
    for env in ("dev", "prod"):
        providers = plan.files[f"environments/{env}/providers.tf"]
        assert 'provider "aws"' in providers
        assert "region = var.aws_region" in providers


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1", "ap-southeast-2"])
def test_backend_region_and_provider_region_cannot_diverge(region):
    # The failure this prevents is silent: state in one region, resources in
    # another, and a plan that wants to destroy everything when the next person
    # runs it from a different shell.
    plan = build_plan(Path("/tmp/x"), "acme", ["vpc"], ["prod"], {**BASE, "aws_region": region})
    backend = plan.files["environments/prod/backend.tf"]
    variables = plan.files["environments/prod/variables.tf"]

    declared = re.search(r'variable "aws_region".*?default\s*=\s*"([^"]+)"', variables, re.S)
    in_backend = re.search(r'^\s+region\s+=\s+"([^"]+)"', backend, re.M)
    assert declared and in_backend
    assert declared.group(1) == in_backend.group(1) == region


def test_region_defaults_are_consistent_when_nothing_is_asked():
    plan = build_plan(Path("/tmp/x"), "acme", ["vpc"], ["dev"], BASE)
    assert f'"{DEFAULT_REGION}"' in plan.files["environments/dev/variables.tf"]
    assert f'region         = "{DEFAULT_REGION}"' in plan.files["environments/dev/backend.tf"]


def test_default_tags_carry_project_environment_and_owner():
    # Untagged infrastructure is the most common reason a cloud bill cannot be
    # explained, and the generator already knows all three values.
    plan = build_plan(Path("/tmp/x"), "acme", ["vpc"], ["prod"], BASE)
    providers = plan.files["environments/prod/providers.tf"]
    assert "default_tags" in providers
    for tag in ("Project", "Environment", "ManagedBy"):
        assert re.search(rf"^\s+{tag}\s+=", providers, re.M), tag
    assert 'ManagedBy   = "terraform"' in providers


def test_providers_file_is_already_formatted():
    plan = build_plan(Path("/tmp/x"), "acme", ["vpc"], ["prod"], BASE)
    providers = plan.files["environments/prod/providers.tf"]
    assert align_hcl(providers) == providers


def test_region_flag_reaches_the_generated_repository(tmp_path):
    out = tmp_path / "repo"
    main(
        [
            "new",
            "acme",
            "-s",
            "vpc",
            "-e",
            "dev",
            "-o",
            str(out),
            "--region",
            "ap-south-1",
            "--yes",
        ]
    )
    assert 'region         = "ap-south-1"' in (out / "environments/dev/backend.tf").read_text()
    assert '"ap-south-1"' in (out / "environments/dev/variables.tf").read_text()
