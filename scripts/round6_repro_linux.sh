#!/usr/bin/env bash
# Round-6 Linux container reproduction (driver runs on the Windows 11 host in Git Bash).
#
#   bash scripts/round6_repro_linux.sh
#
# 1. Source set = scripts/round5_inventory.py:source_files() (git-unignored files minus evidence/,
#    evaluation-sealed/, round logs; .venv/.local/node_modules/caches are gitignored). A SHA256
#    manifest and a tarball of exactly those files are written under private C: scratch.
# 2. An acquisition bridge is used only for apt/pip/npm and Playwright browser downloads. After
#    those artifacts are acquired, the disposable postgres and both runners move to a Docker
#    internal-only network before deterministic suites/build work starts.
#    A disposable postgres:16-alpine container (random superuser
#    password passed via the environment, never written or printed; listens on 5541 in its own
#    network namespace) and a fresh python:3.12-slim runner sharing that namespace, so the
#    product's fixed 127.0.0.1:5541 DSNs reach the disposable server. Admin provisioning goes to
#    the disposable container through FOLIO_POSTGRES_CONTAINER (default stays folio-postgres).
#    The host Docker socket is mounted so the folio-parser:1 sandbox tests run for real.
#    Runner: extract, verify manifest, venv, pip install --require-hashes, freeze == lock, full
#    isolated suite.
# 3. A fresh node:26.7.0-bookworm-slim container: npm ci + npm run build, dist hashes.
# 4. summary.json under evidence/documents/repro/round6-<stamp>/; raw driver and
#    runner logs remain private under .local/repro-round6/run-<stamp>/logs/.
# This is an isolated heavy job: coordinator admission is required, no global build lock.
# Source/parser image must stay frozen for the packet. Only this run's named disposable
# containers/network are removed; source, logs, tarballs and failed attempts are retained.
set -u
export MSYS_NO_PATHCONV=1
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SRC=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PY=${FOLIO_REPRO_PYTHON:-python}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BASE=$SRC/.local/repro-round6
RUN=$BASE/run-$STAMP
WRUN=$(cygpath -m "$RUN")
LOGDIR=$RUN/logs
LOG=$LOGDIR/repro-linux-$STAMP.private.log
EVD=$SRC/evidence/documents/repro/round6-$STAMP
NET=folio-r6repro-net-$STAMP
INTNET=folio-r6repro-internal-$STAMP
PG=folio-r6repro-pg-$STAMP
RUNNER=folio-r6repro-runner-$STAMP
NODEC=folio-r6repro-node-$STAMP
PY_IMAGE=python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
PG_IMAGE=postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777
NODE_IMAGE=node:26.7.0-bookworm-slim@sha256:4db36457f406501e6f608802e5da617e5fbd0e80b75901b6a09de1ae5a667d32
PARSER_IMAGE=folio-parser:1
# The parser image must stay frozen for the whole run: pin the ID present at start.
PARSER_IMAGE_ID=${FOLIO_REPRO_PARSER_IMAGE_ID:-$(docker image inspect "$PARSER_IMAGE" --format '{{.Id}}' 2>/dev/null)}
mkdir -p "$RUN/out" "$LOGDIR" "$EVD"
: > "$RUN/commands.txt"

log() { echo "== $(date -u +%FT%TZ) $*" | tee -a "$LOG"; }
rec() { printf '%s\n' "$*" >> "$RUN/commands.txt"; }
redact() { sed -E 's#postgres(ql)?://[^[:space:]"'"'"']*#<dsn-redacted>#g'; }
disk_ok() {
  powershell -NoProfile -Command '$ErrorActionPreference="Stop"; $d=Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='"'"'C:'"'"'"; $m=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory; $r=@{disk_free=$d.FreeSpace;available_ram=$m.AvailableBytes;commit_percent=$m.PercentCommittedBytesInUse;committed=$m.CommittedBytes;commit_limit=$m.CommitLimit};$r|ConvertTo-Json -Compress;if($d.FreeSpace -lt 50000000000 -or $m.AvailableBytes -le 6442450944 -or $m.PercentCommittedBytesInUse -ge 85){exit 3}' | tee -a "$LOG"
  # Report-only unless FOLIO_RESOURCE_GATES=enforce (user direction 2026-11-04).
  if [ "${PIPESTATUS[0]}" != 0 ] && [ "${FOLIO_RESOURCE_GATES:-}" = enforce ]; then exit 3; fi
}
cleanup() {
  docker rm -f "$NODEC" "$RUNNER" "$PG" >> "$LOG" 2>&1 || true
  docker network rm "$NET" "$INTNET" >> "$LOG" 2>&1 || true
}
trap cleanup EXIT
[ "${FOLIO_REPRO_COORDINATOR_ADMITTED:-0}" = 1 ] || { echo 'Coordinator heavy-slot admission required'; exit 3; }

log "stamp=$STAMP run=$WRUN"
disk_ok

# ---- 1. manifest + tarball -------------------------------------------------------------
log "source manifest"
rec "python -B -c <round5_inventory.source_files() -> source-manifest.sha256 + files.txt>"
(cd "$SRC" && "$PY" -B - "$WRUN" <<'EOF'
import hashlib, re, sys
from pathlib import Path
sys.path.insert(0, "scripts")
import round5_inventory as inv
run = Path(sys.argv[1])
files = inv.source_files()
bad = re.compile(r"(^|/)(evidence|evaluation-sealed|\.local|\.venv|node_modules|__pycache__|\.pytest_cache|\.cache|dist)/|^docs/round\d+-logs/")
leak = [f for f in files if bad.search(f)]
assert not leak, leak[:5]
(run / "files.txt").write_bytes(("\n".join(files) + "\n").encode())
lines = [f"{hashlib.sha256(Path(f).read_bytes()).hexdigest()}  {f}" for f in files]
(run / "source-manifest.sha256").write_bytes(("\n".join(lines) + "\n").encode())
print(f"files={len(files)}")
EOF
) 2>&1 | tee -a "$LOG"
rec "tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 -cf folio-src.tar -C folio -T files.txt"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 -cf "$RUN/folio-src.tar" -C "$SRC" -T "$RUN/files.txt" 2>&1 | tee -a "$LOG"
(cd "$RUN" && sha256sum folio-src.tar source-manifest.sha256) | tee -a "$LOG" > "$RUN/out/inputs.sha256"
[ "$(tar -tf "$RUN/folio-src.tar" | wc -l)" = "$(wc -l < "$RUN/files.txt")" ] && log "tar entries == manifest entries"

# Coordinator freezes parser tag/source during this isolated packet.
disk_ok
# ---- 2. images ---------------------------------------------------------------------------
for img in "$PY_IMAGE" "$PG_IMAGE" "$NODE_IMAGE" "$PARSER_IMAGE"; do
  docker image inspect "$img" --format "$img id={{.Id}} repodigests={{json .RepoDigests}}" ; done > "$RUN/out/images.txt" 2>&1
cat "$RUN/out/images.txt" | tee -a "$LOG"
actual_parser_id=$(docker image inspect "$PARSER_IMAGE" --format '{{.Id}}' 2>/dev/null || true)
[ "$actual_parser_id" = "$PARSER_IMAGE_ID" ] || {
  log "pinned parser image mismatch expected=$PARSER_IMAGE_ID actual=${actual_parser_id:-missing}"
  exit 4
}
docker version --format 'docker client {{.Client.Version}} server {{.Server.Version}} api {{.Server.APIVersion}} os {{.Server.Os}}/{{.Server.Arch}} kernel {{.Server.KernelVersion}}' > "$RUN/out/host-docker.txt" 2>&1
docker info --format 'docker-info os={{.OperatingSystem}} ncpu={{.NCPU}} mem={{.MemTotal}}' >> "$RUN/out/host-docker.txt" 2>&1
{ cmd.exe /c ver 2>/dev/null | tr -d '\r' | grep -v '^$'; } >> "$RUN/out/host-docker.txt"
cat "$RUN/out/host-docker.txt" | tee -a "$LOG"

# ---- 3. disposable PostgreSQL on a dedicated network -----------------------------------
log "network + disposable postgres"
rec "docker network create $NET"
docker network create "$NET" >> "$LOG" 2>&1
POSTGRES_PASSWORD=$("$PY" -c "import secrets; print(secrets.token_urlsafe(32))")
export POSTGRES_PASSWORD
# Same first-boot role/database init as ops/compose.yml (ops/init.sql).
INIT_SQL=$(cygpath -m "$SRC/ops/init.sql")
rec "docker run -d --name $PG --network $NET -e PGPORT=5541 -e POSTGRES_PASSWORD <random, env-only> -v ops/init.sql:/docker-entrypoint-initdb.d/001-folio.sql:ro $PG_IMAGE"
docker run -d --name "$PG" --network "$NET" -e PGPORT=5541 -e POSTGRES_PASSWORD   -v "$INIT_SQL:/docker-entrypoint-initdb.d/001-folio.sql:ro" "$PG_IMAGE" >> "$LOG" 2>&1
unset POSTGRES_PASSWORD
for _ in $(seq 1 60); do
  docker exec "$PG" pg_isready -h 127.0.0.1 -p 5541 -q 2>/dev/null && break; sleep 2; done
docker exec "$PG" psql -U postgres -tAc "select version()" 2>&1 | tee -a "$LOG" > "$RUN/out/pg-version.txt"

# ---- 4. runner -----------------------------------------------------------------------------
cat > "$RUN/inner.sh" <<'EOF'
set -u
O=/run6/out
step() { echo "== $(date -u +%FT%TZ) $*"; }
step "apt: docker-cli (for folio.sandbox parser containers via host socket)"
apt-get update -qq > $O/apt.log 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends docker-cli fonts-dejavu-core >> $O/apt.log 2>&1
echo "apt exit $?"
{ echo "debian $(cat /etc/debian_version)"; uname -srm; python -V; dpkg-query -W docker-cli;
  docker version --format 'docker client {{.Client.Version}} -> server {{.Server.Version}}'; } > $O/runner-toolchain.txt 2>&1
cat $O/runner-toolchain.txt
step "extract + manifest check"
mkdir -p /work/folio && tar -xf /run6/folio-src.tar -C /work/folio && cd /work/folio
sha256sum -c /run6/source-manifest.sha256 --quiet && echo MANIFEST_OK
echo "files_on_disk=$(find . -type f | wc -l) manifest_lines=$(wc -l < /run6/source-manifest.sha256)"
step "venv + pip install --require-hashes"
python -m venv /work/venv
/work/venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r requirements.lock > $O/pip-install.log 2>&1
echo "pip exit $?" | tee -a $O/pip-install.log
/work/venv/bin/python -m pip --version >> $O/runner-toolchain.txt
/work/venv/bin/python -m pip freeze --disable-pip-version-check > $O/pip-freeze.txt
/work/venv/bin/python -I scripts/round6_lock_check.py requirements.lock $O/pip-freeze.txt | tee $O/lock-check.json
touch /run6/out/python-acquisition-ready
while [ ! -f /run6/out/offline-go ]; do sleep 1; done
step "offline gate active: internal-only Postgres network"
step "parser image visible through socket"
docker image inspect folio-parser:1 --format 'folio-parser:1 {{.Id}}'
step "full isolated suite"
date -u +%s > $O/suite-start
PYTHONPATH=. /work/venv/bin/python -B scripts/isolated_tests.py tests --tb=short -p no:cacheprovider > $O/pytest-full.log 2>&1
echo "suite exit $?" | tee -a $O/pytest-full.log
date -u +%s > $O/suite-end
tail -1 $O/pytest-full.log | head -1
grep -E '^=+ .*(passed|failed|error).* in ' $O/pytest-full.log | tail -1
cp evidence/documents/finish/latest.json $O/finish-latest.json 2>/dev/null
last=$(ls -td evidence/documents/finish/*/ 2>/dev/null | head -1) && cp "$last/target.log" $O/target.log 2>/dev/null
d=$(python -c "import json;print(json.load(open('evidence/documents/finish/latest.json'))['evidence'])" 2>/dev/null) && cp "$d/suite.xml" $O/suite.xml
step "runner done"
EOF
disk_ok
log "Node acquisition ($NODE_IMAGE): npm ci + pinned Playwright browser cache"
rec "docker run --name $NODEC --network $NET -v <run>:/run6 $NODE_IMAGE: npm ci + playwright install chromium, wait for offline gate, then build"
docker run --name "$NODEC" --network "$NET" -v "$WRUN:/run6" "$NODE_IMAGE" bash -c '
set -u; O=/run6/out
{ echo "debian $(cat /etc/debian_version)"; uname -srm; echo "node $(node -v)"; echo "npm $(npm -v)"; } > $O/node-toolchain.txt
mkdir -p /work/folio && tar -xf /run6/folio-src.tar -C /work/folio && cd /work/folio/web
sha256sum package-lock.json > $O/package-lock.sha256
export PLAYWRIGHT_BROWSERS_PATH=/run6/browser-cache
npm ci --no-audit --no-fund --cache /tmp/npm-cache > $O/npm-ci.log 2>&1 || exit $?
echo "npm ci exit 0" | tee -a $O/npm-ci.log
npx playwright install chromium > $O/playwright-browser-install.log 2>&1 || exit $?
echo "browser install exit 0" | tee -a $O/playwright-browser-install.log
touch $O/node-acquisition-ready
while [ ! -f $O/offline-go ]; do sleep 1; done
npm run build > $O/npm-build.log 2>&1; echo "build exit $?" | tee -a $O/npm-build.log
find dist -type f -exec sha256sum {} \; | sort -k2 > $O/ui-dist.sha256' > "$LOGDIR/node-acquisition.private.log" 2>&1 &
NODE_DOCKER_PID=$!

for _ in $(seq 1 900); do
  [ -f "$RUN/out/node-acquisition-ready" ] && break
  kill -0 "$NODE_DOCKER_PID" 2>/dev/null || { log "Node package/browser acquisition failed"; exit 5; }
  sleep 2
done
[ -f "$RUN/out/node-acquisition-ready" ] || { log "Node acquisition gate timed out"; exit 5; }

log "runner ($PY_IMAGE): apt/pip acquisition, then full suite after offline gate"
disk_ok
rec "docker run --name $RUNNER --network container:$PG -v /var/run/docker.sock:/var/run/docker.sock -v <run>:/run6 -e FOLIO_POSTGRES_CONTAINER=$PG $PY_IMAGE bash /run6/inner.sh"
docker run --name "$RUNNER" --network "container:$PG" -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$WRUN:/run6" -e FOLIO_POSTGRES_CONTAINER="$PG" -e PIP_NO_CACHE_DIR=1 "$PY_IMAGE" bash /run6/inner.sh \
  > "$LOGDIR/python-runner.private.log" 2>&1 &
RUNNER_DOCKER_PID=$!

for _ in $(seq 1 1800); do
  [ -f "$RUN/out/python-acquisition-ready" ] && break
  kill -0 "$RUNNER_DOCKER_PID" 2>/dev/null || { log "Python acquisition stopped before offline gate"; exit 5; }
  kill -0 "$NODE_DOCKER_PID" 2>/dev/null || { log "Node acquisition stopped before offline gate"; exit 5; }
  sleep 2
done
[ -f "$RUN/out/python-acquisition-ready" ] || { log "Python acquisition gate timed out"; exit 5; }

# Both runners have completed package/browser acquisition. Move application/test traffic to
# a Docker internal network before either deterministic phase begins.
log "switching disposable resources to internal-only network"
rec "docker network create --internal $INTNET; connect postgres and node runner; disconnect acquisition bridge"
docker network create --internal "$INTNET" >> "$LOG" 2>&1
docker network connect "$INTNET" "$PG" >> "$LOG" 2>&1
docker network disconnect "$NET" "$PG" >> "$LOG" 2>&1
docker network connect "$INTNET" "$NODEC" >> "$LOG" 2>&1
docker network disconnect "$NET" "$NODEC" >> "$LOG" 2>&1
touch "$RUN/out/offline-go"

wait "$RUNNER_DOCKER_PID"; RUNNER_EXIT=$?
wait "$NODE_DOCKER_PID"; NODE_EXIT=$?
redact < "$LOGDIR/python-runner.private.log" >> "$LOG"
redact < "$LOGDIR/node-acquisition.private.log" >> "$LOG"
docker inspect "$RUNNER" --format 'runner container exit {{.State.ExitCode}}' | tee -a "$LOG"
docker inspect "$NODEC" --format 'node container exit {{.State.ExitCode}}' | tee -a "$LOG"

docker stop "$PG" >> "$LOG" 2>&1

# ---- 6. compact logs + summary --------------------------------------------------------------
for f in pytest-full.log pip-install.log npm-ci.log npm-build.log apt.log playwright-browser-install.log; do
  [ -f "$RUN/out/$f" ] && redact < "$RUN/out/$f" > "$LOGDIR/repro-linux-$STAMP-$f"; done
cp "$RUN/commands.txt" "$RUN/out/commands.txt"
"$PY" -B - "$WRUN" "$(cygpath -m "$EVD")" "$STAMP" "$NET" "$PG" "$RUNNER" "$NODEC" "$INTNET" "$RUNNER_EXIT" "$NODE_EXIT" <<'EOF' 2>&1 | tee -a "$LOG"
import json, re, sys, xml.etree.ElementTree as ET
from pathlib import Path
run, evd, stamp, net, pg, runner, nodec, intnet = Path(sys.argv[1]), Path(sys.argv[2]), *sys.argv[3:9]
runner_exit, node_exit = int(sys.argv[9]), int(sys.argv[10])
o = run / "out"
rd = lambda n: (o / n).read_text(encoding="utf-8", errors="replace") if (o / n).exists() else ""
counts = {}
m = re.findall(r"^=+ (.*) in [\d.]+s", rd("pytest-full.log"), re.M)
if m:
    for part in m[-1].split(", "):
        n, k = part.split(" ", 1)
        counts[k] = int(n)
junit = {}
if (o / "suite.xml").exists():
    root = ET.parse(o / "suite.xml").getroot()
    s = root if root.tag == "testsuite" else root.find("testsuite")
    junit = {k: int(s.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
failed = re.findall(r"^(?:FAILED|ERROR) (\S+)", rd("pytest-full.log"), re.M)
summary = {
    "stamp": stamp,
    "kind": "round-6 Linux container reproduction (DOC-T-REPRO)",
    "host_caveat": "All containers ran on the same Windows 11 host under the same Docker Desktop "
                   "(WSL2 Linux VM) engine as the live stack. This is a fresh Linux userland and a "
                   "fresh dependency install, NOT a separate physical or virtual host.",
    "acquisition": "online: apt (docker-cli), PyPI (pip --require-hashes, no cache), npm registry "
                   "(npm ci, empty cache) and locked Playwright Chromium revision were acquired "
                   "during the run; deterministic Python tests and UI build start only after both "
                   "containers move to a Docker internal-only network. Base images were already "
                   "present locally and are identified by ID/RepoDigest below.",
    "docker_socket": "host Docker socket mounted into the runner so folio.sandbox can start "
                     "folio-parser:1 parser containers (the host's existing image, not rebuilt here). "
                     "DB admin went to the disposable container via FOLIO_POSTGRES_CONTAINER.",
    "images": rd("images.txt").splitlines(),
    "host_docker": rd("host-docker.txt").splitlines(),
    "postgres": rd("pg-version.txt").strip(),
    "runner_toolchain": rd("runner-toolchain.txt").splitlines(),
    "node_toolchain": rd("node-toolchain.txt").splitlines(),
    "inputs_sha256": rd("inputs.sha256").splitlines(),
    "manifest_files": len((run / "files.txt").read_text().splitlines()),
    "lock_check": json.loads(rd("lock-check.json") or "null"),
    "pip_install_exit": (re.findall(r"pip exit (\d+)", rd("pip-install.log")) or [None])[-1],
    "suite": {"summary_line_counts": counts, "junit": junit, "failed_or_error": failed,
              "exit": (re.findall(r"suite exit (\d+)", rd("pytest-full.log")) or [None])[-1],
              "seconds": (int(rd("suite-end") or 0) - int(rd("suite-start") or 0)) or None},
    "ui": {"npm_ci_exit": (re.findall(r"npm ci exit (\d+)", rd("npm-ci.log")) or [None])[-1],
           "build_exit": (re.findall(r"build exit (\d+)", rd("npm-build.log")) or [None])[-1],
           "container_exit": node_exit,
           "package_lock": rd("package-lock.sha256").strip(),
           "dist_sha256": rd("ui-dist.sha256").splitlines()},
    "runner_container_exit": runner_exit,
    "browser_workflow": {
        "selection": "web/live-tests/live-journeys.spec.ts: J5 all fourteen screens at 1440/1024/390",
        "config": "web/repro-14.config.ts",
        "offline_guard": "FOLIO_REPRO_OFFLINE=1 permits only configured loopback UI origin and data/blob assets",
        "required_artifacts": ["j5-screens.json", "j5-network-guard.json", "14x3 route observations", "28 screenshots at 1440/390"],
        "execution": "not run by this driver revision; app-backed browser phase awaits a separately admitted integrated run"
    },
    "commands": rd("commands.txt").splitlines(),
    "disposable_resources_removed_on_exit": {"acquisition_network": net, "internal_network": intnet, "postgres_container": pg,
                           "runner_container": runner, "node_container": nodec,
                           "scratch": str(run)},
 "logs": f".local/repro-round6/run-{stamp}/logs/*",
}
evd.mkdir(parents=True, exist_ok=True)
text = re.sub(r"postgres(ql)?://\S+", "<dsn-redacted>", json.dumps(summary, indent=1))
(evd / "summary.json").write_text(text, encoding="utf-8")
print(json.dumps({"suite": summary["suite"], "ui": {k: v for k, v in summary["ui"].items() if k != "dist_sha256"},
                  "lock_check": summary["lock_check"]}, indent=1))
passed = summary['suite']['exit'] == '0' and not junit.get('failures', 1) and not junit.get('errors', 1)
passed = passed and summary['ui']['build_exit'] == '0' and summary['ui']['npm_ci_exit'] == '0'
passed = passed and (re.findall(r"browser install exit (\d+)", rd("playwright-browser-install.log")) or [None])[-1] == '0'
passed = passed and runner_exit == 0 and node_exit == 0
passed = passed and (summary['lock_check'] or {}).get('equal') is True
sys.exit(0 if passed else 1)
EOF
result=${PIPESTATUS[0]}
log "done; evidence=$EVD"
exit "$result"
