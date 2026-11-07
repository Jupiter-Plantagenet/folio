#!/usr/bin/env bash
# Round-5 clean-clone reproduction (Windows 11 host, Git Bash). Run while holding the build lock.
# Copies exactly the manifest source set into a fresh temp dir, builds a NEW venv from
# requirements.lock with no pip cache, npm ci with an EMPTY npm cache, builds the UI, rebuilds
# the parser image under a separate tag, and runs the full isolated PostgreSQL suite.
# Shared host infrastructure that is NOT recreated: Docker Engine, the folio-postgres container
# (disposable folio_finish_* DBs/roles are created inside it), Python 3.12.10 and Node install.
set -u
SRC=/c/Users/user/portfolio-build/folio
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
R=/c/Users/user/AppData/Local/Temp/folio-repro-$STAMP
LOG=$SRC/docs/round5-logs/s7-repro-$STAMP
mkdir -p "$R/folio" "$LOG"
PY312=/c/Users/user/AppData/Local/Programs/Python/Python312/python.exe
step() { echo "== $(date -u +%T) $*" | tee -a "$LOG/commands.log"; }

step "manifest regenerate (scripts/round5_inventory.py)"
(cd "$SRC" && PYTHONPATH=. .venv/Scripts/python.exe -B scripts/round5_inventory.py) > "$LOG/inventory.log" 2>&1
cp "$SRC/evidence/documents/delivery/round5/source-manifest.sha256" "$R/source-manifest.sha256"
step "copy manifest files only"
(cd "$SRC" && cut -c67- "$R/source-manifest.sha256" | while read -r f; do mkdir -p "$R/folio/$(dirname "$f")"; cp "$f" "$R/folio/$f"; done)
(cd "$R/folio" && sha256sum -c ../source-manifest.sha256 --quiet && echo MANIFEST_OK) > "$LOG/manifest-check.log" 2>&1
sha256sum "$R/source-manifest.sha256" >> "$LOG/manifest-check.log"

step "toolchain"
{ "$PY312" -V; node -v; npm -v; docker version --format 'docker server {{.Server.Version}}'; git --version;
  docker image inspect folio-parser:1 --format 'folio-parser:1 {{.Id}}'; cmd //c ver; } > "$LOG/toolchain.txt" 2>&1

step "venv + pip install --no-cache-dir -r requirements.lock"
"$PY312" -m venv "$R/folio/.venv" > "$LOG/venv.log" 2>&1
VPY="$R/folio/.venv/Scripts/python.exe"
"$VPY" -m pip install --no-cache-dir --disable-pip-version-check -r "$R/folio/requirements.lock" > "$LOG/pip-install.log" 2>&1; echo "pip exit $?" >> "$LOG/pip-install.log"
"$VPY" -m pip freeze --disable-pip-version-check > "$LOG/pip-freeze.txt" 2>&1
diff <(grep -v '^#' "$R/folio/requirements.lock" | grep '==' | sed 's/ .*//' | tr 'A-Z_' 'a-z-' | sort) \
     <(tr 'A-Z_' 'a-z-' < "$LOG/pip-freeze.txt" | sort) > "$LOG/lock-vs-freeze.diff"; echo "diff exit $?" >> "$LOG/lock-vs-freeze.diff"

step "npm ci (empty cache) + build"
(cd "$R/folio/web" && npm ci --cache "$R/npm-cache" --no-audit --no-fund > "$LOG/npm-ci.log" 2>&1; echo "npm ci exit $?" >> "$LOG/npm-ci.log"
 npm run build > "$LOG/npm-build.log" 2>&1; echo "build exit $?" >> "$LOG/npm-build.log"
 find dist -type f -exec sha256sum {} \; | sort -k2 > "$LOG/ui-dist.sha256")

step "parser image rebuild under separate tag (live folio-parser:1 untouched)"
(cd "$R/folio" && docker build -f ops/parser.Dockerfile -t folio-parser:repro-r5 . > "$LOG/parser-build.log" 2>&1; echo "docker build exit $?" >> "$LOG/parser-build.log"
 docker image inspect folio-parser:repro-r5 --format 'folio-parser:repro-r5 {{.Id}}' >> "$LOG/toolchain.txt" 2>&1)

step "ruff (lint) on clone"
(cd "$R/folio" && "$VPY" -m ruff check . > "$LOG/ruff.log" 2>&1; echo "ruff exit $?" >> "$LOG/ruff.log")

step "full isolated PostgreSQL suite from the clone"
(cd "$R/folio" && PYTHONPATH=. "$VPY" -B scripts/isolated_tests.py tests --tb=short -p no:cacheprovider > "$LOG/pytest-full.log" 2>&1; echo "suite exit $?" >> "$LOG/pytest-full.log")
sed -i -E 's#postgresql://[^ "]*#<dsn-redacted>#g' "$LOG"/*.log
step "done R=$R"
echo "$LOG"
