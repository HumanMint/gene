#!/usr/bin/env bash
# Batched maintainer release: Gene control plane + cogs, then Fri. Run as root on the host:
#   gene-batch-release.sh <phase> <full gene sha> <full fri sha>
# Phases, in order: baseline stage rehearse drain gene fri evals verify-off resume cleanup.
# Also: status, rollback-gene, resume-old. Each phase checks the journal of the ones before it.
# Prints no secrets: env files are passed to Docker or copied, never read into output.
set -Eeuo pipefail
umask 077

PHASE=${1:?phase}; G=${2:?gene sha}; F=${3:?fri sha}
[[ $G =~ ^[0-9a-f]{40}$ && $F =~ ^[0-9a-f]{40}$ ]] || { echo 'Full 40-character SHAs required' >&2; exit 2; }
OLD_G=f66700140d91dc80df0bca8ae29ca671ca67f80e
OLD_F=6a1d410cbb7f04f36cd5be629cc48b05a221d653
ROOT=/srv/gene
FRI_ROOT=/srv/fri
DB=$ROOT/data/controller/gene.sqlite3
GENE_HEALTH=$ROOT/data/red/gene-health.json
FRI_HEALTH=$ROOT/data/fri/fri-health.json
STAGE=$ROOT/staging/batch-$G
JOURNAL=$STAGE/journal

log() { echo "== $*"; }
die() {
  echo "FAIL: $*" >&2
  # exit skips the ERR trap, so a refusal mid-switch must start the rollback itself.
  if [[ ${SWITCHING:-} == 1 ]]; then on_gene_error; fi
  exit 1
}
mark() { echo "$1" >> "$JOURNAL"; sync "$JOURNAL"; }
has() { [[ -f $JOURNAL ]] && grep -qx "$1" "$JOURNAL"; }
need() { for step in "$@"; do has "$step" || die "phase needs '$step' first"; done; }
backup_dir() { cat "$STAGE/backup-path"; }

compose_gene() {
  # Compose files are identical between the two releases (stage refuses an ops diff), so use
  # whichever source tree is currently in place.
  local file=$ROOT/source/ops/compose.yml
  [[ -f $file ]] || file=$(backup_dir)/installed-source/ops/compose.yml
  docker compose --project-name gene --env-file "$ROOT/release.env" -f "$file" "$@"
}

# Python in the running controller container: UID 10001, the controller's code and /data.
ctl_py() { docker exec -i gene-controller-1 python - "$@"; }

settings_transition() {  # from_active from_previous to_active to_previous action
  ctl_py "$@" <<'PY'
import sqlite3, sys, time
from_a, from_p, to_a, to_p, action = sys.argv[1:6]
db = sqlite3.connect('/data/gene.sqlite3', timeout=30, isolation_level=None)
keys = "SELECT key,value FROM settings WHERE key IN ('active_sha','previous_sha')"
db.execute('BEGIN IMMEDIATE')
try:
    current = dict(db.execute(keys))
    if current == {'active_sha': to_a, 'previous_sha': to_p}:
        db.execute('ROLLBACK')
        print('already recorded', current)
        sys.exit(0)
    if current != {'active_sha': from_a, 'previous_sha': from_p}:
        raise SystemExit('unexpected release settings: '+repr(current))
    for key, value, expected in (('previous_sha', to_p, from_p), ('active_sha', to_a, from_a)):
        if db.execute('UPDATE settings SET value=? WHERE key=? AND value=?', (value, key, expected)).rowcount != 1:
            raise SystemExit('setting not updated: '+key)
    db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
               (time.time(), 'maintainer:claude', action, to_a,
                'Chat-authorized batched maintainer release; from '+from_a))
    db.execute('COMMIT')
except BaseException:
    if db.in_transaction:
        db.execute('ROLLBACK')
    raise
print('recorded', dict(db.execute(keys)))
PY
}

set_paused() {  # true|false
  ctl_py "$1" <<'PY'
import sys
from pathlib import Path
from gene.store import Store
Store(Path('/data/gene.sqlite3')).set_paused(sys.argv[1] == 'true', 'maintainer:claude')
print('paused =', sys.argv[1])
PY
}

audit() {  # action target
  ctl_py "$@" <<'PY'
import sqlite3, sys, time
with sqlite3.connect('/data/gene.sqlite3', timeout=30) as db:
    db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
               (time.time(), 'maintainer:claude', sys.argv[1], sys.argv[2], 'Chat-authorized batched maintainer release'))
PY
}

state() {  # Read-only snapshot of what is running and recorded; no row contents.
  python3 - "$DB" "$GENE_HEALTH" "$FRI_HEALTH" "$ROOT/release.env" "$FRI_ROOT/release.json" <<'PY'
import json, sqlite3, sys, time
db, gene_health, fri_health, release_env, fri_record = sys.argv[1:6]
def load(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}
with sqlite3.connect('file:'+db+'?mode=ro', uri=True, timeout=30) as conn:
    settings = dict(conn.execute("SELECT key,value FROM settings WHERE key IN "
                                 "('active_sha','previous_sha','paused','fri_active_sha')"))
    busy = conn.execute("SELECT count(*) FROM jobs WHERE state IN ('building','deploying')").fetchone()[0]
env = dict(line.split('=', 1) for line in open(release_env).read().splitlines() if '=' in line)
gene, fri = load(gene_health), load(fri_health)
beat = fri.get('agent_heartbeat') or {}
print(json.dumps({
    'settings': settings, 'busy_jobs': busy, 'release_env_sha': env.get('GENE_RELEASE_SHA'),
    'runtime_image': env.get('GENE_RUNTIME_IMAGE'),
    'gene': {k: gene.get(k) for k in ('sha', 'guild_id', 'presence', 'guild_connected', 'controls_running')}
            | {'age': round(time.time()-gene.get('at', 0), 1)},
    'fri': {k: fri.get(k) for k in ('sha', 'guild_id', 'guild_connected')}
           | {'running': beat.get('running'), 'failed': beat.get('failed'), 'age': round(time.time()-fri.get('at', 0), 1)},
    'fri_record_sha': load(fri_record).get('sha')}))
PY
}

probe_gene() {  # sha started_epoch: 6 consecutive good 5 s samples within 180 s
  python3 - "$1" "$2" "$STAGE/baseline.json" "$GENE_HEALTH" "$FRI_HEALTH" <<'PY'
import json, subprocess, sys, time, urllib.request
sha, after, baseline, gene_path, fri_path = sys.argv[1], float(sys.argv[2]), *sys.argv[3:6]
base = json.load(open(baseline))
deadline, good, reason = time.monotonic()+180, 0, ''
while time.monotonic() < deadline:
    try:
        gene, fri = json.load(open(gene_path)), json.load(open(fri_path))
        health = subprocess.run(['docker', 'inspect', '-f', '{{.State.Health.Status}}', 'gene-controller-1'],
                                capture_output=True, text=True).stdout.strip()
        with urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=5) as response:
            code = response.status
        beat = fri['agent_heartbeat']
        checks = {'controller_healthy': health == 'healthy', 'controller_http': code == 200,
                  'sha': gene['sha'] == sha, 'guild_connected': gene['guild_connected'] is True,
                  'guild_id': str(gene['guild_id']) == base['gene']['guild_id'],
                  'presence': gene['presence'] == base['gene']['presence'],
                  'controls_running': gene['controls_running'] is True,
                  'fresh': gene['at'] > after and time.time()-gene['at'] < 25,
                  'fri_alive': beat['running'] and not beat['failed'] and time.time()-fri['at'] < 30}
        reason = ','.join(k for k, v in checks.items() if not v)
        good = 0 if reason else good+1
    except Exception as exc:  # Missing or partial files count as unhealthy samples.
        good, reason = 0, type(exc).__name__
    if good >= 6:
        print('gene healthy at', sha)
        sys.exit(0)
    time.sleep(5)
sys.exit('gene unhealthy: '+reason)
PY
}

probe_fri() {  # sha
  python3 - "$1" "$STAGE/baseline.json" "$FRI_HEALTH" <<'PY'
import json, sys, time
sha, baseline, path = sys.argv[1:4]
base = json.load(open(baseline))
deadline, good, reason = time.monotonic()+120, 0, ''
while time.monotonic() < deadline:
    try:
        fri = json.load(open(path))
        beat = fri['agent_heartbeat']
        checks = {'sha': fri['sha'] == sha, 'guild_connected': fri['guild_connected'] is True,
                  'guild_id': str(fri['guild_id']) == base['fri']['guild_id'],
                  'running': beat['running'] is True, 'not_failed': not beat['failed'],
                  'fresh': time.time()-fri['at'] < 30}
        reason = ','.join(k for k, v in checks.items() if not v)
        good = 0 if reason else good+1
    except Exception as exc:
        good, reason = 0, type(exc).__name__
    if good >= 4:
        print('fri healthy at', sha, '; health keys:', sorted(fri), '; heartbeat keys:', sorted(beat))
        sys.exit(0)
    time.sleep(5)
sys.exit('fri unhealthy: '+reason)
PY
}

audio_tx() {  # Bytes the audio node has sent; Lavalink streams voice straight to Discord.
  docker exec gene-audio-node-1 cat /proc/net/dev | python3 -c '
import sys
rows = [line.split(":", 1) for line in sys.stdin if ":" in line]
print(sum(int(rest.split()[8]) for name, rest in rows if name.strip() != "lo"))'
}

quiet() {  # Wait up to 30 min for no audio playback, then refuse.
  local tries=0 first second
  while (( tries < 60 )); do
    first=$(audio_tx); sleep 15; second=$(audio_tx)
    if (( second - first < 60000 )); then log "audio quiet ($(( second - first )) bytes in 15 s)"; return 0; fi
    log "audio is streaming ($(( second - first )) bytes in 15 s); waiting"
    tries=$(( tries + 1 )); sleep 15
  done
  die 'Gene is still playing audio after 30 minutes; not restarting Red'
}

baseline() {
  mkdir -p "$STAGE"; chmod 700 "$ROOT/staging" "$STAGE"
  local snapshot; snapshot=$(state)
  python3 - "$snapshot" "$OLD_G" "$OLD_F" "$STAGE/baseline.json" <<'PY'
import json, os, sys
snap, old_g, old_f, path = json.loads(sys.argv[1]), *sys.argv[2:5]
problems = [name for name, ok in {
    'active_sha': snap['settings'].get('active_sha') == old_g,
    'release_env': snap['release_env_sha'] == old_g,
    'gene_health_sha': snap['gene']['sha'] == old_g,
    'fri_health_sha': snap['fri']['sha'] == old_f,
    'fri_record': snap['fri_record_sha'] == old_f,
    'fri_active_sha': snap['settings'].get('fri_active_sha') in (None, old_f),
    'previous_sha': len(snap['settings'].get('previous_sha') or '') == 40}.items() if not ok]
if problems:
    sys.exit('baseline does not match the expected live release: '+', '.join(problems))
snap['gene']['guild_id'] = str(snap['gene']['guild_id'])
snap['fri']['guild_id'] = str(snap['fri']['guild_id'])
if os.path.exists(path):
    print('baseline already recorded; current state still matches it')
else:
    with open(path, 'x') as out:
        json.dump(snap, out)
print(json.dumps({k: snap[k] for k in ('settings', 'busy_jobs', 'gene', 'fri')}))
PY
  has baseline || mark baseline
}

stage() {
  need baseline
  has staged && { log 'already staged'; return; }
  for name in gene fri; do
    [[ -f $STAGE/$name.tar && -f $STAGE/$name.tar.sha256 ]] || die "upload $STAGE/$name.tar and $name.tar.sha256 first"
    (cd "$STAGE" && sha256sum -c "$name.tar.sha256")
    [[ ! -e $STAGE/$name-src ]] || die "$STAGE/$name-src exists from an earlier attempt; inspect and remove it"
    mkdir -m 700 "$STAGE/$name-src"
    tar -x --no-same-owner -f "$STAGE/$name.tar" -C "$STAGE/$name-src"
    chmod -R a+rX,go-w "$STAGE/$name-src"
  done
  echo "$G" > "$STAGE/gene-src/.release-sha"
  for file in pyproject.toml uv.lock; do
    cmp -s "$ROOT/source/$file" "$STAGE/gene-src/$file" || die "$file changed: the host venv needs its own plan"
  done
  for file in ops/compose.yml ops/fri-compose.yml ops/gene-worker.service ops/gene-deployer.service; do
    cmp -s "$ROOT/source/$file" "$STAGE/gene-src/$file" || { diff -u "$ROOT/source/$file" "$STAGE/gene-src/$file" || true
      [[ ${ALLOW_OPS_DIFF:-} == 1 ]] || die "$file changed; review the diff, then rerun with ALLOW_OPS_DIFF=1"; }
  done
  log 'building images'
  docker build -q -f "$STAGE/gene-src/ops/Dockerfile" --target runtime \
    --label org.opencontainers.image.revision="$G" -t "gene-runtime:$G" "$STAGE/gene-src"
  docker build -q -f "$STAGE/gene-src/ops/Dockerfile" --target worker \
    --label org.opencontainers.image.revision="$G" -t "gene-worker:$G" "$STAGE/gene-src"
  docker build -q -f "$STAGE/fri-src/ops/Dockerfile" --build-arg FRI_RELEASE_SHA="$F" \
    -t "fri-runtime:$F" "$STAGE/fri-src"
  for image in "gene-runtime:$G" "gene-worker:$G" "fri-runtime:$F"; do
    label=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
    [[ $label == "${image#*:}" ]] || die "$image label is $label"
  done
  log 'isolated import checks'
  local isolated=(docker run --rm --network none --user 10001:10001 --read-only --tmpfs /tmp)
  "${isolated[@]}" "gene-runtime:$G" python -c 'import gene.api, gene.store, gene.runtime, gene.fri_release, gene.fri_reply_checks, gene.deploy, gene.provenance_downgrade; print("gene imports ok")'
  "${isolated[@]}" "gene-worker:$G" codex --version
  "${isolated[@]}" "fri-runtime:$F" python -c 'import fri.fri, fri.fri_eval, fri.fri_request_eval; print("fri imports ok")'
  log 'controller contract on synthetic state'
  local contract
  contract=$("${isolated[@]}" --tmpfs /data:uid=10001,gid=10001 -e GENE_DB=/data/synthetic.sqlite3 \
    -e GENE_RUNTIME_TOKEN=synthetic-runtime-token-0000000000000000 -e GENE_WORKER_TOKEN=synthetic-worker-token-00000000000000000 \
    -e GENE_DEPLOY_TOKEN=synthetic-deploy-token-00000000000000000 -e GENE_FRI_TOKEN=synthetic-fri-token-0000000000000000000000 \
    "gene-runtime:$G" python -c '
import json
from fastapi.testclient import TestClient
from gene.api import create_app
client = TestClient(create_app())
assert client.get("/health").status_code == 200
response = client.get("/fri/contract", headers={"Authorization": "Bearer synthetic-fri-token-0000000000000000000000"})
assert response.status_code == 200, response.status_code
print(json.dumps(response.json()))')
  echo "$contract"
  "${isolated[@]}" "fri-runtime:$F" python -c 'import json, sys; from fri.fri_protocol import compatible
assert compatible(json.loads(sys.argv[1])), "Fri rejects the candidate controller contract"; print("fri accepts contract")' "$contract"
  python3 - "$ROOT" <<'PY'
import shutil, subprocess, sys
root = sys.argv[1]
size = lambda path: int(subprocess.run(['du', '-sb', path], capture_output=True, text=True).stdout.split()[0])
needed = 2*size(root+'/data/controller') + size(root+'/data/red') + size(root+'/data/fri') + size(root+'/source') + 3*2**30
free = shutil.disk_usage('/').free
print(f'disk: {free/2**30:.1f} GiB free, {needed/2**30:.1f} GiB needed for backups and headroom')
if free < needed:
    sys.exit('not enough disk for the backups')
PY
  mark staged
}

rehearse() {  # Migrate copies of the live DB with the new code, then start the old code on them.
  need staged
  has rehearsed && { log 'already rehearsed'; return; }
  local test=$STAGE/dbtest
  [[ ! -e $test ]] || die "$test exists; remove it first"
  mkdir -m 700 "$test"
  for copy in forward downgrade; do
    python3 -c 'import sqlite3, sys
src = sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close()' "$DB" "$test/$copy.sqlite3"
  done
  chown -R 10001:10001 "$test"
  trap 'rm -rf "$test"' EXIT  # The copies hold member data; never leave them behind.
  local run=(docker run --rm --network none --user 10001:10001 --read-only --tmpfs /tmp
             --env-file "$ROOT/secrets/controller.env" -v "$test:/data")
  local start='import sys; from fastapi.testclient import TestClient; from gene.api import create_app
client = TestClient(create_app()); assert client.get("/health").status_code == 200
print(sys.argv[1], "controller starts; status keys:", sorted(client.app.state.store.status()))'
  "${run[@]}" -e GENE_DB=/data/forward.sqlite3 "gene-runtime:$G" python -c "$start" new
  "${run[@]}" -e GENE_DB=/data/forward.sqlite3 "gene-runtime:$OLD_G" python -c "$start" old-on-migrated
  "${run[@]}" -e GENE_DB=/data/forward.sqlite3 "gene-runtime:$OLD_G" python -c '
import sqlite3, uuid
from gene.config import Settings
from gene.store import Store
settings = Settings.from_env()
store = Store(settings.db)
try:
    job = store.request(settings.guild_id, "0", "rehearsal:"+uuid.uuid4().hex, "", "rehearsal write")
    print("old code writes a request on the migrated schema:", job["state"] if isinstance(job, dict) else "ok")
except sqlite3.Error:
    raise
except Exception as exc:
    print("old request write refused by validation, not schema:", type(exc).__name__)'
  "${run[@]}" -e GENE_DB=/data/downgrade.sqlite3 "gene-runtime:$G" python -c "$start" new
  "${run[@]}" "gene-runtime:$G" python -m gene.fri_reply_checks purge /data/downgrade.sqlite3
  if "${run[@]}" "gene-runtime:$G" python -c 'import gene.profile_downgrade' 2>/dev/null; then
    "${run[@]}" "gene-runtime:$G" python -m gene.profile_downgrade /data/downgrade.sqlite3
  fi
  "${run[@]}" -e GENE_DB=/data/downgrade.sqlite3 "gene-runtime:$OLD_G" python -c "$start" old-after-downgrade
  rm -rf "$test"
  mark rehearsed
}

drain() {  # Pause new work, let current work finish, then stop the worker and deployer.
  need baseline
  has drained && { log 'already drained'; return; }
  if [[ $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["settings"]["paused"])' "$STAGE/baseline.json") == false ]]; then
    has paused_by_release || { set_paused true; mark paused_by_release; }
  fi
  local waited=0
  until python3 -c 'import json,sys; sys.exit(json.loads(sys.argv[1])["busy_jobs"] != 0)' "$(state)" \
        && [[ ! -e $ROOT/deploy-result.json ]]; do
    (( waited < 1800 )) || die 'work is still building or deploying after 30 minutes; development stays paused'
    log 'waiting for current work to finish'; sleep 30; waited=$(( waited + 30 ))
  done
  systemctl stop gene-worker gene-deployer
  mark units_stopped
  ! systemctl is-active -q gene-worker && ! systemctl is-active -q gene-deployer || die 'units still active'
  python3 -c 'import json,sys; sys.exit(json.loads(sys.argv[1])["busy_jobs"] != 0)' "$(state)" || die 'a job started'
  [[ ! -e $ROOT/deploy-result.json ]] || die 'a deployment receipt appeared'
  mark drained
}

on_gene_error() {
  trap - ERR
  SWITCHING=0
  echo 'FAIL: Gene switch failed; rolling back' >&2
  rollback_gene || echo 'FAIL: automatic rollback did not finish; operator recovery required' >&2
  exit 1
}

gene() {
  need staged rehearsed drained
  has gene_recorded && { log 'Gene already switched and recorded'; return; }
  has gene_attempted && die 'an earlier Gene attempt is in the journal; run rollback-gene or status first'
  quiet
  local B; B=$ROOT/backups/maintainer-batch-$G-$(date +%s)
  mkdir -m 700 "$B"
  echo "$B" > "$STAGE/backup-path"
  mark gene_attempted
  SWITCHING=1
  trap on_gene_error ERR
  python3 -c 'import sqlite3, sys
src = sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close()' "$DB" "$B/controller.sqlite3"
  cp -p "$ROOT/release.env" "$B/release.env"
  cp -p "$ROOT/secrets/worker.env" "$B/worker.env"
  cp -p "$FRI_ROOT/release.env" "$B/fri-release.env"
  cp -p "$FRI_ROOT/release.json" "$B/fri-release.json"
  mark backed_up
  compose_gene stop red
  mark red_stopped
  tar -C "$ROOT/data" -czf "$B/red.tar.gz" red
  chmod 600 "$B"/*
  if [[ -e $ROOT/releases/$G ]]; then
    diff -r "$ROOT/releases/$G/cogs" "$STAGE/gene-src/cogs" >/dev/null || die "releases/$G exists with different cogs"
  else
    mkdir -m 755 "$ROOT/releases/$G"
    cp -r "$STAGE/gene-src/cogs" "$ROOT/releases/$G/cogs"
    find "$ROOT/releases/$G" -type d -exec chmod 755 {} + ; find "$ROOT/releases/$G" -type f -exec chmod 444 {} +
  fi
  mark cogs_installed
  mv "$ROOT/source" "$B/installed-source"
  mark source_moved
  mv "$STAGE/gene-src" "$ROOT/source"
  mark source_installed
  cp -p "$ROOT/release.env" "$ROOT/release.env.tmp"
  printf 'GENE_RELEASE_SHA=%s\nGENE_ROOT=%s\nGENE_RUNTIME_IMAGE=gene-runtime:%s\n' "$G" "$ROOT" "$G" > "$ROOT/release.env.tmp"
  mv "$ROOT/release.env.tmp" "$ROOT/release.env"
  mark env_pinned
  sed -i "s|^GENE_WORKER_IMAGE=.*|GENE_WORKER_IMAGE=gene-worker:$G|" "$ROOT/secrets/worker.env"
  mark worker_pinned
  [[ $(grep -Fxc -- "GENE_WORKER_IMAGE=gene-worker:$G" "$ROOT/secrets/worker.env") == 1 ]] || die 'worker image not pinned'
  [[ $(stat -c %a:%u:%g "$ROOT/secrets/worker.env") == $(stat -c %a:%u:%g "$B/worker.env") ]] || die 'worker.env mode changed'
  local started; started=$(date +%s)
  mark gene_started
  compose_gene up -d --no-deps --force-recreate controller red
  probe_gene "$G" "$started"
  settings_transition "$OLD_G" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["settings"]["previous_sha"])' "$STAGE/baseline.json")" "$G" "$OLD_G" maintainer_deployed
  mark gene_recorded
  trap - ERR
  SWITCHING=0
  state
}

restore_old_fri() {  # Before any Gene downgrade, put back the Fri that ran against the old controller.
  local running; running=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sha",""))' "$FRI_ROOT/release.json")
  [[ $running == "$OLD_F" ]] && return 0
  log "restoring Fri $OLD_F first"
  "$ROOT/venv/bin/python" -m gene.fri_release "$OLD_F"
  probe_fri "$OLD_F"
  mark fri_restored
}

rollback_gene() {
  trap - ERR
  set +e
  local B; B=$(backup_dir)
  local prev; prev=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["settings"]["previous_sha"])' "$STAGE/baseline.json")
  restore_old_fri || { echo 'FAIL: Fri could not be restored; stopping before touching Gene' >&2; return 1; }
  if has gene_started; then
    compose_gene stop controller red
    local run=(docker run --rm --network none --user 10001:10001 --read-only --tmpfs /tmp --env-file "$ROOT/secrets/controller.env"
               -e GENE_DB=/data/gene.sqlite3 -v "$ROOT/data/controller:/data" "gene-runtime:$G")
    "${run[@]}" python -m gene.fri_reply_checks purge /data/gene.sqlite3 || { echo 'FAIL: reply-check purge' >&2; return 1; }
    if "${run[@]}" python -c 'import gene.profile_downgrade' 2>/dev/null; then
      "${run[@]}" python -m gene.profile_downgrade /data/gene.sqlite3 || { echo 'FAIL: profile downgrade' >&2; return 1; }
    fi
  fi
  if has worker_pinned; then cp -p "$B/worker.env" "$ROOT/secrets/worker.env" || return 1; fi
  if has env_pinned; then cp -p "$B/release.env" "$ROOT/release.env" || return 1; fi
  if has source_installed; then mv "$ROOT/source" "$B/failed-source" || return 1; fi
  if has source_moved; then mv "$B/installed-source" "$ROOT/source" || return 1; fi
  if has red_stopped; then
    local started; started=$(date +%s)
    compose_gene up -d --no-deps --force-recreate controller red || return 1
    probe_gene "$OLD_G" "$started" || { echo 'FAIL: old Gene is not healthy; operator recovery required' >&2; return 1; }
  fi
  if has gene_recorded; then
    settings_transition "$G" "$OLD_G" "$OLD_G" "$prev" maintainer_rollback || return 1
  fi
  mark gene_rolled_back
  log 'Gene rolled back. Workers stay stopped and development stays paused: run resume-old when settled.'
  state
}

fri() {
  need gene_recorded
  has fri_switched && { log 'Fri already switched'; return; }
  mark fri_attempted
  if ! "$ROOT/venv/bin/python" -m gene.fri_release "$F"; then
    echo 'FAIL: Fri release failed; state for reconciliation:' >&2
    state
    grep -E '^FRI_RELEASE_SHA=' "$FRI_ROOT/release.env" || true
    local latest; latest=$(ls -dt "$FRI_ROOT"/backups/*-"$F" 2>/dev/null | head -1)
    [[ -n $latest && -f $latest/failure.json ]] && cat "$latest/failure.json"
    die 'workers stay stopped until Fri is reconciled'
  fi
  mark fri_switched
  probe_fri "$F"
}

evals() {
  need fri_switched
  local suite
  for suite in requests sass controls; do
    log "eval suite $suite"
    docker exec fri-runtime-1 python -m fri.fri_eval --suite "$suite" > "$STAGE/eval-$suite.jsonl" 2>&1 \
      && echo "suite $suite passed" || echo "suite $suite FAILED (exit $?)"
    python3 - "$STAGE/eval-$suite.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    try:
        row = json.loads(line)
    except ValueError:
        print('  ', line.rstrip()[:300])
        continue
    print('  ', row.get('case'), row.get('status'), row.get('response_kind'), 'passed='+str(row.get('passed')),
          (row.get('error') or '')[:200], '|', (row.get('reply') or '')[:160].replace('\n', ' '))
PY
  done
}

verify_off() {
  ctl_py <<'PY'
import json, sqlite3
with sqlite3.connect('file:/data/gene.sqlite3?mode=ro', uri=True) as db:
    row = db.execute("SELECT json_extract(value,'$.reply_check_enabled') FROM agent_state WHERE key='config'").fetchone()
    runs = db.execute('SELECT count(*) FROM fri_reply_check_runs').fetchone()[0]
print(json.dumps({'reply_check_enabled': row[0] if row else None, 'reply_check_runs': runs}))
if (row and row[0]) or runs:
    raise SystemExit('reply checks are not off')
PY
}

consistent() {  # gene_sha fri_sha: running, pinned and recorded state agree
  python3 - "$(state)" "$1" "$2" <<'PY'
import json, sys
s, gene, fri = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
checks = {'active_sha': s['settings'].get('active_sha') == gene, 'release_env': s['release_env_sha'] == gene,
          'gene_health': s['gene']['sha'] == gene and s['gene']['age'] < 25,
          'fri_health': s['fri']['sha'] == fri and s['fri']['age'] < 30 and s['fri']['running'] and not s['fri']['failed'],
          'fri_record': s['fri_record_sha'] == fri, 'fri_active_sha': s['settings'].get('fri_active_sha') in (None, fri),
          'idle': s['busy_jobs'] == 0}
bad = [k for k, v in checks.items() if not v]
if bad:
    sys.exit('state is not consistent: '+', '.join(bad))
print('consistent')
PY
}

resume_services() {  # gene_sha
  systemctl start gene-worker gene-deployer
  systemctl is-active -q gene-worker && systemctl is-active -q gene-deployer || die 'units did not start'
  if has paused_by_release; then set_paused false; fi
  audit maintainer_services_resumed "$1"
}

resume() {
  need gene_recorded fri_switched
  has resumed && { log 'already resumed'; return; }
  consistent "$G" "$F"
  resume_services "$G"
  python3 - "$ROOT/maintainer-$G-result.json" "$G" "$OLD_G" "$F" "$OLD_F" "$(backup_dir)" <<'PY'
import json, os, sys, time
path, sha, previous, fri, fri_previous, backup = sys.argv[1:7]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump({'sha': sha, 'previous': previous, 'success': True, 'recovered': False, 'backup': backup,
               'at': time.time(), 'fri_sha': fri, 'fri_previous': fri_previous}, out)
PY
  mark resumed
  state
}

resume_old() {
  need gene_rolled_back
  consistent "$OLD_G" "$OLD_F"
  resume_services "$OLD_G"
  mark resumed_old
}

cleanup() {
  need resumed
  rm -rf "$STAGE/fri-src" "$STAGE/dbtest" "$STAGE"/*.tar "$STAGE"/*.tar.sha256
  log "kept: journal, baseline, evals in $STAGE; backup $(backup_dir); all images"
}

case $PHASE in
  baseline) baseline ;;
  stage) stage ;;
  rehearse) rehearse ;;
  drain) drain ;;
  gene) gene ;;
  fri) fri ;;
  evals) evals ;;
  verify-off) verify_off ;;
  resume) resume ;;
  cleanup) cleanup ;;
  status) state; [[ -f $JOURNAL ]] && cat "$JOURNAL" ;;
  rollback-gene) need gene_attempted; rollback_gene ;;
  resume-old) resume_old ;;
  *) die "unknown phase $PHASE" ;;
esac
