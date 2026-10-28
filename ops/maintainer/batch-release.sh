#!/usr/bin/env bash
# Maintainer release: Gene control plane + cogs, then Fri. Run as root on the host:
#   batch-release.sh <phase> <full gene sha> <full fri sha>
# Phases in order: baseline stage rehearse drain gene fri evals [accept-evals] verify-off resume cleanup.
# It upgrades from the live release, which baseline records. When G or F is already live, that half
# is left alone. Recovery: rollback-gene (reconciles from actual state, safe to repeat), then
# resume-old. A release that leaves Gene alone rolls back with rollback-fri instead.
# Prints no secrets: env files are passed to Docker, copied or compared, never read into output.
# Every controller DB access runs as the DB owner in a throwaway container of the old runtime
# image, so no root SQLite client leaves journal files the controller cannot open, and none
# of it needs a working controller. The one exception is the daily cap's read-only check
# (drained_today), which runs before anything else: a read-only connection creates no journal,
# and SQLite gives a shared-memory file it creates as root to the DB owner.
set -Eeuo pipefail
umask 077

PHASE=${1:?phase}; G=${2:?gene sha}; F=${3:?fri sha}
[[ $G =~ ^[0-9a-f]{40}$ && $F =~ ^[0-9a-f]{40}$ ]] || { echo 'Full 40-character SHAs required' >&2; exit 2; }
[[ ${BATCH_ATTEMPT:-} =~ ^[0-9]*$ ]] || { echo 'BATCH_ATTEMPT is a number' >&2; exit 2; }
ROOT=${BATCH_ROOT:-/srv/gene}
FRI_ROOT=${BATCH_FRI_ROOT:-/srv/fri}
DB=$ROOT/data/controller/gene.sqlite3
GENE_HEALTH=$ROOT/data/red/gene-health.json
FRI_HEALTH=$ROOT/data/fri/fri-health.json
STAGE=$ROOT/staging/batch-$G-$F${BATCH_ATTEMPT:+-$BATCH_ATTEMPT}  # A retry of a rolled-back pair sets BATCH_ATTEMPT
JOURNAL=$STAGE/journal
MANIFEST=$STAGE/manifest.json
BASELINE=$STAGE/baseline.json
RESULT=$ROOT/maintainer-$G-$F${BATCH_ATTEMPT:+-$BATCH_ATTEMPT}-result.json  # One per attempt, like STAGE
KEEP_RELEASES=${KEEP_RELEASES:-3}  # cleanup keeps the old and new images of this many recent releases
CONTROLLER=gene-controller-1
RED=gene-red-1
FRI=fri-runtime-1
AUDIO=gene-audio-node-1
PROBE_WINDOW=${PROBE_WINDOW:-180}  # seconds; shorter only in offline tests of this script
WAIT_WINDOW=${WAIT_WINDOW:-1800}
# Controller data a Gene feature adds that a release without the feature can't handle, in the order a
# rollback removes it (downgrade_step has each command). Only a feature the old release lacks is removed:
# the old release reads its own features' data correctly, and removing that would lose it.
DOWNGRADE_MODULES=(gene.fri_reply_checks gene.profile_downgrade gene.fri_member_summary)
SWITCHING=0

log() { echo "== $*"; }
epoch() {  # Now, for health freshness. Recovery must not stop on a clock read: Python's clock, then 0 (no baseline).
  date +%s 2>/dev/null || python3 -c 'import time; print(int(time.time()))' 2>/dev/null || echo 0
}
die() {
  echo "FAIL: $*" >&2
  # exit skips the ERR trap, so a refusal mid-switch must start the rollback itself.
  if [[ $SWITCHING == 1 ]]; then on_gene_error; fi
  exit 1
}
mark() { echo "$1" >> "$JOURNAL"; sync "$JOURNAL"; }
has() { [[ -f $JOURNAL ]] && grep -qx -- "$1" "$JOURNAL"; }
latest() {  # step: journaled since the last evals run started, so a later run's result replaces an earlier one's
  [[ -f $JOURNAL ]] && awk -v step="$1" '$0 == "evals_started" {found = 0} $0 == step {found = 1} END {exit !found}' "$JOURNAL"
}
need() { local step; for step in "$@"; do has "$step" || die "phase needs '$step' first"; done; }
has_module() {  # module docker-run-words...: true when the image has the module; a probe that fails to run stops the phase
  local module=$1 found; shift
  found=$("$@" python -c 'import importlib.util, sys; print("yes" if importlib.util.find_spec(sys.argv[1]) else "no")' "$module") ||
    die "could not check the image for $module"
  case $found in yes) return 0 ;; no) return 1 ;; *) die "could not check the image for $module" ;; esac
}
gene_changes() { [[ $G != "$OLD_G" ]]; }
fri_changes() { [[ $F != "$OLD_F" ]]; }
changes() { if [[ $1 == fri ]]; then fri_changes; else gene_changes; fi; }  # runtime|worker|fri: a new image
downgrade_step() {  # module db docker-run-words...: remove the module's data for a release without its feature
  local module=$1 db=$2; shift 2
  case $module in
    gene.fri_reply_checks) "$@" python -m gene.fri_reply_checks purge "$db" ;;
    gene.profile_downgrade) "$@" python -m gene.profile_downgrade "$db" ;;
    gene.fri_member_summary) "$@" python -m gene.fri_member_summary purge "$db" ;;
    *) die "no downgrade step for $module" ;;
  esac
}
pause_mark() {  # The journal's latest pause line, empty when this release never paused. skipped:pause: its
  # fence found development already paused, by someone else, so that pause was never its own.
  [[ -f $JOURNAL ]] || return 0
  { grep -x -e intent:pause -e done:unpause -e skipped:pause "$JOURNAL" || true; } | tail -n 1
}
owns_pause() { [[ $(pause_mark) == intent:pause ]]; }  # This release paused development and has not unpaused it since.
pause_id() {  # The audit id of this release's latest pause; empty when it was killed before journaling it
  [[ -f $JOURNAL ]] || return 0
  awk '$0 == "intent:pause" {id = ""} /^paused:[0-9]+$/ {id = substr($0, 8)} END {print id}' "$JOURNAL"
}
jget() {  # file dotted.key
  python3 -c 'import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    value = value[key]
print(value)' "$1" "$2"
}
mset() {  # dotted.key value: set a string in the manifest
  python3 - "$MANIFEST" "$1" "$2" <<'PY'
import json, os, sys
path, dotted, value = sys.argv[1:4]
data = json.load(open(path))
node, keys = data, dotted.split('.')
for key in keys[:-1]:
    node = node.setdefault(key, {})
node[keys[-1]] = value
with open(path+'.tmp', 'w') as out:
    json.dump(data, out, indent=1)
os.replace(path+'.tmp', path)
PY
}
image_id() { docker image inspect -f '{{.Id}}' "$1"; }
container_image() { docker inspect -f '{{.Image}}' "$1" 2>/dev/null || echo none; }
container_running() { [[ $(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false) == true ]]; }
backup_dir() { jget "$MANIFEST" backup; }

compose_gene() {
  # Compose files are identical between the releases (stage refuses an ops diff), so use
  # whichever source tree is in place.
  local file=$ROOT/source/ops/compose.yml
  [[ -f $file ]] || file=$(backup_dir)/installed-source/ops/compose.yml
  docker compose --project-name gene --env-file "$ROOT/release.env" -f "$file" "$@"
}

fri_compose() {  # Fri's own project, exactly as gene.fri_release runs it
  local file=$ROOT/source/ops/fri-compose.yml
  [[ -f $file ]] || file=$(backup_dir)/installed-source/ops/fri-compose.yml
  docker compose --project-name fri --env-file "$FRI_ROOT/release.env" -f "$file" "$@"
}

db_owner() {  # uid:gid of the controller DB; its directory must agree, since SQLite creates files there
  local owner
  owner=$(stat -c %u:%g "$DB")
  [[ $(stat -c %u:%g "$(dirname "$DB")") == "$owner" ]] || die 'the controller data dir and database have different owners'
  echo "$owner"
}

sidecar_owner() {  # file: its uid:gid, or nothing when it is gone; fails when it can't be read but is there
  # A running controller's SQLite creates and removes the DB's journal files at any moment, so each is
  # read once, and one that vanishes before or during the read has nothing to check. Any other failure
  # (an I/O error, say) is the caller's to stop on: it returns non-zero rather than dying in a subshell.
  local owner
  if owner=$(stat -c %u:%g "$1" 2>/dev/null); then echo "$owner"; return 0; fi
  [[ ! -e $1 && ! -L $1 ]]
}

repair_sidecars() {
  # gene.fri_release opens the DB as root. SQLite normally hands new journal files to the DB owner,
  # but a build without that would leave root-owned ones the owner-run helpers can't open, so every
  # DB access first gives them back. The read-only phases leave them alone and fail visibly instead.
  [[ $PHASE == baseline || $PHASE == status ]] && return 0
  local owner file current
  owner=$(db_owner)
  for file in "$DB-wal" "$DB-shm" "$DB-journal"; do
    current=$(sidecar_owner "$file") || die "could not read the owner of ${file##*/}"
    [[ -n $current && $current != "$owner" ]] || continue
    log "giving ${file##*/} back to the database owner" >&2
    if ! chown "$owner" "$file"; then
      # Removed meanwhile, or already replaced by the owner's own: nothing is left to repair.
      current=$(sidecar_owner "$file") || die "could not read the owner of ${file##*/}"
      [[ -z $current || $current == "$owner" ]] || die "could not give ${file##*/} back to the database owner"
      log "${file##*/} was removed or replaced meanwhile; nothing to repair" >&2
    fi
  done
}

db_image() {  # The old runtime image exists before, during and after the switch.
  if [[ -f $MANIFEST ]]; then jget "$MANIFEST" old.runtime.id; else sed -n 's/^GENE_RUNTIME_IMAGE=//p' "$ROOT/release.env"; fi
}

db_py() {  # args: Python on stdin, as the DB owner, with only the controller data dir mounted at /data
  local owner image
  repair_sidecars
  owner=$(db_owner); image=$(db_image)
  docker run -i --rm --network none --user "$owner" --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges:true -v "$ROOT/data/controller:/data" "$image" python - "$@"
}

db_copy() {  # dir name: an online copy of the live DB at dir/name, made as the DB owner into a dir they own
  local owner image
  repair_sidecars
  owner=$(db_owner); image=$(db_image)
  [[ $(stat -c %u:%g "$1") == "$owner" ]] || die "$1 is not owned by the database owner"
  docker run -i --rm --network none --user "$owner" --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges:true -v "$ROOT/data/controller:/data" -v "$1:/copy" "$image" python - "/copy/$2" <<'PY'
import sqlite3, sys
src = sqlite3.connect('file:/data/gene.sqlite3?mode=ro', uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'the copy failed its quick_check'
dst.close()
src.close()
print('database copied')
PY
}

settings_transition() {  # from_active from_previous to_active to_previous action; a no-op if already there
  db_py "$@" <<'PY'
import sqlite3, sys, time
from_a, from_p, to_a, to_p, action = sys.argv[1:6]
db = sqlite3.connect('/data/gene.sqlite3', timeout=30, isolation_level=None)
keys = "SELECT key,value FROM settings WHERE key IN ('active_sha','previous_sha')"
db.execute('BEGIN IMMEDIATE')
try:
    current = dict(db.execute(keys))
    if current == {'active_sha': to_a, 'previous_sha': to_p}:
        db.execute('ROLLBACK')
        print('release settings already', current)
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
print('release settings recorded', dict(db.execute(keys)))
PY
}

pause_development() {  # The writes Store.set_paused makes, without needing the controller, only if development
  # is still unpaused: checked and changed in one transaction, so an owner's /pause since the fence read the
  # setting stays theirs. The audit row names this batch, and its id goes in the journal: unpause_if_ours
  # lifts only that pause.
  local id rc=0
  id=$(db_py "${STAGE##*/}" <<'PY'
import sqlite3, sys, time
db = sqlite3.connect('/data/gene.sqlite3', timeout=30, isolation_level=None)
db.execute('BEGIN IMMEDIATE')
try:
    row = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
    if row is None or row[0] not in ('true', 'false'):
        raise SystemExit('unexpected paused setting: '+repr(row))
    if row[0] == 'true':  # Paused since the fence looked: that pause is not this batch's.
        db.execute('ROLLBACK')
        sys.exit(3)
    db.execute("UPDATE settings SET value='true' WHERE key='paused'")
    audit = db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
                       (time.time(), 'maintainer:claude', 'pause', sys.argv[1], '')).lastrowid
    db.execute('COMMIT')
except BaseException:
    if db.in_transaction:
        db.execute('ROLLBACK')
    raise
print(audit)
PY
  ) || rc=$?
  if (( rc == 3 )); then
    mark skipped:pause
    log 'development was paused meanwhile by someone else; that pause stays theirs'
    return 0
  fi
  if (( rc != 0 )) || [[ ! $id =~ ^[0-9]+$ ]]; then die "could not pause development: ${id:-no answer from the controller DB}"; fi
  mark "paused:$id"
  log "development paused (audit $id)"
}

unpause_if_ours() {  # Unpause only while this batch's pause is still the latest pause or resume, checked and
  # changed in one transaction, so a pause the owner made since (/pause) stays. Exit 3: left as it is, and why.
  # The baseline's time bounds a pause from the script before audit ids (8deef14), whose row names no batch.
  db_py "$(pause_id)" "${STAGE##*/}" "$(stat -c %Y "$BASELINE" 2>/dev/null || true)" <<'PY'
import sqlite3, sys, time
mine, stage, baseline = sys.argv[1:4]
db = sqlite3.connect('/data/gene.sqlite3', timeout=30, isolation_level=None)
db.execute('BEGIN IMMEDIATE')
try:
    latest = db.execute("SELECT id,action,actor,target,at FROM audit WHERE action IN ('pause','resume') "
                        'ORDER BY id DESC LIMIT 1').fetchone()
    # A repeat of a resume killed before its journal line: the latest resume is this batch's own.
    if latest is not None and latest[1:4] == ('resume', 'maintainer:claude', stage):
        db.execute('ROLLBACK')
        print(f'development already resumed by this batch (audit {latest[0]})')
        sys.exit(0)
    if latest is None or latest[1] != 'pause':
        ours = False
    elif mine:
        ours = latest[0] == int(mine)
    else:  # No id journaled: a pause killed before its id reached the journal is known by the batch its row
        # names. One from the script before audit ids names none: it counts if made since this batch's baseline.
        ours = latest[2] == 'maintainer:claude' and (latest[3] == stage or (
            latest[3] == '' and baseline.isdigit() and latest[4] >= int(baseline)))
    if not ours:
        db.execute('ROLLBACK')
        if latest is None:
            print('no pause or resume is recorded, so development is left as it is')
        elif latest[1] == 'pause':
            print(f"development stays paused: the latest pause (audit {latest[0]}) is not this batch's")
        else:
            print(f'development was resumed (audit {latest[0]}) after this batch paused it, and is left as it is')
        sys.exit(3)
    if db.execute("UPDATE settings SET value='false' WHERE key='paused'").rowcount != 1:
        raise SystemExit('the controller has no paused setting')
    db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
               (time.time(), 'maintainer:claude', 'resume', stage, f'lifts the pause in audit {latest[0]}'))
    db.execute('COMMIT')
except BaseException:
    if db.in_transaction:
        db.execute('ROLLBACK')
    raise
print(f"development resumed: this batch's pause (audit {latest[0]}) was still the latest")
PY
}

pause_kept() {  # Why development is still paused when this batch doesn't own the pause; nothing when it isn't
  # paused. Reporting only: the pause stays, whoever made it.
  db_py "$(pause_mark)" <<'PY'
import sqlite3, sys
from contextlib import closing
with closing(sqlite3.connect('file:/data/gene.sqlite3?mode=ro', uri=True, timeout=30)) as db:
    paused = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
    latest = db.execute("SELECT id FROM audit WHERE action='pause' ORDER BY id DESC LIMIT 1").fetchone()
if paused is None or paused[0] != 'true':
    sys.exit(0)
if latest is None:
    print('development stays paused: no pause is in the audit, and this batch never paused it')
elif sys.argv[1] == 'skipped:pause':
    print(f'development stays paused: someone else paused it first (audit {latest[0]}), so this batch never took a pause of its own')
else:
    print(f"development stays paused: the latest pause (audit {latest[0]}) is not this batch's")
PY
}

set_fri_active() {  # sha: the controller's record of the active Fri, as gene.fri_release keeps it
  db_py "$1" <<'PY'
import sqlite3, sys, time
sha = sys.argv[1]
db = sqlite3.connect('/data/gene.sqlite3', timeout=30, isolation_level=None)
db.execute('BEGIN IMMEDIATE')
row = db.execute("SELECT value FROM settings WHERE key='fri_active_sha'").fetchone()
if row and row[0] == sha:
    db.execute('ROLLBACK')
    print('fri_active_sha already', sha[:7])
    sys.exit(0)
db.execute("INSERT INTO settings VALUES ('fri_active_sha', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sha,))
db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
           (time.time(), 'maintainer:claude', 'fri_release', sha, 'restored by the batched maintainer rollback'))
db.execute('COMMIT')
print('fri_active_sha =', sha[:7])
PY
}

audit() {  # action target
  db_py "$@" <<'PY'
import sqlite3, sys, time
db = sqlite3.connect('/data/gene.sqlite3', timeout=30)
with db:
    db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
               (time.time(), 'maintainer:claude', sys.argv[1], sys.argv[2], 'Chat-authorized batched maintainer release'))
db.close()
PY
}

source_state() {  # The installed tree's release marker, which this script writes, else unmarked or missing.
  if [[ -f $ROOT/source/.release-sha ]]; then cat "$ROOT/source/.release-sha"
  elif [[ -d $ROOT/source ]]; then echo unmarked
  else echo missing; fi
}

state() {  # Read-only snapshot of running, pinned and recorded state; no row contents or secrets.
  local worker_pin=unknown sha health http=0 src_state db
  for sha in "$G" "$OLD_G"; do
    if [[ $(grep -Fxc -- "GENE_WORKER_IMAGE=gene-worker:$sha" "$ROOT/secrets/worker.env" || true) == 1 ]]; then
      worker_pin=gene-worker:$sha
    fi
  done
  health=$(docker inspect -f '{{.State.Health.Status}}' "$CONTROLLER" 2>/dev/null || echo none)
  http=$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8787/health || true)
  src_state=$(source_state)
  db=$(db_py <<'PY'
import json, sqlite3
with sqlite3.connect('file:/data/gene.sqlite3?mode=ro', uri=True, timeout=30) as conn:
    settings = dict(conn.execute("SELECT key,value FROM settings WHERE key IN "
                                 "('active_sha','previous_sha','paused','fri_active_sha')"))
    busy = conn.execute("SELECT count(*) FROM jobs WHERE state IN ('building','deploying')").fetchone()[0]
print(json.dumps({'settings': settings, 'busy_jobs': busy}))
PY
  )
  python3 - "$db" "$GENE_HEALTH" "$FRI_HEALTH" "$ROOT/release.env" "$FRI_ROOT/release.env" "$FRI_ROOT/release.json" \
    "$worker_pin" "$health" "$http" "$(container_image "$CONTROLLER")" "$(container_image "$RED")" "$(container_image "$FRI")" \
    "$(container_running "$RED" && echo true || echo false)" "$(container_running "$FRI" && echo true || echo false)" \
    "$src_state" <<'PY'
import json, sys, time
(db, gene_health, fri_health, release_env, fri_env, fri_record, worker_pin, health, http,
 controller_image, red_image, fri_image, red_running, fri_running, source) = sys.argv[1:16]
def load(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}
def env(path):
    try:
        return dict(line.split('=', 1) for line in open(path).read().splitlines() if '=' in line)
    except OSError:
        return {}
gene, fri, genv, fenv = load(gene_health), load(fri_health), env(release_env), env(fri_env)
beat = fri.get('agent_heartbeat') or {}
now = time.time()
print(json.dumps(json.loads(db) | {
    'gene_env': {k: genv.get(k) for k in ('GENE_RELEASE_SHA', 'GENE_RUNTIME_IMAGE')}, 'worker_pin': worker_pin,
    'fri_env': {k: fenv.get(k) for k in ('FRI_RELEASE_SHA', 'FRI_RUNTIME_IMAGE')},
    'fri_record_sha': load(fri_record).get('sha'), 'source': source,
    'controller': {'health': health, 'http': http, 'image': controller_image},
    'red': {'image': red_image, 'running': red_running == 'true'},
    'fri_container': {'image': fri_image, 'running': fri_running == 'true'},
    'gene': {k: gene.get(k) for k in ('sha', 'guild_connected', 'controls_running', 'presence')}
            | {'guild_id': str(gene.get('guild_id')), 'age': round(now-gene.get('at', 0), 1)},
    'fri': {k: fri.get(k) for k in ('sha', 'guild_connected', 'presence')}
           | {'guild_id': str(fri.get('guild_id')), 'running': beat.get('running'), 'failed': beat.get('failed'),
              'age': round(now-fri.get('at', 0), 1)}}))
PY
}

verify() {  # old|new [any-work]: everything running, pinned and recorded agrees with one release
  python3 - "$(state)" "$1" "${2:-}" "$MANIFEST" "$BASELINE" "$G" "$F" "$OLD_G" "$OLD_F" <<'PY'
import json, sys
s, which, mode, manifest, baseline = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], json.load(open(sys.argv[4])), json.load(open(sys.argv[5]))
G, F, OLD_G, OLD_F = sys.argv[6:10]
gene_sha, fri_sha = (G, F) if which == 'new' else (OLD_G, OLD_F)
images = manifest['new' if which == 'new' else 'old']
gene_moves = which == 'new' and G != OLD_G  # A release that leaves Gene alone keeps its settings and source.
expect_settings = ({'active_sha': G, 'previous_sha': OLD_G} if gene_moves
                   else {'active_sha': OLD_G, 'previous_sha': baseline['settings']['previous_sha']})
checks = {
    'controller_healthy': s['controller']['health'] == 'healthy' and s['controller']['http'] == '200',
    'controller_image': s['controller']['image'] == images['runtime']['id'],
    'red_image': s['red']['running'] and s['red']['image'] == images['runtime']['id'],
    'fri_image': s['fri_container']['running'] and s['fri_container']['image'] == images['fri']['id'],
    'gene_env': s['gene_env'] == {'GENE_RELEASE_SHA': gene_sha, 'GENE_RUNTIME_IMAGE': images['runtime']['tag']},
    'worker_pin': s['worker_pin'] == images['worker']['tag'],
    'source': s['source'] == (G if gene_moves else baseline['source']),
    'fri_env': s['fri_env'] == {'FRI_RELEASE_SHA': fri_sha, 'FRI_RUNTIME_IMAGE': images['fri']['tag']},
    'fri_record': s['fri_record_sha'] == fri_sha,
    'release_settings': {k: s['settings'].get(k) for k in expect_settings} == expect_settings,
    'fri_active_sha': s['settings'].get('fri_active_sha') == fri_sha
                      or (which == 'old' and s['settings'].get('fri_active_sha') == baseline['settings'].get('fri_active_sha')),
    'gene_health': (s['gene']['sha'] == gene_sha and s['gene']['age'] < 25 and s['gene']['guild_connected'] is True
                    and s['gene']['controls_running'] is True and s['gene']['guild_id'] == baseline['gene']['guild_id']
                    and s['gene']['presence'] == baseline['gene']['presence']),
    'fri_health': (s['fri']['sha'] == fri_sha and s['fri']['age'] < 30 and s['fri']['guild_connected'] is True
                   and s['fri']['running'] is True and not s['fri']['failed']
                   and s['fri']['guild_id'] == baseline['fri']['guild_id'] and s['fri']['presence'] == baseline['fri']['presence'])}
if mode != 'any-work':
    checks['idle'] = s['busy_jobs'] == 0
bad = [k for k, v in checks.items() if not v]
if bad:
    print(json.dumps(s))
    sys.exit(which+' release is not consistent: '+', '.join(bad))
print(which, 'release consistent')
PY
}

pair_check() {  # Rollback only reconciles live states built from the recorded old and new releases.
  python3 - "$(state)" "$MANIFEST" "$BASELINE" "$(backup_dir)" "$ROOT" "$FRI_ROOT" "$G" "$F" "$OLD_G" "$OLD_F" <<'PY'
import json, os, re, sys
s, manifest, baseline = json.loads(sys.argv[1]), json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))
B, root, fri_root, G, F, OLD_G, OLD_F = sys.argv[4:11]
def read(path):  # compared only, never printed: some of these hold secrets
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except FileNotFoundError:
        return None
saved = {name: read(B+'/'+name) for name in ('release.env', 'worker.env', 'fri-release.env', 'fri-release.json')}
missing = [name for name, value in saved.items() if value is None]
if missing or not os.path.isfile(B+'/db/controller.sqlite3'):
    sys.exit('the switch backup in '+B+' is incomplete: '+', '.join(missing or ['db/controller.sqlite3']))
new_release = f'GENE_RELEASE_SHA={G}\nGENE_ROOT={root}\nGENE_RUNTIME_IMAGE=gene-runtime:{G}\n'.encode()
new_worker = re.sub(rb'(?m)^GENE_WORKER_IMAGE=.*$', b'GENE_WORKER_IMAGE=gene-worker:'+G.encode(), saved['worker.env'])
new_fri_env = saved['fri-release.env'].replace(OLD_F.encode(), F.encode())
try:
    record = json.loads(read(fri_root+'/release.json') or b'null')
except ValueError:
    record = None
old, new = manifest['old'], manifest['new']
gene_images = (old['runtime']['id'], new['runtime']['id'], 'none')
active = (s['settings'].get('active_sha'), s['settings'].get('previous_sha'))
checks = {
    'release.env': read(root+'/release.env') in (saved['release.env'], new_release),
    'worker.env': read(root+'/secrets/worker.env') in (saved['worker.env'], new_worker),
    'fri release.env': read(fri_root+'/release.env') in (saved['fri-release.env'], new_fri_env),
    'fri release.json': (read(fri_root+'/release.json') == saved['fri-release.json']
                         or (isinstance(record, dict) and record.get('sha') == F and record.get('previous') == OLD_F)),
    'release settings': active in ((OLD_G, baseline['settings']['previous_sha']), (G, OLD_G)),
    'fri_active_sha': s['settings'].get('fri_active_sha') in (None, OLD_F, F),
    'controller image': s['controller']['image'] in gene_images,
    'red image': s['red']['image'] in gene_images,
    'fri image': s['fri_container']['image'] in (old['fri']['id'], new['fri']['id'], 'none'),
    'source': (s['source'], os.path.isdir(B+'/installed-source')) in ((baseline['source'], False), (G, True), ('missing', True)),
}
bad = [k for k, v in checks.items() if not v]
if bad:
    sys.exit('live state is outside the recorded old/new pair ('+', '.join(bad)+'); reconcile by hand, nothing was changed')
print('live state is built from the recorded old and new releases')
PY
}

probe_gene() {  # sha started_epoch image_id: 6 consecutive good 5 s samples within 180 s
  local deadline=$(( SECONDS + PROBE_WINDOW )) good=0 snapshot reason=''  # SECONDS: no clock read can fail
  while (( SECONDS < deadline )); do
    snapshot=$(state)
    if reason=$(python3 - "$snapshot" "$1" "$2" "$3" "$BASELINE" <<'PY'
import json, sys, time
s, sha, after, image, base = json.loads(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4], json.load(open(sys.argv[5]))
checks = {'controller': s['controller']['health'] == 'healthy' and s['controller']['http'] == '200'
                        and s['controller']['image'] == image,
          'red_image': s['red']['running'] and s['red']['image'] == image,
          'sha': s['gene']['sha'] == sha, 'connected': s['gene']['guild_connected'] is True,
          'guild': s['gene']['guild_id'] == base['gene']['guild_id'],
          'presence': s['gene']['presence'] == base['gene']['presence'],
          'controls': s['gene']['controls_running'] is True,
          'fresh': s['gene']['age'] < 25 and time.time()-s['gene']['age'] > after,
          'fri_alive': s['fri']['running'] is True and not s['fri']['failed'] and s['fri']['age'] < 30}
bad = ','.join(k for k, v in checks.items() if not v)
print(bad)
sys.exit(1 if bad else 0)
PY
    ); then
      good=$(( good + 1 ))
      if (( good >= 6 )); then log "Gene healthy at $1"; return 0; fi
    else
      good=0
    fi
    sleep 5
  done
  echo "FAIL: Gene unhealthy at $1: $reason" >&2
  return 1
}

probe_fri() {  # sha image_id: 4 consecutive good samples within 180 s
  local deadline=$(( SECONDS + PROBE_WINDOW )) good=0 reason=''
  while (( SECONDS < deadline )); do
    if reason=$(python3 - "$(state)" "$1" "$2" "$BASELINE" "$FRI_HEALTH" <<'PY'
import json, sys
s, sha, image, base = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], json.load(open(sys.argv[4]))
checks = {'image': s['fri_container']['running'] and s['fri_container']['image'] == image,
          'sha': s['fri']['sha'] == sha, 'connected': s['fri']['guild_connected'] is True,
          'guild': s['fri']['guild_id'] == base['fri']['guild_id'], 'presence': s['fri']['presence'] == base['fri']['presence'],
          'running': s['fri']['running'] is True, 'not_failed': not s['fri']['failed'], 'fresh': s['fri']['age'] < 30}
bad = ','.join(k for k, v in checks.items() if not v)
print(bad)
sys.exit(1 if bad else 0)
PY
    ); then
      good=$(( good + 1 ))
      if (( good >= 4 )); then
        log "Fri healthy at $1"
        python3 -c 'import json, sys; h = json.load(open(sys.argv[1]))
print("  fri health keys:", sorted(h), "heartbeat keys:", sorted(h.get("agent_heartbeat") or {}))' "$FRI_HEALTH"
        return 0
      fi
    else
      good=0
    fi
    sleep 5
  done
  echo "FAIL: Fri unhealthy at $1: $reason" >&2
  return 1
}

player_snapshot() {
  # The audio node's own counts from Lavalink's stats op. Lavalink 3.7 StatsCollector sums players
  # and playingPlayers over every connected client, so this probe session sees Red's players too.
  local config=$ROOT/secrets/audio.yml
  docker run -i --rm --network "container:$AUDIO" --user "$(stat -c %u:%g "$config")" --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges:true -v "$config:/audio.yml:ro" "$(jget "$MANIFEST" old.runtime.id)" python - <<'PY'
import asyncio, json, aiohttp, yaml
config = yaml.safe_load(open('/audio.yml'))
password = config['lavalink']['server']['password']
port = (config.get('server') or {}).get('port', 2333)
async def main():
    headers = {'Authorization': str(password), 'User-Id': '1', 'Client-Name': 'gene-maintainer-probe', 'Num-Shards': '1'}
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f'ws://127.0.0.1:{port}', headers=headers) as ws:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    if data.get('op') == 'stats':
                        return {'players': data['players'], 'playing': data['playingPlayers']}
            raise SystemExit('the audio node closed before sending stats')
print(json.dumps(asyncio.run(asyncio.wait_for(main(), 90))))
PY
}

quiet() {  # wait_seconds: Red restarts only once the audio node reports no players at all
  local deadline=$(( SECONDS + $1 )) snapshot players playing
  while true; do
    snapshot=$(player_snapshot) || die 'could not read the audio node player state; not restarting Red'
    players=$(jget <(echo "$snapshot") players); playing=$(jget <(echo "$snapshot") playing)
    log "audio node: $players players, $playing playing"
    (( players == 0 )) && return 0
    (( SECONDS < deadline )) || die "the audio node still has $players players ($playing playing); not restarting Red. If Gene is sitting idle in voice, ask the owner before disconnecting it"
    sleep 60
  done
}

tree_hash() {  # dir: content, modes and owners of every entry
  python3 - "$1" <<'PY'
import hashlib, os, sys
root, digest = sys.argv[1], hashlib.sha256()
for base, dirs, files in sorted(os.walk(root)):
    dirs.sort()
    for name in [''] + sorted(files):
        path = os.path.join(base, name) if name else base
        st = os.lstat(path)
        digest.update(f'{os.path.relpath(path, root)}|{st.st_mode:o}|{st.st_uid}|{st.st_gid}|'.encode())
        if name:
            digest.update(hashlib.sha256(open(path, 'rb').read()).digest())
print(digest.hexdigest())
PY
}

disk_admission() {  # label=path:bytes ...; sums needs per filesystem and keeps 2 GiB spare on each
  python3 - "$@" <<'PY'
import os, sys
need = {}
for item in sys.argv[1:]:
    label, rest = item.split('=', 1)
    path, size = rest.rsplit(':', 1)
    dev = os.stat(path).st_dev
    need.setdefault(dev, [path, 0, []])
    need[dev][1] += int(size)
    need[dev][2].append(label)
for dev, (path, size, labels) in need.items():
    st = os.statvfs(path)
    free = st.f_bavail*st.f_frsize
    print(f'disk {path} ({"+".join(labels)}): {free/2**30:.1f} GiB free, {size/2**30:.1f} GiB needed')
    if free < size + 2*2**30:
        sys.exit('not enough disk on '+path)
PY
}
du_bytes() {  # path: its size in bytes
  # A file removed while du walks the tree, such as a running controller's SQLite journal, makes du
  # fail after it prints the total; that file just isn't counted. Any other error (a permission or I/O
  # error, which would undercount) stops the script, and so does a "no such file" for a path still there.
  local size errors line gone re="^du: cannot access '(.*)': No such file or directory$"
  errors=$(mktemp)
  size=$(LC_ALL=C du -sb "$1" 2>"$errors" | cut -f1) || true
  while IFS= read -r line; do
    gone=''
    [[ $line =~ $re ]] && gone=${BASH_REMATCH[1]}
    if [[ -z $gone || -e $gone || -L $gone ]]; then rm -f "$errors"; die "could not measure $1: $line"; fi
  done < "$errors"
  rm -f "$errors"
  [[ $size =~ ^[0-9]+$ ]] || die "could not measure $1"
  echo "$size"
}

rename_tree() {  # src dst: a single rename(2) that fails rather than copying; dst must not exist
  python3 - "$1" "$2" <<'PY'
import os, sys
src, dst = sys.argv[1:3]
if os.path.lexists(dst):
    sys.exit(dst+' already exists')
os.rename(src, dst)
PY
}

rename_preflight() {  # backup_dir: walk a probe dir through every directory the switch moves trees between
  # Equal device numbers are not enough: a bind mount shares them and still refuses rename(2).
  python3 - "$ROOT" "$STAGE" "$ROOT/releases" "$ROOT" "$1" <<'PY'
import os, re, sys, uuid
root, stage, *hops = sys.argv[1:]
# A bind mount on the same filesystem looks like a plain directory to stat, so read the mount table.
trees = [os.path.realpath(p) for p in (root+'/source', stage+'/gene-src', stage+'/cogs-ready') if os.path.exists(p)]
with open('/proc/self/mountinfo') as info:
    for line in info:
        point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), line.split()[4])
        for tree in trees:
            if point == tree or point.startswith(tree+'/'):
                sys.exit(f'{point} is a mount point in {tree}, which the switch renames')
name = '.rename-probe-'+uuid.uuid4().hex
here = os.path.join(stage, name)
os.mkdir(here, 0o700)
try:
    for directory in hops + [stage]:
        target = os.path.join(directory, name)
        try:
            os.rename(here, target)
        except OSError as error:
            sys.exit(f'cannot rename {here} to {target} ({error.strerror}); the switch needs plain renames '
                     'between staging, releases/, the backups dir and the source tree')
        here = target
finally:
    os.rmdir(here)
print('switch moves are plain renames')
PY
}

check_manifest() {  # Every phase after baseline runs against the same G, F and images.
  [[ -f $MANIFEST ]] || die 'no manifest; run baseline'
  [[ $(jget "$MANIFEST" gene) == "$G" && $(jget "$MANIFEST" fri) == "$F" ]] || die 'manifest is for a different G/F'
  local kind name
  for kind in old new; do
    for name in runtime worker fri; do
      python3 -c 'import json, sys; m = json.load(open(sys.argv[1])); sys.exit(0 if sys.argv[3] in m.get(sys.argv[2], {}) else 1)' \
        "$MANIFEST" "$kind" "$name" || continue
      [[ $(image_id "$(jget "$MANIFEST" "$kind.$name.tag")") == "$(jget "$MANIFEST" "$kind.$name.id")" ]] \
        || die "image $(jget "$MANIFEST" "$kind.$name.tag") changed since it was recorded"
    done
  done
}

baseline() {
  local tool
  for tool in docker flock python3 sha256sum tar cmp; do command -v "$tool" > /dev/null || die "$tool is missing"; done
  [[ $OLD_G =~ ^[0-9a-f]{40}$ && $OLD_F =~ ^[0-9a-f]{40}$ ]] || die 'the release.env files do not name the live Gene and Fri'
  gene_changes || fri_changes || die "Gene ${G:0:7} and Fri ${F:0:7} are already live; nothing to release"
  local owner file current
  owner=$(db_owner)
  for file in "$DB-wal" "$DB-shm" "$DB-journal"; do
    current=$(sidecar_owner "$file") || die "could not read the owner of ${file##*/}"
    [[ -z $current || $current == "$owner" ]] || die "${file##*/} is not owned by the database owner"
  done
  mkdir -p "$STAGE"; chmod 700 "$ROOT/staging" "$STAGE"
  local snapshot runtime_tag fri_tag
  snapshot=$(state)
  runtime_tag=$(jget <(echo "$snapshot") gene_env.GENE_RUNTIME_IMAGE)
  fri_tag=$(jget <(echo "$snapshot") fri_env.FRI_RUNTIME_IMAGE)
  python3 - "$snapshot" "$OLD_G" "$OLD_F" "$(image_id "$runtime_tag")" "$(image_id "$fri_tag")" \
    "$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$runtime_tag")" \
    "$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$fri_tag")" <<'PY'
import json, sys
s, old_g, old_f, runtime_id, fri_id, runtime_label, fri_label = json.loads(sys.argv[1]), *sys.argv[2:8]
checks = {
    'active_sha': s['settings'].get('active_sha') == old_g, 'previous_sha': len(s['settings'].get('previous_sha') or '') == 40,
    'gene_env': s['gene_env']['GENE_RELEASE_SHA'] == old_g, 'runtime_label': runtime_label == old_g,
    'worker_pin': s['worker_pin'] == 'gene-worker:'+old_g, 'source': s['source'] in ('unmarked', old_g),
    'controller': s['controller']['health'] == 'healthy' and s['controller']['http'] == '200' and s['controller']['image'] == runtime_id,
    'red': s['red']['running'] and s['red']['image'] == runtime_id,
    'gene_health': s['gene']['sha'] == old_g and s['gene']['age'] < 25 and s['gene']['guild_connected'] is True
                   and s['gene']['controls_running'] is True and s['gene']['presence'] is not None,
    'fri_env': s['fri_env']['FRI_RELEASE_SHA'] == old_f and fri_label == old_f, 'fri_record': s['fri_record_sha'] == old_f,
    'fri_container': s['fri_container']['running'] and s['fri_container']['image'] == fri_id,
    'fri_active_sha': s['settings'].get('fri_active_sha') in (None, old_f),
    'fri_health': s['fri']['sha'] == old_f and s['fri']['age'] < 30 and s['fri']['guild_connected'] is True
                  and s['fri']['running'] is True and not s['fri']['failed'] and s['fri']['presence'] is not None}
bad = [k for k, v in checks.items() if not v]
if bad:
    print(json.dumps(s))
    sys.exit('live state is not the expected healthy release: '+', '.join(bad))
print('live state matches Gene', old_g[:7], 'and Fri', old_f[:7])
PY
  if [[ ! -f $BASELINE ]]; then
    echo "$snapshot" > "$BASELINE"
    python3 - "$MANIFEST" "$G" "$F" "$OLD_G" "$OLD_F" "$runtime_tag" "$(image_id "$runtime_tag")" "gene-worker:$OLD_G" \
      "$(image_id "gene-worker:$OLD_G")" "$fri_tag" "$(image_id "$fri_tag")" <<'PY'
import json, os, sys
path, g, f, old_g, old_f, rt, rid, wt, wid, ft, fid = sys.argv[1:12]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump({'gene': g, 'fri': f, 'old_gene': old_g, 'old_fri': old_f,
               'old': {'runtime': {'tag': rt, 'id': rid}, 'worker': {'tag': wt, 'id': wid}, 'fri': {'tag': ft, 'id': fid}}},
              out, indent=1)
PY
    mark baseline
  else
    check_manifest
    log 'baseline already recorded; live state still healthy at the old release'
  fi
  if ! gene_changes; then log "Gene ${G:0:7} is live: this release changes only Fri"
  elif ! fri_changes; then log "Fri ${F:0:7} is live: this release changes only Gene"; fi
  echo "$snapshot"
}

stage() {
  need baseline; check_manifest
  has staged && { log 'already staged'; return; }
  local names=() name file kind
  if gene_changes; then names+=(gene); fi
  if fri_changes; then names+=(fri); fi
  for name in "${names[@]}"; do
    [[ -f $STAGE/$name.tar && -f $STAGE/$name.tar.sha256 ]] || die "upload $STAGE/$name.tar and $name.tar.sha256 first"
    (cd "$STAGE" && sha256sum -c "$name.tar.sha256")
    mset "archives.$name" "$(cut -d' ' -f1 "$STAGE/$name.tar.sha256")"
  done
  if gene_changes; then [[ ! -e $ROOT/releases/$G ]] || die "$ROOT/releases/$G already exists; inspect it before staging"; fi
  local images=0
  for kind in runtime worker fri; do
    if changes "$kind"; then images=$(( images + $(docker image inspect -f '{{.Size}}' "$(jget "$MANIFEST" "old.$kind.tag")") )); fi
  done
  local db_size tars=0 gene_tar disk
  db_size=$(du_bytes "$ROOT/data/controller")
  for name in "${names[@]}"; do tars=$(( tars + $(stat -c %s "$STAGE/$name.tar") )); done
  disk=("docker=$(docker info -f '{{.DockerRootDir}}'):$images" "staging=$STAGE:$(( 2*tars + 2*db_size ))")
  mkdir -p "$ROOT/backups" "$FRI_ROOT/backups"
  if gene_changes; then
    mkdir -p "$ROOT/releases"
    rename_preflight "$ROOT/backups"
    gene_tar=$(stat -c %s "$STAGE/gene.tar")
    disk+=("gene_backup=$ROOT/backups:$(( db_size + $(du_bytes "$ROOT/data/red") + $(du_bytes "$ROOT/source") ))"
           "releases=$ROOT/releases:$gene_tar" "source=$ROOT:$gene_tar")
  fi
  if fri_changes; then disk+=("fri_backup=$FRI_ROOT/backups:$(( $(du_bytes "$ROOT/data/fri") + db_size ))"); fi
  disk_admission "${disk[@]}"
  for name in "${names[@]}"; do
    clear_interrupted "$STAGE/$name-src"
    mkdir -m 700 "$STAGE/$name-src"
    tar -x --no-same-owner -f "$STAGE/$name.tar" -C "$STAGE/$name-src"
    chmod -R a+rX,go-w "$STAGE/$name-src"
  done
  if gene_changes; then
    echo "$G" > "$STAGE/gene-src/.release-sha"
    for file in pyproject.toml uv.lock; do
      cmp -s "$ROOT/source/$file" "$STAGE/gene-src/$file" || die "$file changed: the host venv needs its own plan"
    done
    for file in ops/compose.yml ops/fri-compose.yml ops/gene-worker.service ops/gene-deployer.service; do
      if ! cmp -s "$ROOT/source/$file" "$STAGE/gene-src/$file"; then
        diff -u "$ROOT/source/$file" "$STAGE/gene-src/$file" || true
        [[ ${ALLOW_OPS_DIFF:-} == 1 ]] || die "$file changed; review the diff, then rerun with ALLOW_OPS_DIFF=1"
      fi
    done
    # The cog release, prepared now so downtime only renames it into place.
    clear_interrupted "$STAGE/cogs-ready"
    mkdir -m 755 "$STAGE/cogs-ready"
    cp -r "$STAGE/gene-src/cogs" "$STAGE/cogs-ready/cogs"
    chown -R 0:0 "$STAGE/cogs-ready"
    find "$STAGE/cogs-ready" -type d -exec chmod 755 {} +
    find "$STAGE/cogs-ready" -type f -exec chmod 444 {} +
    mset cogs "$(tree_hash "$STAGE/cogs-ready")"
    log 'building Gene images'
    open_for 'the Gene runtime build'
    docker build -q -f "$STAGE/gene-src/ops/Dockerfile" --target runtime \
      --label org.opencontainers.image.revision="$G" -t "gene-runtime:$G" "$STAGE/gene-src"
    open_for 'the Gene worker build'
    docker build -q -f "$STAGE/gene-src/ops/Dockerfile" --target worker \
      --label org.opencontainers.image.revision="$G" -t "gene-worker:$G" "$STAGE/gene-src"
  fi
  if fri_changes; then
    log 'building the Fri image'
    open_for 'the Fri build'
    docker build -q -f "$STAGE/fri-src/ops/Dockerfile" --build-arg FRI_RELEASE_SHA="$F" \
      -t "fri-runtime:$F" "$STAGE/fri-src"
  fi
  local image label tag
  for image in "runtime:gene-runtime:$G" "worker:gene-worker:$G" "fri:fri-runtime:$F"; do
    kind=${image%%:*}; tag=${image#*:}
    if changes "$kind"; then
      label=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$tag")
      [[ $label == "${tag#*:}" ]] || die "$tag label is $label"
      mset "new.$kind.tag" "$tag"; mset "new.$kind.id" "$(image_id "$tag")"
    else  # Unchanged: the new release keeps the live image.
      mset "new.$kind.tag" "$(jget "$MANIFEST" "old.$kind.tag")"; mset "new.$kind.id" "$(jget "$MANIFEST" "old.$kind.id")"
    fi
  done
  open_for 'the isolated import checks'
  log 'isolated import checks'
  local isolated=(forward_docker run --rm --network none --user 10001:10001 --read-only --tmpfs /tmp --cap-drop ALL
                  --security-opt no-new-privileges:true)
  if gene_changes; then
    "${isolated[@]}" "gene-runtime:$G" python -c 'import gene.api, gene.store, gene.runtime, gene.fri_release, gene.fri_reply_checks, gene.deploy, gene.provenance_downgrade; print("gene imports ok")'
    "${isolated[@]}" "gene-worker:$G" codex --version
  fi
  if fri_changes; then
    "${isolated[@]}" "fri-runtime:$F" python -c 'import fri.fri, fri.fri_eval, fri.fri_request_eval; print("fri imports ok")'
  fi
  open_for 'the controller contract check'
  log 'controller contract on synthetic state'
  local contract
  contract=$("${isolated[@]}" --tmpfs /data:uid=10001,gid=10001 -e GENE_DB=/data/synthetic.sqlite3 \
    -e GENE_RUNTIME_TOKEN=synthetic-runtime-token-0000000000000000 -e GENE_WORKER_TOKEN=synthetic-worker-token-00000000000000000 \
    -e GENE_DEPLOY_TOKEN=synthetic-deploy-token-00000000000000000 -e GENE_FRI_TOKEN=synthetic-fri-token-0000000000000000000000 \
    "$(jget "$MANIFEST" new.runtime.tag)" python -c '
import json
from fastapi.testclient import TestClient
from gene.api import create_app
client = TestClient(create_app())
assert client.get("/health").status_code == 200
response = client.get("/fri/contract", headers={"Authorization": "Bearer synthetic-fri-token-0000000000000000000000"})
assert response.status_code == 200, response.status_code
print(json.dumps(response.json()))')
  echo "$contract"
  "${isolated[@]}" "$(jget "$MANIFEST" new.fri.tag)" python -c 'import json, sys
from fri.fri_protocol import compatible
assert compatible(json.loads(sys.argv[1])), "Fri rejects the candidate controller contract"
print("fri accepts the contract")' "$contract"
  if gene_changes; then
    open_for 'the audio player check'
    log 'audio player snapshot works'
    player_snapshot
  fi
  mark staged
}

rehearse() {  # Migrate copies of the live DB with G, then start the exact rollback image on them.
  need staged; check_manifest
  has rehearsed && { log 'already rehearsed'; return; }
  if ! gene_changes; then
    log "Gene stays at ${G:0:7}: no database migration to rehearse"
    mset downgrades ''
    mark rehearsed
    return
  fi
  local old new owner
  old=$(jget "$MANIFEST" old.runtime.id); new=$(jget "$MANIFEST" new.runtime.id); owner=$(db_owner)
  (
    test=$STAGE/dbtest
    [[ ! -e $test ]] || { echo "FAIL: $test exists; remove it first" >&2; exit 1; }
    trap 'rm -rf "$test"' EXIT  # The copies hold member data; never leave them behind.
    install -d -m 700 -o "${owner%:*}" -g "${owner#*:}" "$test"
    run=(forward_docker run --rm --network none --user "$owner" --read-only --tmpfs /tmp --cap-drop ALL
         --security-opt no-new-privileges:true --env-file "$ROOT/secrets/controller.env" -v "$test:/data")
    # Asked of both images with a probe that answers yes or no, so one that fails to start stops here
    # rather than skipping a step. The rollback runs exactly the recorded list.
    downgrades=()
    for module in "${DOWNGRADE_MODULES[@]}"; do
      if has_module "$module" "${run[@]}" "$new" && ! has_module "$module" "${run[@]}" "$old"; then downgrades+=("$module"); fi
    done
    mset downgrades "${downgrades[*]}"
    echo "a rollback downgrades: ${downgrades[*]:-nothing, the old release has every feature G has a downgrade for}"
    for copy in forward downgrade; do open_for "the $copy copy"; db_copy "$test" "$copy.sqlite3"; done
    start='import sys
from fastapi.testclient import TestClient
from gene.api import create_app
client = TestClient(create_app())
assert client.get("/health").status_code == 200
print(sys.argv[1], "controller starts; status keys:", sorted(client.app.state.store.status()))'
    open_for 'the forward rehearsal'
    "${run[@]}" -e GENE_DB=/data/forward.sqlite3 "$new" python -c "$start" new
    "${run[@]}" -e GENE_DB=/data/forward.sqlite3 "$old" python -c "$start" old-on-migrated
    "${run[@]}" -e GENE_DB=/data/forward.sqlite3 "$old" python -c '
import uuid
from gene.config import Settings
from gene.store import Store
settings = Settings.from_env()
job = Store(settings.db).request(settings.guild_id, "0", "rehearsal:"+uuid.uuid4().hex, "", "rehearsal write")
assert isinstance(job, dict) and job.get("id"), job
print("old code wrote a request on the migrated schema:", job["state"])'
    open_for 'the downgrade rehearsal'
    "${run[@]}" -e GENE_DB=/data/downgrade.sqlite3 "$new" python -c "$start" new
    for module in "${downgrades[@]}"; do
      if [[ $module == gene.profile_downgrade ]]; then
        # A profile removal on the copy, so the downgrade must keep it in every configured channel. Without
        # the channels in controller.env it refuses, which a rollback must not find out halfway through.
        "${run[@]}" -e GENE_DB=/data/downgrade.sqlite3 "$new" python -c '
import sqlite3
from gene.config import Settings
with sqlite3.connect(Settings.from_env().db) as db:
    db.execute("INSERT INTO personal_note_removals VALUES (?,?,?,?,?)", ("0", "server", "0", "rehearsal", 0))'
      fi
      downgrade_step "$module" /data/downgrade.sqlite3 "${run[@]}" "$new"
      if [[ $module == gene.profile_downgrade ]]; then
        "${run[@]}" -e GENE_DB=/data/downgrade.sqlite3 "$new" python -c '
import sqlite3
from gene.config import Settings
with sqlite3.connect(Settings.from_env().db) as db:
    kept = db.execute("SELECT count(*) FROM personal_note_removals WHERE digest=? AND channel_id!=?", ("rehearsal", "server")).fetchone()[0]
    left = db.execute("SELECT count(*) FROM personal_note_removals WHERE channel_id=?", ("server",)).fetchone()[0]
assert kept and not left, (kept, left)
print("profile downgrade keeps a removal in", kept, "channels")'
      fi
    done
    "${run[@]}" -e GENE_DB=/data/downgrade.sqlite3 "$old" python -c "$start" old-after-downgrade
  )
  mark rehearsed
}

state_value() {  # dotted.key of a fresh state read; use it in an assignment, so a failed read stops the phase
  local snapshot
  snapshot=$(state)
  jget <(echo "$snapshot") "$1"
}

fence() {  # Pause new work, let current work finish, then stop the worker and deployer.
  # Checked against the live setting every time, so a fence after resume pauses again. The intent
  # line and the pause's audit id are the ownership record: resume unpauses only while the latest pause
  # in the journal is ours and the controller has recorded no pause or resume after it. The pause itself
  # rechecks the setting, so one made between this read and it is never taken for ours.
  local paused busy waited=0
  paused=$(state_value settings.paused)
  [[ $paused == true || $paused == false ]] || die "unexpected paused setting: $paused"
  if [[ $paused == false ]]; then
    mark intent:pause
    pause_development
  fi
  while true; do
    busy=$(state_value busy_jobs)
    [[ $busy == 0 && ! -e $ROOT/deploy-result.json ]] && break
    (( waited < WAIT_WINDOW )) || die 'work is still building or deploying after 30 minutes; development stays paused'
    log 'waiting for current work to finish'; sleep 30; waited=$(( waited + 30 ))
  done
  systemctl stop gene-worker gene-deployer
  if systemctl is-active -q gene-worker || systemctl is-active -q gene-deployer; then die 'worker or deployer still active'; fi
  busy=$(state_value busy_jobs)
  [[ $busy == 0 ]] || die 'a job started while stopping the units'
  [[ ! -e $ROOT/deploy-result.json ]] || die 'a deployment receipt appeared'
  log 'development fenced: paused, idle, worker and deployer stopped'
}

drain() {
  need baseline; check_manifest
  fence
  if ! has drained; then
    local used
    # The fence waits out a deployment claimed before the pause, and that one used today's batch. This
    # batch changed nothing but the fence, and the deployment may have moved Gene, so development
    # resumes as it is, without the old-pair check resume-old makes. The batch never goes forward.
    if [[ -z $EXCEPTION && -z $CRITICAL ]] && used=$(drained_today); then
      log "today's batch was used during the drain ($used)"
      resume_as_is
      die "today's batch was used during the drain ($used); $(resumed_as), and this pair waits for a later day"
    fi
    audit "$DRAINED" "${STAGE##*/}"  # Before the journal line, so a drained batch always holds the controller's claims.
    mark drained
  fi
  if closed_during 'the drain'; then
    resume_old
    die 'the window closed during the drain; development resumed at the old release, and this pair waits for a later window'
  fi
}

on_gene_error() {
  # errtrace also fires this inside command substitutions; only the main shell rolls back.
  [[ $BASHPID == "$$" ]] || exit 1
  trap - ERR
  SWITCHING=0
  echo 'FAIL: Gene switch failed; rolling back' >&2
  rollback_gene
  exit 1
}

gene_disk() {  # What the switch itself writes: the backups, the cogs and the source move.
  disk_admission "gene_backup=$ROOT/backups:$(( $(du_bytes "$ROOT/data/controller") + $(du_bytes "$ROOT/data/red") + $(du_bytes "$ROOT/source") ))" \
    "releases=$ROOT/releases:$(du_bytes "$STAGE/cogs-ready")" "source=$ROOT:$(du_bytes "$STAGE/gene-src")"
}

backup_db() {  # dir: an online copy at dir/db/controller.sqlite3, made as the DB owner, then root-only
  local owner
  owner=$(db_owner)
  install -d -m 700 -o "${owner%:*}" -g "${owner#*:}" "$1/db"
  db_copy "$1/db" controller.sqlite3
  chown -R 0:0 "$1/db"
  chmod 600 "$1/db/controller.sqlite3"
}

gene() {
  need staged rehearsed drained; check_manifest
  no_rollback
  if ! gene_changes; then
    if ! has gene_recorded; then
      fence  # Re-verified, not trusted from the drain marker.
      verify old
      mark gene_recorded
    fi
    log "Gene stays at ${G:0:7}"
    return
  fi
  if has gene_recorded; then verify_gene_only; return; fi
  has gene_attempted && die 'an earlier Gene attempt is in the journal; finish rollback-gene, then stage again in a fresh directory'
  fence  # Re-verified, not trusted from the drain marker.
  verify old
  [[ ! -e $ROOT/releases/$G ]] || die "$ROOT/releases/$G appeared after staging"
  [[ $(tree_hash "$STAGE/cogs-ready") == "$(jget "$MANIFEST" cogs)" ]] || die 'staged cogs changed'
  rename_preflight "$ROOT/backups"
  gene_disk
  quiet "$WAIT_WINDOW"
  verify old  # The wait can be long; the backup must describe the release it backs up.
  local B
  B=$ROOT/backups/maintainer-batch-$G-$(date +%s)
  mkdir -m 700 "$B"
  backup_db "$B"
  # cp -p keeps each file's mode and owner, so a rollback puts back exactly what was there.
  cp -p "$ROOT/release.env" "$B/release.env"
  cp -p "$ROOT/secrets/worker.env" "$B/worker.env"
  cp -p "$FRI_ROOT/release.env" "$B/fri-release.env"
  cp -p "$FRI_ROOT/release.json" "$B/fri-release.json"
  mset backup "$B"
  rename_preflight "$B"
  # Nothing has changed yet. From here on every failure rolls back from this complete backup.
  if closed_during 'the Gene backup'; then
    resume_old
    die 'the window closed before the Gene switch; nothing switched, development resumed at the old release'
  fi
  mark gene_attempted
  SWITCHING=1
  trap on_gene_error ERR
  gene_disk
  mark intent:cogs
  rename_tree "$STAGE/cogs-ready" "$ROOT/releases/$G"
  quiet 0  # A fresh player snapshot right before Red stops.
  mark intent:red_stop
  local stopped
  stopped=$(date +%s 2>/dev/null || true)
  compose_gene stop red
  ! container_running "$RED" || die 'Red did not stop'
  tar -C "$ROOT/data" -czf "$B/red.tar.gz" red
  mark intent:source_swap
  rename_tree "$ROOT/source" "$B/installed-source"
  rename_tree "$STAGE/gene-src" "$ROOT/source"
  mark intent:env
  cp -p "$ROOT/release.env" "$ROOT/release.env.tmp"
  printf 'GENE_RELEASE_SHA=%s\nGENE_ROOT=%s\nGENE_RUNTIME_IMAGE=gene-runtime:%s\n' "$G" "$ROOT" "$G" > "$ROOT/release.env.tmp"
  mv "$ROOT/release.env.tmp" "$ROOT/release.env"
  mark intent:worker
  sed -i "s|^GENE_WORKER_IMAGE=.*|GENE_WORKER_IMAGE=gene-worker:$G|" "$ROOT/secrets/worker.env"
  [[ $(grep -Fxc -- "GENE_WORKER_IMAGE=gene-worker:$G" "$ROOT/secrets/worker.env" || true) == 1 ]] || die 'worker image not pinned'
  [[ $(stat -c %a:%u:%g "$ROOT/secrets/worker.env") == $(stat -c %a:%u:%g "$B/worker.env") ]] || die 'worker.env mode changed'
  local started
  started=$(epoch)
  mark intent:start_new
  repair_sidecars
  compose_gene up -d --no-deps --force-recreate controller red
  probe_gene "$G" "$started" "$(jget "$MANIFEST" new.runtime.id)"
  RESTART_S=$(since "$stopped")
  mark intent:record
  settings_transition "$OLD_G" "$(jget "$BASELINE" settings.previous_sha)" "$G" "$OLD_G" maintainer_deployed
  mark gene_recorded
  trap - ERR
  SWITCHING=0
  verify_gene_only
}

verify_gene_only() {  # After the Gene switch, before Fri: Gene new, Fri still old and alive.
  local s; s=$(state)
  python3 - "$s" "$G" "$OLD_G" <<'PY'
import json, sys
s, g, old = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
assert s['settings'].get('active_sha') == g and s['settings'].get('previous_sha') == old, s['settings']
assert s['gene']['sha'] == g and s['gene_env']['GENE_RELEASE_SHA'] == g and s['worker_pin'] == 'gene-worker:'+g, s
assert s['source'] == g, s['source']
print('Gene switched to', g[:7], '; Fri still', s['fri']['sha'][:7])
PY
}

restore_file() {  # backup live: the backup's exact bytes, mode and owner, put back atomically
  if cmp -s "$1" "$2" && [[ $(stat -c %a:%u:%g "$1") == "$(stat -c %a:%u:%g "$2")" ]]; then return 0; fi
  cp -p "$1" "$2.restore-tmp"
  mv "$2.restore-tmp" "$2"
  cmp -s "$1" "$2" || die "$2 not restored"
}

old_fri_pinned() {  # The old Fri's exact pins and record are in place and its image is running.
  # Health is checked separately: a broken new controller can degrade an old Fri's heartbeat.
  local B
  B=$(backup_dir)
  cmp -s "$B/fri-release.env" "$FRI_ROOT/release.env" && cmp -s "$B/fri-release.json" "$FRI_ROOT/release.json" || return 1
  python3 - "$(state)" "$OLD_F" "$(jget "$MANIFEST" old.fri.id)" <<'PY'
import json, sys
s, old_f, image = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.exit(0 if (s['settings'].get('fri_active_sha') in (None, old_f) and s['fri_container']['running']
               and s['fri_container']['image'] == image) else 1)
PY
}

lock_fri() {  # gene.fri_release's own lock, held until this process exits, so no coordinator runs meanwhile
  exec 9>> "$FRI_ROOT/release.lock"
  flock -n 9 || die 'a Fri release holds its lock; let it finish, then repeat rollback-gene'
}

restore_old_fri() {
  # Called with the Fri lock held. Before any Gene downgrade, the Fri that ran against the old
  # controller must be back. This puts the pre-switch pins and record back byte for byte, so it
  # also reconciles an interrupted Fri release, and it needs no healthy controller.
  if old_fri_pinned; then log "Fri $OLD_F is pinned and running; its health is checked once Gene is restored"; return 0; fi
  local B
  B=$(backup_dir)
  log "restoring Fri $OLD_F from the switch backup"
  mark intent:fri_restore
  restore_file "$B/fri-release.env" "$FRI_ROOT/release.env"
  fri_compose stop runtime
  ! container_running "$FRI" || die 'Fri did not stop'
  restore_file "$B/fri-release.json" "$FRI_ROOT/release.json"
  set_fri_active "$OLD_F"
  fri_compose up -d --no-deps --force-recreate runtime
  old_fri_pinned || die 'Fri restore finished but its state does not match the old release'
  mark fri_restored
}

rollback_gene() {
  # Reconciles from actual state, so it is safe to repeat. errexit stays on: any failure stops here.
  trap - ERR
  SWITCHING=0
  local B prev old_id owner
  B=$(backup_dir)
  prev=$(jget "$BASELINE" settings.previous_sha)
  old_id=$(jget "$MANIFEST" old.runtime.id)
  owner=$(db_owner)
  repair_sidecars
  log 'rollback: check that live state is built from the recorded releases'
  pair_check
  # From here the forward phases refuse: a half-done rollback is finished by repeating rollback-gene.
  has intent:rollback || mark intent:rollback
  log 'rollback: fence development'
  fence
  lock_fri
  # The fence can wait for work that finishes a deployment, so check again with nothing left running.
  log 'rollback: check again with development fenced and Fri releases locked out'
  pair_check
  log 'rollback: Fri first'
  restore_old_fri
  if has intent:start_new && ! has done:downgrade; then
    log 'rollback: stop the new controller and Red, then downgrade the database with G tools'
    compose_gene stop controller red
    if container_running "$CONTROLLER" || container_running "$RED"; then die 'controller or Red still running'; fi
    repair_sidecars
    local run=(docker run --rm --network none --user "$owner" --read-only --tmpfs /tmp --cap-drop ALL
               --security-opt no-new-privileges:true --env-file "$ROOT/secrets/controller.env"
               -v "$ROOT/data/controller:/data" "$(jget "$MANIFEST" new.runtime.id)")
    local list downgrades module
    list=$(jget "$MANIFEST" downgrades)  # Worked out and run on a copy by rehearse.
    read -ra downgrades <<< "$list"
    for module in "${downgrades[@]}"; do downgrade_step "$module" /data/gene.sqlite3 "${run[@]}"; done
    mark done:downgrade
  fi
  log 'rollback: pins'
  restore_file "$B/release.env" "$ROOT/release.env"
  restore_file "$B/worker.env" "$ROOT/secrets/worker.env"
  log 'rollback: source'
  if [[ -f $ROOT/source/.release-sha && $(cat "$ROOT/source/.release-sha") == "$G" ]]; then
    local failed=$B/failed-source
    [[ ! -e $failed ]] || failed=$B/failed-source-$(date +%s%N 2>/dev/null || echo "$$-$RANDOM")
    rename_tree "$ROOT/source" "$failed"
  fi
  if [[ ! -e $ROOT/source ]]; then
    [[ -d $B/installed-source ]] || die 'neither the new nor the old source is in place; operator recovery required'
    rename_tree "$B/installed-source" "$ROOT/source"
  fi
  [[ $(source_state) == "$(jget "$BASELINE" source)" ]] || die 'old source not in place'
  log 'rollback: old controller and Red'
  if [[ $(container_image "$CONTROLLER") == "$old_id" && $(container_image "$RED") == "$old_id" ]] \
     && container_running "$CONTROLLER" && container_running "$RED" && probe_gene "$OLD_G" 0 "$old_id"; then
    log 'old controller and Red already running'
  else
    local started
    started=$(epoch)
    repair_sidecars
    compose_gene up -d --no-deps --force-recreate controller red
    probe_gene "$OLD_G" "$started" "$old_id"
  fi
  log 'rollback: release settings'
  settings_transition "$G" "$OLD_G" "$OLD_G" "$prev" maintainer_rollback
  probe_fri "$OLD_F" "$(jget "$MANIFEST" old.fri.id)"
  verify old
  has gene_rolled_back || mark gene_rolled_back
  log 'Gene rolled back. Workers stay stopped and development stays paused; run resume-old when settled.'
}

fri() {
  need gene_recorded; check_manifest
  no_rollback
  if has fri_switched || ! fri_changes; then
    probe_fri "$F" "$(jget "$MANIFEST" new.fri.id)"  # Unchanged: the live Fri, now against the new Gene.
    has fri_switched || mark fri_switched
    return
  fi
  if ! gene_changes && ! has fri_backup; then
    # The pins and record rollback-fri restores. Taken once, before the first attempt: a retry after a
    # crash must not record its partial state as the old release.
    fence
    verify old
    local B
    B=$ROOT/backups/maintainer-fri-$F-$(date +%s)
    mkdir -m 700 "$B"
    cp -p "$FRI_ROOT/release.env" "$B/fri-release.env"
    cp -p "$FRI_ROOT/release.json" "$B/fri-release.json"
    mset backup "$B"
    mark fri_backup
  fi
  if closed_during 'the Fri preparation'; then
    if gene_changes; then rollback_gene; fi
    resume_old
    die 'the window closed before the Fri switch; development resumed at the old release'
  fi
  mark intent:fri
  local switching
  switching=$(date +%s 2>/dev/null || true)
  if ! "$ROOT/venv/bin/python" -m gene.fri_release "$F"; then
    repair_sidecars
    echo 'FAIL: Fri release failed. State for reconciliation:' >&2
    state
    local latest
    latest=$(find "$FRI_ROOT/backups" -maxdepth 1 -name "*-$F" -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)
    if [[ -n $latest && -f $latest/failure.json ]]; then cat "$latest/failure.json"; fi
    if gene_changes; then die 'workers stay stopped; rollback-gene restores the old Fri first, from any partial state'; fi
    die 'workers stay stopped; gene.fri_release put the old Fri back if it could, and rollback-fri restores it from any partial state'
  fi
  repair_sidecars
  probe_fri "$F" "$(jget "$MANIFEST" new.fri.id)"
  RESTART_S=$(since "$switching")
  mark fri_switched
}

evals() {
  need fri_switched; check_manifest
  mark evals_started  # Whatever an earlier run passed or had accepted no longer counts.
  local suite failed=()
  for suite in requests sass controls; do
    if closed_during "the evals (before $suite)"; then
      if gene_changes; then rollback_gene; else rollback_fri; fi
      resume_old
      die 'the window closed before the evals finished; the release was rolled back and development resumed at the old release'
    fi
    log "eval suite $suite"
    if docker exec "$FRI" python -m fri.fri_eval --suite "$suite" > "$STAGE/eval-$suite.jsonl" 2>&1; then
      echo "suite $suite passed"
    else
      echo "suite $suite FAILED"
      failed+=("$suite")
    fi
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
  log 'fri health: heartbeat stages and rejected usage receipts'
  python3 - "$FRI_HEALTH" <<'PY'
import json, sys
health = json.load(open(sys.argv[1]))
stages = (health.get('agent_heartbeat') or {}).get('stages') or {}
failing = {name: stage.get('error') for name, stage in stages.items() if stage.get('error')}  # exception class names
print('  stages:', sorted(stages), '| failing:', failing or 'none', '| rejected usage receipts:', health.get('rejected_usage'))
PY
  mark evals_ran
  # Every suite passed. With failures, keeping the release is the eval rule's call: accept-evals records it.
  if (( ${#failed[@]} == 0 )); then mark evaluated; fi
  (( ${#failed[@]} == 0 )) || die "eval suites failed: ${failed[*]}; decide with the owner whether to roll back ($(gene_changes && echo rollback-gene || echo rollback-fri))"
}

verify_off() {
  verify_off_check
  if [[ -d $STAGE ]]; then has verified_off || mark verified_off; fi
}

accept_evals() {  # The eval rule kept a release whose suites had failures (known misses, a passing rerun).
  latest evals_ran || die 'the last evals run has not finished; run evals'
  [[ -n ${EVALS_ACCEPTED:-} ]] || die 'set EVALS_ACCEPTED to why the eval rule keeps this release'
  latest evaluated || mark evaluated
  log "evals accepted: $EVALS_ACCEPTED"
}

verify_off_check() {
  db_py <<'PY'
import json, sqlite3
with sqlite3.connect('file:/data/gene.sqlite3?mode=ro', uri=True, timeout=30) as db:
    row = db.execute("SELECT json_extract(value,'$.reply_check_enabled') FROM agent_state WHERE key='config'").fetchone()
    runs = db.execute('SELECT count(*) FROM fri_reply_check_runs').fetchone()[0]
print(json.dumps({'reply_check_enabled': row[0] if row else None, 'reply_check_runs': runs}))
if (row and row[0]) or runs:
    raise SystemExit('reply checks are not off')
PY
}

resume_services() {  # gene_sha
  systemctl start gene-worker gene-deployer || die 'the worker and deployer did not start'
  systemctl is-active -q gene-worker || die 'gene-worker did not start'
  systemctl is-active -q gene-deployer || die 'gene-deployer did not start'
  local said rc=0
  if owns_pause; then
    said=$(unpause_if_ours) || rc=$?
    case $rc in
      0) mark done:unpause; log "$said" ;;
      3) NOTE=$said; log "$said" ;;  # The log line and the exit message carry it too.
      *) die "could not resume development: ${said:-no answer from the controller DB}" ;;
    esac
  elif said=$(pause_kept); then  # Someone else's pause stays, and is said the same way.
    if [[ -n $said ]]; then NOTE=$said; log "$said"; fi
  else
    NOTE='development was left as it was, and whether it is still paused could not be read'
    log "WARN: $NOTE"
  fi
  audit maintainer_services_resumed "$1"
}
resume_as_is() {  # After a fence that Gene may have moved under (a deployment landing during its wait): this
  # batch changed nothing but the fence, so development goes back as it is, without the old-pair check. The
  # stop is journaled before anything is restored: if restoring stops, resume-old replays it, at any hour.
  local live
  aborting || mark intent:abort
  live=$(live_sha "$ROOT/release.env" GENE_RELEASE_SHA)
  log "resuming development as it is, at Gene ${live:0:7}"
  resume_services "$live"
  mark resumed_old
}
aborting() {  # A stop after a fence whose restore hasn't finished
  [[ -f $JOURNAL && $({ grep -x -e intent:abort -e resumed_old "$JOURNAL" || true; } | tail -n 1) == intent:abort ]]
}
gene_moved() {  # The live Gene isn't the old release: before a batch drains, only a deployment moves it
  local live
  live=$(live_sha "$ROOT/release.env" GENE_RELEASE_SHA)
  [[ $live =~ ^[0-9a-f]{40}$ && $live != "$OLD_G" ]]
}
resumed_as() {  # How development came back, for an exit message
  if [[ -n $NOTE ]]; then echo "the worker and deployer are running again, but $NOTE"; else echo 'development resumed as it was'; fi
}

write_result() {  # The deployer's receipt for this release; a repeat accepts the same receipt.
  python3 - "$RESULT" "$G" "$OLD_G" "$F" "$OLD_F" "$(backup_dir)" <<'PY'
import json, os, sys, tempfile, time
path, sha, previous, fri, fri_previous, backup = sys.argv[1:7]
result = {'sha': sha, 'previous': previous, 'success': True, 'recovered': False, 'backup': backup,
          'fri_sha': fri, 'fri_previous': fri_previous}
def matches():
    existing = json.load(open(path))
    if {k: existing.get(k) for k in result} != result:
        sys.exit(path+' already exists with a different result')
    print('result already written')
if os.path.exists(path):
    matches()
    sys.exit(0)
# Written in full to this writer's own temporary beside the final name, then linked into place:
# the final name never holds a partial file, and link() refuses to replace one that appeared meanwhile.
fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path)+'.')
try:
    with os.fdopen(fd, 'w') as out:
        json.dump(result | {'at': time.time()}, out)
        out.flush()
        os.fsync(out.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        matches()
finally:
    os.unlink(temporary)
directory = os.open(os.path.dirname(path), os.O_RDONLY)
os.fsync(directory)
os.close(directory)
PY
}

no_rollback() {
  if has intent:rollback; then die "a rollback has started; repeat $(gene_changes && echo rollback-gene || echo rollback-fri) until it finishes, then resume-old"; fi
}

resume() {
  need gene_recorded fri_switched; check_manifest
  no_rollback
  if has resumed; then
    verify new any-work
    if ! systemctl is-active -q gene-worker || ! systemctl is-active -q gene-deployer; then die 'resumed earlier, but the units are not running'; fi
    log 'already resumed'
    return
  fi
  if ! has intent:services; then
    verify new
    mark intent:services
  else
    verify new any-work  # Resumed units may already be running jobs.
  fi
  resume_services "$G"
  write_result
  mark resumed
  state
}

resume_old() {
  check_manifest
  if has gene_attempted && ! has gene_rolled_back; then die 'a Gene switch was attempted; run rollback-gene first'; fi
  touched || die "phase needs 'drained' first"  # A drain that timed out after pausing still resumes here.
  # A stop after a fence whose restore didn't finish, or a batch that never drained and whose Gene a deployment
  # has moved since: the old pair is gone, and the batch changed nothing but its fence.
  if aborting || { ! has drained && gene_moved; }; then resume_as_is; return; fi
  if ! has intent:services_old; then
    verify old
    mark intent:services_old
  else
    verify old any-work
  fi
  resume_services "$OLD_G"
  has resumed_old || mark resumed_old
}

fri_pair_check() {  # rollback-fri only reconciles a live Fri built from the recorded old and new releases.
  python3 - "$(state)" "$MANIFEST" "$(backup_dir)" "$FRI_ROOT" "$F" "$OLD_F" <<'PY'
import json, sys
s, manifest = json.loads(sys.argv[1]), json.load(open(sys.argv[2]))
B, fri_root, F, OLD_F = sys.argv[3:7]
def read(path):  # compared only, never printed
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except FileNotFoundError:
        return None
saved = {name: read(B+'/'+name) for name in ('fri-release.env', 'fri-release.json')}
if None in saved.values():
    sys.exit('the Fri backup in '+B+' is incomplete')
try:
    record = json.loads(read(fri_root+'/release.json') or b'null')
except ValueError:
    record = None
checks = {
    'fri release.env': read(fri_root+'/release.env') in (saved['fri-release.env'], saved['fri-release.env'].replace(OLD_F.encode(), F.encode())),
    'fri release.json': (read(fri_root+'/release.json') == saved['fri-release.json']
                         or (isinstance(record, dict) and record.get('sha') == F and record.get('previous') == OLD_F)),
    'fri_active_sha': s['settings'].get('fri_active_sha') in (None, OLD_F, F),
    'fri image': s['fri_container']['image'] in (manifest['old']['fri']['id'], manifest['new']['fri']['id'], 'none'),
}
bad = [k for k, v in checks.items() if not v]
if bad:
    sys.exit('live Fri is outside the recorded old/new pair ('+', '.join(bad)+'); reconcile by hand, nothing was changed')
print('live Fri is built from the recorded old and new releases')
PY
}

gene_unmoved() {  # A Fri-only rollback puts Fri back beside the Gene it left alone, so that Gene must still be live.
  python3 - "$(state)" "$MANIFEST" "$BASELINE" "$G" <<'PY'
import json, sys
s, manifest, baseline, G = json.loads(sys.argv[1]), json.load(open(sys.argv[2])), json.load(open(sys.argv[3])), sys.argv[4]
runtime, worker = manifest['old']['runtime'], manifest['old']['worker']
checks = {
    'active_sha': s['settings'].get('active_sha') == G,
    'release.env': s['gene_env'] == {'GENE_RELEASE_SHA': G, 'GENE_RUNTIME_IMAGE': runtime['tag']},
    'worker_pin': s['worker_pin'] == worker['tag'],
    'source': s['source'] == baseline['source'],
    'controller_image': s['controller']['image'] == runtime['id'],
    'red_image': s['red']['image'] == runtime['id'],
}
bad = [k for k, v in checks.items() if not v]
if bad:  # A member request deployed since, say: the old pair this rollback restores no longer exists.
    sys.exit('Gene is no longer '+G[:7]+' ('+', '.join(bad)+'); rollback-fri only puts Fri back beside it. Reconcile by hand')
print('Gene is still '+G[:7])
PY
}

rollback_fri() {
  # A release that leaves Gene alone: putting the old Fri back is the whole rollback. It reconciles from
  # actual state through the backup the fri phase took, so it is safe to repeat.
  gene_changes && die 'this release changes Gene; rollback-gene puts the old Fri back first'
  need fri_backup; check_manifest
  aborting && die 'nothing was changed: an earlier rollback-fri stopped before it restored development, so run resume-old first'
  fri_pair_check
  gene_unmoved || die 'nothing was changed'
  fence
  lock_fri
  fri_pair_check  # The fence can wait for a deployment that lands a third Fri,
  if ! gene_unmoved; then  # or a Gene release: development goes back as it was, with Fri untouched.
    resume_as_is
    die "Gene moved during the fence; Fri was not changed and $(resumed_as)"
  fi
  has intent:rollback || mark intent:rollback  # From here on, only a repeat of rollback-fri finishes it.
  restore_old_fri
  probe_fri "$OLD_F" "$(jget "$MANIFEST" old.fri.id)"
  verify old
  has fri_rolled_back || mark fri_rolled_back
  log 'Fri rolled back. Workers stay stopped and development stays paused; run resume-old when settled.'
}

prune_images() {  # Release images nothing recent needs. Docker also keeps any a container uses.
  local keep repo tag
  # Kept: this release's old and new images; the images the live pins name, whichever release set them,
  # since an idle worker has no container to protect its image; the old and new images of the
  # KEEP_RELEASES most recently resumed releases, so a rollback a few releases deep still finds them;
  # and every image of a release that is not back in service yet.
  # Only the pin lines are read from the env files, and they are compared, never printed.
  keep=$(python3 - "$MANIFEST" "$ROOT/staging" "$KEEP_RELEASES" "$ROOT/release.env" GENE_RUNTIME_IMAGE \
    "$ROOT/secrets/worker.env" GENE_WORKER_IMAGE "$FRI_ROOT/release.env" FRI_RUNTIME_IMAGE <<'PY'
import glob, json, os, sys
manifest, staging, depth, *pins = sys.argv[1:]
def images(path):
    m = json.load(open(path))
    return {m[kind][name]['tag'] for kind in ('old', 'new') for name in ('runtime', 'worker', 'fri') if name in m.get(kind, {})}
keep = images(manifest)
for path, key in zip(pins[::2], pins[1::2]):
    with open(path) as handle:
        keep |= {line.split('=', 1)[1].strip() for line in handle if line.startswith(key+'=')}
resumed, unfinished = [], []
for journal in glob.glob(staging+'/batch-*/journal'):
    with open(journal) as handle:
        marks = set(handle.read().split())
    path = os.path.dirname(journal)+'/manifest.json'
    if 'resumed' in marks:
        resumed.append((os.stat(journal).st_mtime, path))
    # Not yet back in service: staged, drained or switching, or mid-rollback. Its next phase checks
    # every image its manifest names, so all of them stay, however old.
    if 'resumed_old' not in marks and ('resumed' not in marks or 'intent:rollback' in marks):
        unfinished.append(path)
for path in [path for _, path in sorted(resumed, reverse=True)[:int(depth)]] + unfinished:
    try:
        keep |= images(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # An unreadable manifest only means fewer images are kept for its phases.
print('\n'.join(sorted(keep)))
PY
  ) || die 'could not work out which images the live and recent releases need; no images removed'
  for repo in gene-runtime gene-worker fri-runtime; do
    while read -r tag; do
      [[ $tag =~ ^$repo:[0-9a-f]{40}$ ]] || continue  # Only release tags; bootstrap and others stay.
      grep -Fxq -- "$tag" <<< "$keep" && continue
      if docker image rm "$tag" > /dev/null; then log "removed image $tag"; else log "kept image $tag: docker would not remove it"; fi
    done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' "$repo")
  done
}

cleanup() {
  has resumed || has resumed_old || die 'resume first'
  rm -rf "$STAGE/fri-src" "$STAGE/gene-src" "$STAGE/cogs-ready" "$STAGE/dbtest" "$STAGE"/*.tar "$STAGE"/*.tar.sha256
  if [[ ${KEEP_IMAGES:-} == 1 ]]; then log 'KEEP_IMAGES=1: no images removed'; else prune_images; fi
  log "kept: journal, manifest, baseline and evals in $STAGE; backup $(backup_dir 2>/dev/null || echo none); the live release's images and the old and new images of the last $KEEP_RELEASES releases"
  # Backups hold copies of member data, so removing them is the owner's call.
  local dir
  for dir in "$ROOT/backups" "$FRI_ROOT/backups"; do
    log "$dir: $(find "$dir" -mindepth 1 -maxdepth 1 | wc -l) backups, $(du -sh "$dir" | cut -f1); none removed"
  done
}

# Production maintenance window (owner policy, 2026-10-27; widened 2026-10-28). Forward phases start
# only in WINDOW, following daylight saving, and one production batch runs per local day across
# both routes: a batch that drains here, or an automatic feature or repair deployment. A wait that
# crosses the close takes the batch's safe path instead of switching. Recovery runs at any hour, and
# an unknown clock or zone counts as closed. MAINTENANCE_EXCEPTION carries the owner's explicit
# exception; CRITICAL_FIX, a P0 fix's reason, lifts only the daily cap. The log records both.
# src/gene/maintenance.py holds the same window and cap for the deployer; tests/test_maintenance.py
# keeps WINDOW_OPENS and WINDOW_CLOSES equal to its OPENS and CLOSES.
WINDOW_OPENS=03:30
WINDOW_CLOSES=07:30
WINDOW="[$WINDOW_OPENS, $WINDOW_CLOSES) America/Los_Angeles"
ACTIONS=$ROOT/production-actions.jsonl
EXCEPTION=${MAINTENANCE_EXCEPTION:-}
CRITICAL=${CRITICAL_FIX:-}
[[ $CRITICAL =~ [^[:space:]] ]] || CRITICAL=''  # A blank reason is no reason.
DRAINED=batch_drained  # The controller's audit action for a drained batch: maintenance.DRAINED holds claims for the day.
STARTED_AT=$(date +%s 2>/dev/null || true)  # Instrumentation only: an unreadable clock never blocks recovery.
RESTART_S='' REFUSED='' NOTE=''  # NOTE: why resume_services left development as it was
now_epoch() { TZ=UTC date +%s; }  # The window's clock; the offline tests shift only this call.
window() {  # epoch: "open|closed <local date> <local time>"; fails on an unknown zone or clock
  python3 - "$1" "$WINDOW_OPENS" "$WINDOW_CLOSES" <<'PY'
import datetime as dt, sys
from zoneinfo import ZoneInfo
stamp, opens, closes = int(sys.argv[1]), dt.time.fromisoformat(sys.argv[2]), dt.time.fromisoformat(sys.argv[3])
if stamp < 1793059200:  # 2026-10-27T00:00Z: a clock that reads earlier than the policy is wrong
    sys.exit('the clock reads before 2026-10-27')
local = dt.datetime.fromtimestamp(stamp, ZoneInfo('America/Los_Angeles'))
print('open' if opens <= local.time() < closes else 'closed', local.date().isoformat(), local.isoformat())
PY
}
window_open() {
  local now state
  now=$(now_epoch) && [[ $now =~ ^[0-9]+$ ]] || return 1
  state=$(window "$now") || return 1
  [[ $state == open\ * ]]
}
forward_docker() {  # docker for stage and rehearse: each container starts inside the window or not at all
  open_for "the next $PHASE container"
  docker "$@"
}
clear_interrupted() {  # path: a tree stage generates, left by a stage the close cut off; nothing live uses it yet
  [[ -e $1 ]] || return 0
  has staged && die "$1 exists after staging; inspect it"
  log "removing ${1##*/}, left by an interrupted stage"
  rm -rf -- "$1"
}
open_for() {  # what: before a step that changes nothing live (a build), stop once the window has closed
  [[ -n $EXCEPTION ]] || window_open || die "the window closed before $1; production is untouched, so repeat this phase in the next window"
}
closed_during() {  # what: true, with a note, when the window closed during this phase's wait
  [[ -z $EXCEPTION ]] && ! window_open || return 1
  log "the maintenance window closed during $1: no switch starts now; taking this batch's safe path"
}
drained_today() {  # True, naming why, when today's batch is used: another batch drained, or an automatic
  # feature or repair deployment ran, on today's local date. Anything unknown counts as used. The
  # controller's own deployment record decides for automatic deployments, as /deploy/claim's cap does
  # (maintenance.batch_used); the action log only adds the lines a deployer wrote.
  local now rc=0
  now=$(now_epoch) && [[ $now =~ ^[0-9]+$ ]] || { echo 'the clock is unknown'; return 0; }
  python3 - "$now" "$ROOT/staging" "$STAGE" "$ACTIONS" "$DB" <<'PY' || rc=$?
import datetime as dt, glob, json, os, re, sqlite3, sys
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo
zone = ZoneInfo('America/Los_Angeles')
now, staging, stage, actions, database = int(sys.argv[1]), *sys.argv[2:6]
today = dt.datetime.fromtimestamp(now, zone).date()
deployment = re.compile(r'deploy (\S+) job \S+$')  # gene.deploy's line, whatever its outcome
def day(stamp):  # A deployment's local date from its time with offset; an undated one counts as today.
    try:
        moment = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return today
    return moment.astimezone(zone).date() if moment.tzinfo else today
logged = {}  # batch dir name -> local dates its drain completed, from the action log
if os.path.exists(actions):
    for line in open(actions, errors='replace'):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get('action') == 'batch-release drain' and row.get('outcome') == 'ok':
            logged.setdefault(row.get('stage'), set()).add(row.get('local_date'))
        found = deployment.match(row['action']) if isinstance(row.get('action'), str) else None
        # A rollback never counts. One dated after today counts: the clock went back.
        if found and found[1] != 'rollback' and day(row.get('at')) >= today:
            print('an automatic deployment ran today:', row['action'])
            sys.exit(0)
for journal in glob.glob(staging+'/batch-*/journal'):
    path = os.path.dirname(journal)
    if path == stage or 'drained' not in open(journal).read().split():
        continue
    # A batch drained before the log existed is dated by its journal's last change.
    dates = logged.get(os.path.basename(path)) or {
        dt.datetime.fromtimestamp(os.stat(journal).st_mtime, zone).date().isoformat()}
    if today.isoformat() in dates:
        print('a batch already drained today:', os.path.basename(path))
        sys.exit(0)
# Every deployment claimed since local midnight but a rollback, whatever its outcome, also one whose job is
# gone or one dated later (the clock went back). Read-only (mode=ro): a missing DB is an error, never created.
since = dt.datetime.combine(today, dt.time(0), tzinfo=zone).timestamp()
try:
    with closing(sqlite3.connect(Path(database).absolute().as_uri()+'?mode=ro', uri=True, timeout=30)) as db:
        claimed = db.execute("SELECT d.job_id, j.kind FROM deployments d LEFT JOIN jobs j ON j.id=d.job_id "
                             "WHERE d.at>=? AND j.kind IS NOT 'rollback' ORDER BY d.at LIMIT 1", (since,)).fetchone()
except sqlite3.Error as exc:
    print(f"the controller's deployment history could not be read ({exc}), so today's batch counts as used")
    sys.exit(0)
if claimed:
    print('the controller claimed an automatic deployment today:', claimed[1] or 'unrecorded', 'job', claimed[0])
    sys.exit(0)
sys.exit(3)
PY
  case $rc in
    0) return 0 ;;
    3) return 1 ;;
    *) echo "today's batches could not be read"; return 0 ;;
  esac
}
touched() { has drained || has intent:pause || has intent:abort; }  # This batch has fenced development, or begun to.
safe_path() {  # What this batch may do outside the window, from its journal.
  if ! touched; then echo 'Production is untouched; start again in the next window.'
  elif aborting; then echo 'Run resume-old: it resumes development as it is, without the old-pair check.'
  elif has resumed || has resumed_old; then echo 'This batch has resumed; cleanup waits for the next window.'
  elif latest evaluated; then echo 'Its evals passed: verify-off, then resume, run at any hour (or roll back).'
  elif latest evals_ran; then echo 'Its evals ran with failures: under the eval rule, accept-evals (EVALS_ACCEPTED=<why>), verify-off and resume, or roll back.'
  elif gene_changes_now && has gene_attempted && ! has gene_rolled_back; then echo 'Run rollback-gene, then resume-old.'
  elif has intent:fri && ! has fri_rolled_back; then echo 'Run rollback-fri, then resume-old.'
  else echo 'Run resume-old.'; fi
}
gene_changes_now() {  # gene_changes before OLD_G is known: from the manifest, if there is one
  [[ -f $MANIFEST ]] && [[ $G != "$(jget "$MANIFEST" old_gene)" ]]
}
refuse() { REFUSED=$1; echo "REFUSED: $1. $(safe_path)" >&2; exit 4; }
gate() {
  case $PHASE in
    rollback-gene|rollback-fri|resume-old) return 0 ;;  # Recovery runs at any hour.
    status|verify-off) touched && return 0 ;;  # Read checks within a batch that paused development.
    accept-evals) latest evals_ran && return 0 ;;  # The eval rule's decision on the last run; it changes no service.
    resume) latest evaluated && has verified_off && return 0 ;;  # Service resumes on a release that passed its gates.
  esac
  if has resumed_old && [[ $PHASE =~ ^(drain|gene|fri|evals|resume)$ ]]; then
    refuse 'this batch already resumed the old release; a retry is a new batch (BATCH_ATTEMPT) in a later window'
  fi
  if aborting && [[ $PHASE =~ ^(drain|gene|fri|evals|resume)$ ]]; then
    refuse 'this batch stopped after its fence and has not restored development yet; a retry is a new batch (BATCH_ATTEMPT) in a later window'
  fi
  if [[ -n $EXCEPTION ]]; then log "maintenance exception: $EXCEPTION"; return 0; fi
  window_open || refuse "outside the maintenance window $WINDOW, or the clock or zone is unknown"
  local used
  if [[ $PHASE =~ ^(baseline|stage|rehearse|drain)$ ]] && ! has drained && used=$(drained_today); then
    [[ -n $CRITICAL ]] || refuse "$used; one production batch per local day. Only a P0 fix goes ahead, with CRITICAL_FIX='<reason>'"
    log "critical fix: $CRITICAL ($used)"
  fi
}
live_sha() { sed -n "s/^$2=//p" "$1" 2>/dev/null | head -n 1 || true; }
since() {  # epoch: seconds from then to now, or nothing when either clock read failed
  local now
  now=$(date +%s 2>/dev/null) || return 0
  if [[ $1 =~ ^[0-9]+$ && $now =~ ^[0-9]+$ ]]; then echo $(( now - $1 )); fi
}
elapsed() { since "$STARTED_AT"; }
BEFORE_G=$(live_sha "$ROOT/release.env" GENE_RELEASE_SHA) BEFORE_F=$(live_sha "$FRI_ROOT/release.env" FRI_RELEASE_SHA)
log_action() {  # rc: this command's line in the production-action log; it never changes the outcome
  trap - ERR; set +e
  local rc=$1 outcome=ok
  [[ $PHASE == status ]] && return 0
  if [[ -n $REFUSED ]]; then outcome="refused: $REFUSED"; elif (( rc )); then outcome="failed: exit $rc"; fi
  python3 - "$ACTIONS" "$(now_epoch)" "$PHASE" "$(basename "$STAGE")" "$BEFORE_G" "$BEFORE_F" \
    "$(live_sha "$ROOT/release.env" GENE_RELEASE_SHA)" "$(live_sha "$FRI_ROOT/release.env" FRI_RELEASE_SHA)" \
    "$(elapsed)" "$RESTART_S" "$outcome" "$EXCEPTION${CRITICAL:+${EXCEPTION:+; }critical fix: $CRITICAL}" "${SUDO_USER:-$(id -un)}" \
    "${BATCH_TRIGGER:-maintainer}" "$G" "$F" "$NOTE" <<'PY' || echo 'WARN: the production-action log was not written' >&2
import datetime as dt, fcntl, json, os, sys
(path, now, phase, stage, before_g, before_f, after_g, after_f, duration, restart, outcome, exception, actor,
 trigger, g, f, note) = sys.argv[1:18]
try:
    from zoneinfo import ZoneInfo
    local, tz = dt.datetime.fromtimestamp(int(now), ZoneInfo('America/Los_Angeles')), 'America/Los_Angeles'
except Exception:
    local, tz = dt.datetime.now(dt.timezone.utc), 'UTC (zone unavailable)'
row = {'at': local.isoformat(), 'local_date': local.date().isoformat(), 'tz': tz, 'actor': actor, 'trigger': trigger,
       'action': 'batch-release '+phase, 'reason': 'maintainer batch release Gene '+g+' Fri '+f, 'stage': stage,
       'before': {'gene': before_g or None, 'fri': before_f or None}, 'after': {'gene': after_g or None, 'fri': after_f or None},
       'duration_s': int(duration) if duration else None, 'restart_s': int(restart) if restart else None, 'outcome': outcome,
       'exception': exception or None, 'note': note or None}
line, limit = json.dumps(row, sort_keys=True)+'\n', 1 << 20  # Past 1 MiB the log keeps its newest half.
while True:
    with open(path, 'a') as log:
        os.fchmod(log.fileno(), 0o600)
        fcntl.flock(log, fcntl.LOCK_EX)
        if os.fstat(log.fileno()).st_ino != os.stat(path).st_ino:
            continue  # Trimmed meanwhile: write to the file that is there now.
        log.write(line)
        log.flush()
        if log.tell() > limit:
            kept, size = [], 0
            for old in reversed(open(path, 'rb').read().splitlines(keepends=True)):
                if size+len(old) > limit//2:
                    break
                kept.append(old)
                size += len(old)
            with open(path+'.tmp', 'wb') as out:
                os.fchmod(out.fileno(), 0o600)
                out.writelines(reversed(kept))
                out.flush()
                os.fsync(out.fileno())
            os.replace(path+'.tmp', path)
        break
PY
}

batch_lock() {
  # Host-wide. Children inherit the descriptor, so a command a killed batch shell left running (a
  # compose up, a coordinator, a DB helper) keeps the lock until it ends, and nothing races it.
  exec 8>> "$ROOT/batch-release.lock"
  flock -n 8 || die "another batch-release command, or one a killed run left behind, still is running; find it with: fuser -v $ROOT/batch-release.lock"
}

trap 'log_action $?' EXIT
gate
[[ $PHASE == status ]] || batch_lock
if [[ -f $MANIFEST ]]; then
  OLD_G=$(jget "$MANIFEST" old_gene) && OLD_F=$(jget "$MANIFEST" old_fri) || die "$MANIFEST names no old release"
else  # Before baseline the old release is the live one; baseline checks that it is healthy and consistent.
  OLD_G=$(sed -n 's/^GENE_RELEASE_SHA=//p' "$ROOT/release.env" 2>/dev/null || true)
  OLD_F=$(sed -n 's/^FRI_RELEASE_SHA=//p' "$FRI_ROOT/release.env" 2>/dev/null || true)
fi
case $PHASE in
  baseline) baseline ;;
  stage) stage ;;
  rehearse) rehearse ;;
  drain) drain ;;
  gene) gene ;;
  fri) fri ;;
  evals) evals ;;
  verify-off) verify_off ;;
  accept-evals) accept_evals ;;
  resume) resume ;;
  cleanup) cleanup ;;
  status) state; if [[ -f $JOURNAL ]]; then cat "$JOURNAL"; fi ;;
  rollback-gene) need gene_attempted; check_manifest; rollback_gene ;;
  rollback-fri) rollback_fri ;;
  resume-old) resume_old ;;
  *) die "unknown phase $PHASE" ;;
esac
if [[ -n $NOTE ]]; then log "$NOTE"; fi  # Development left as the owner set it: the last thing said.
