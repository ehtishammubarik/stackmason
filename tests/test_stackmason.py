import json
import re
import subprocess
import sys

import pytest

from stackmason.cli import main
from stackmason.generate import align_hcl, build_plan, write
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
        assert "publicly_accessible = false" in plan.files[f"environments/{env}/main.tf"]


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
    plan = build_plan(tmp_path, "acme", ["vpc"], ["dev"], BASE)
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
