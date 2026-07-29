"""Command line interface.

stackmason new <project>              interactive interview
stackmason new <project> --answers a.json --yes
stackmason stacks                     list what is available
stackmason plan <project> ...         show what would be written, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .generate import build_plan, write
from .interview import Interview, ValidationError, render_question
from .stacks.registry import ALL_STACKS, monthly_floor


def _ask(q) -> object:
    print()
    print(render_question(q))
    suffix = " [suggest]" if q.recommend else ""
    return input(f"> {q.key}{suffix}: ").strip()


def _collect(args) -> tuple[list[str], list[str], dict]:
    stacks = args.stack or []
    envs = [e.strip() for e in args.environments.split(",") if e.strip()]

    if args.answers:
        answers = json.loads(Path(args.answers).read_text())
        stacks = stacks or answers.pop("stacks", [])
        envs = answers.pop("environments", envs)
    else:
        answers = {}

    if not stacks:
        print("Which stacks do you want? Comma separated.\n")
        for s in ALL_STACKS:
            req = f"  (requires {', '.join(s.requires)})" if s.requires else ""
            print(f"  {s.id:15} {s.summary}{req}")
        stacks = [x.strip() for x in input("\n> stacks: ").split(",") if x.strip()]

    iv = Interview(stacks)
    iv.answers.update(answers)
    if args.yes:
        # Non-interactive: take the suggestion where one exists, else the default.
        iv.run(
            lambda q: "suggest" if iv.suggestion_for(q) else q.default,
            on_error=lambda m: print(f"  {m}", file=sys.stderr),
        )
    else:
        iv.run(_ask)
    return iv.stacks, envs, {**iv.answers, "_reasons": iv.reasons}


def cmd_stacks(args) -> int:
    by_cat: dict[str, list] = {}
    for s in ALL_STACKS:
        by_cat.setdefault(s.category, []).append(s)
    for cat, items in by_cat.items():
        print(f"\n{cat}")
        for s in items:
            cost = f"  from ~${s.monthly_floor_usd:.0f}/mo" if s.monthly_floor_usd else ""
            print(f"  {s.id:15} {s.summary}{cost}")
            if s.requires:
                print(f"  {'':15} requires: {', '.join(s.requires)}")
    return 0


def cmd_plan(args) -> int:
    stacks, envs, answers = _collect(args)
    reasons = answers.pop("_reasons", {})
    plan = build_plan(Path(args.output or args.project), args.project, stacks, envs, answers)

    print(f"\n{len(plan.files)} files across {len(envs)} environment(s):\n")
    print(plan.tree())

    print(f"\nrough fixed cost: ~${monthly_floor(stacks, len(envs)):.0f}/month\n")
    if plan.guardrails and plan.guardrails.findings:
        print(plan.guardrails.render())
    if reasons:
        print("\nsuggested values:")
        for k, why in reasons.items():
            print(f"  {k}: {why}")
    if plan.blocked:
        print("\nBLOCKED. Nothing was written.", file=sys.stderr)
        return 1
    print("\nNothing written. Run 'new' to generate.")
    return 0


def cmd_new(args) -> int:
    stacks, envs, answers = _collect(args)
    reasons = answers.pop("_reasons", {})
    root = Path(args.output or args.project)
    plan = build_plan(root, args.project, stacks, envs, answers)

    if plan.guardrails and plan.guardrails.findings:
        print("\n" + plan.guardrails.render())

    if plan.blocked:
        print("\nBLOCKED. Fix the findings above; nothing was written.", file=sys.stderr)
        return 1

    if not args.yes:
        print(f"\nAbout to write {len(plan.files)} files to {root}/")
        if input("proceed? [y/N]: ").strip().lower() not in {"y", "yes"}:
            print("aborted")
            return 1

    written = write(plan, force=args.force)
    print(f"\nwrote {len(written)} files to {root}/")

    if reasons:
        # Show why the suggested values were chosen. Without this the user gets
        # an instance type they did not pick and no way to evaluate it. The same
        # reasoning is written to DECISIONS.md so it outlives this terminal.
        print("\nvalues chosen for you, and why:")
        for k, why in reasons.items():
            print(f"  {k}: {why}")
    print(f"\n  cd {root}/environments/{envs[0]}")
    print("  cp terraform.tfvars.example terraform.tfvars")
    print("  terraform init && terraform plan")
    print(f"\nRead {root}/ARCHITECTURE.md for why it looks like this.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stackmason",
        description="Interview-driven Terraform repositories, secure by default.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("stacks", help="list available stacks").set_defaults(func=cmd_stacks)

    for name, fn, helptext in (
        ("new", cmd_new, "generate a repository"),
        ("plan", cmd_plan, "show what would be generated, write nothing"),
    ):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("project", help="project name, used as the directory and name prefix")
        c.add_argument("-s", "--stack", action="append", help="stack id; repeatable")
        c.add_argument(
            "-e", "--environments", default="dev,prod", help="comma separated (default: dev,prod)"
        )
        c.add_argument("-o", "--output", help="output directory (default: ./<project>)")
        c.add_argument("--answers", help="JSON file of answers, for non-interactive use")
        c.add_argument(
            "--yes", action="store_true", help="take suggestions and defaults without asking"
        )
        c.add_argument("--force", action="store_true", help="overwrite existing files")
        c.set_defaults(func=fn)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValidationError, RuntimeError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
