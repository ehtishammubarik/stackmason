#!/usr/bin/env bash
# Runs INSIDE a stock python image. Nothing from the repository is present:
# this tests only what `pip install stackmason` actually delivers.
# $SPEC is passed through the environment.
set -euo pipefail

# Snapshot before installing. Stock python images ship different preinstalled
# sets (3.11-slim has `packaging`, 3.12-slim does not), so an exclude list is
# wrong. Diffing is the only correct test: anything new besides stackmason itself
# is a real runtime dependency.
pip list --format=freeze 2>/dev/null | cut -d= -f1 | sort > /tmp/before.txt

pip install --quiet --no-cache-dir "$SPEC" 2>&1 | grep -viE 'notice|warning|upgrade pip' || true

echo "--- installed version"
python -c 'import importlib.metadata as m; print(m.version("stackmason"))'

echo "--- zero runtime dependencies"
pip list --format=freeze 2>/dev/null | cut -d= -f1 | sort > /tmp/after.txt
added=$(comm -13 /tmp/before.txt /tmp/after.txt | grep -vx 'stackmason' || true)
if [ -n "$added" ]; then
  echo "FAILED: install pulled in:"; echo "$added"; exit 1
fi
echo "nothing installed except stackmason itself"

echo "--- every module imports"
python - <<'PY2'
import importlib
mods = ["stackmason.cli", "stackmason.generate", "stackmason.guardrails",
        "stackmason.interview", "stackmason.stacks.registry"]
for m in mods:
    importlib.import_module(m)
print(f"{len(mods)} modules imported")
PY2

echo "--- cli entry point"
stackmason --help > /dev/null && stackmason stacks > /dev/null && echo "cli ok"

echo "--- generates a valid repository"
cat > /tmp/answers.json <<'JSON'
{"stacks":["eks","rds"],"environments":["dev","prod"],"allowed_cidrs":["10.1.0.0/16"],
 "cidr":"10.0.0.0/16","az_count":2,"k8s_version":"1.31","node_group_count":1,
 "node_instance_type":"m6i.large","node_min":2,"node_max":6,"engine":"postgres",
 "instance_class":"db.m6g.large","storage_gb":50,"spot":false,"irsa":true,
 "multi_az":false,"backup_retention_days":7,"nat_gateway_per_az":false,
 "enable_flow_logs":true}
JSON
stackmason new acme --answers /tmp/answers.json --yes -o /tmp/gen > /dev/null

python - <<'PY2'
import pathlib
root = pathlib.Path("/tmp/gen")
for f in ("environments/dev/main.tf", "environments/prod/main.tf",
          "ARCHITECTURE.md", "DECISIONS.md", ".gitignore"):
    assert (root / f).exists(), f
prod = (root / "environments/prod/main.tf").read_text()
assert "deletion_protection     = true" in prod
assert "publicly_accessible = false" in prod
assert "cluster_name" in prod
dev = (root / "environments/dev/main.tf").read_text()
assert "deletion_protection     = false" in dev
print("generated repository verified")
PY2

echo "--- no credential is ever emitted"
if grep -rInE '(password|secret|token)[[:space:]]*=[[:space:]]*"[^"$][^"]{4,}"' /tmp/gen; then
  echo "FAILED: literal credential in generated output"; exit 1
fi
echo "clean"

echo "--- guardrails block a dangerous request"
cat > /tmp/bad.json <<'JSON'
{"stacks":["rds"],"environments":["prod"],"allowed_cidrs":["0.0.0.0/0"],
 "cidr":"10.0.0.0/16","az_count":2,"engine":"postgres",
 "instance_class":"db.m6g.large","storage_gb":50,"backup_retention_days":7,
 "nat_gateway_per_az":false,"enable_flow_logs":true,"multi_az":false}
JSON
if stackmason new bad --answers /tmp/bad.json --yes -o /tmp/badgen > /dev/null 2>&1; then
  echo "FAILED: generated a config that should have been blocked"; exit 1
fi
test ! -d /tmp/badgen || { echo "FAILED: wrote files despite blocking"; exit 1; }
echo "blocked correctly, wrote nothing"
