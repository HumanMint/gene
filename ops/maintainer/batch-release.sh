#!/usr/bin/env bash
# Batched maintainer release: Gene control plane + cogs, then Fri. Run as root on the host:
#   batch-release.sh <phase> <full gene sha> <full fri sha>
# Phases in order: baseline stage rehearse drain gene fri evals verify-off resume cleanup.
# Recovery: rollback-gene (reconciles from actual state, safe to repeat), then resume-old.
# Prints no secrets: env files are passed to Docker, copied or compared, never read into output.
# Every controller DB access runs as the DB owner in a throwaway container of the old runtime
# image, so no root SQLite client leaves journal files the controller cannot open, and none
# of it needs a working controller.
set -Eeuo pipefail
umask 077

PHASE=${1:?phase}; G=${2:?gene sha}; F=${3:?fri sha}
[[ $G =~ ^[0-9a-f]{40}$ && $F =~ ^[0-9a-f]{40}$ ]] || { echo 'Full 40-character SHAs required' >&2; exit 2; }
OLD_G=${OLD_G:-f66700140d91dc80df0bca8ae29ca671ca67f80e}
OLD_F=${OLD_F:-6a1d410cbb7f04f36cd5be629cc48b05a221d653}
ROOT=${BATCH_ROOT:-/srv/gene}
FRI_ROOT=${BATCH_FRI_ROOT:-/srv/fri}
DB=$ROOT/data/controller/gene.sqlite3
GENE_HEALTH=$ROOT/data/red/gene-health.json
FRI_HEALTH=$ROOT/data/fri/fri-health.json
STAGE=$ROOT/staging/batch-$G
JOURNAL=$STAGE/journal
MANIFEST=$STAGE/manifest.json
BASELINE=$STAGE/baseline.json
CONTROLLER=gene-controller-1
RED=gene-red-1
FRI=fri-runtime-1
AUDIO=gene-audio-node-1
PROBE_WINDOW=${PROBE_WINDOW:-180}  # seconds; shorter only in offline tests of this script
WAIT_WINDOW=${WAIT_WINDOW:-1800}
SWITCHING=0

log() { echo "== $*"; }
die() {
  echo "FAIL: $*" >&2
  # exit skips the ERR trap, so a refusal mid-switch must start the rollback itself.
  if [[ $SWITCHING == 1 ]]; then on_gene_error; fi
  exit 1
}
mark() { echo "$1" >> "$JOURNAL"; sync "$JOURNAL"; }
has() { [[ -f $JOURNAL ]] && grep -qx -- "$1" "$JOURNAL"; }
need() { local step; for step in "$@"; do has "$step" || die "phase needs '$step' first"; done; }
owns_pause() {  # This release paused development and has not unpaused it since.
  [[ -f $JOURNAL && $({ grep -x -e intent:pause -e done:unpause "$JOURNAL" || true; } | tail -n 1) == intent:pause ]]
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

repair_sidecars() {
  # gene.fri_release opens the DB as root. SQLite normally hands new journal files to the DB owner,
  # but a build without that would leave root-owned ones the owner-run helpers can't open, so every
  # DB access first gives them back. The read-only phases leave them alone and fail visibly instead.
  [[ $PHASE == baseline || $PHASE == status ]] && return 0
  local owner file
  owner=$(db_owner)
  for file in "$DB-wal" "$DB-shm" "$DB-journal"; do
    if [[ -e $file && $(stat -c %u:%g "$file") != "$owner" ]]; then
      log "giving ${file##*/} back to the database owner" >&2
      chown "$owner" "$file"
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

set_paused() {  # true|false: the writes Store.set_paused makes, without needing the controller
  db_py "$1" <<'PY'
import sqlite3, sys, time
paused = sys.argv[1] == 'true'
db = sqlite3.connect('/data/gene.sqlite3', timeout=30, isolation_level=None)
db.execute('BEGIN IMMEDIATE')
if db.execute("UPDATE settings SET value=? WHERE key='paused'", ('true' if paused else 'false',)).rowcount != 1:
    raise SystemExit('the controller has no paused setting')
db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
           (time.time(), 'maintainer:claude', 'pause' if paused else 'resume', '', ''))
db.execute('COMMIT')
print('development paused =', sys.argv[1])
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

state() {  # Read-only snapshot of running, pinned and recorded state; no row contents or secrets.
  local worker_pin=unknown sha health http=0 src_state db
  for sha in "$G" "$OLD_G"; do
    if [[ $(grep -Fxc -- "GENE_WORKER_IMAGE=gene-worker:$sha" "$ROOT/secrets/worker.env" || true) == 1 ]]; then
      worker_pin=gene-worker:$sha
    fi
  done
  health=$(docker inspect -f '{{.State.Health.Status}}' "$CONTROLLER" 2>/dev/null || echo none)
  http=$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8787/health || true)
  if [[ -f $ROOT/source/.release-sha ]]; then src_state=$(cat "$ROOT/source/.release-sha")
  elif [[ -d $ROOT/source ]]; then src_state=unmarked
  else src_state=missing; fi
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
expect_settings = ({'active_sha': G, 'previous_sha': OLD_G} if which == 'new'
                   else {'active_sha': OLD_G, 'previous_sha': baseline['settings']['previous_sha']})
checks = {
    'controller_healthy': s['controller']['health'] == 'healthy' and s['controller']['http'] == '200',
    'controller_image': s['controller']['image'] == images['runtime']['id'],
    'red_image': s['red']['running'] and s['red']['image'] == images['runtime']['id'],
    'fri_image': s['fri_container']['running'] and s['fri_container']['image'] == images['fri']['id'],
    'gene_env': s['gene_env'] == {'GENE_RELEASE_SHA': gene_sha, 'GENE_RUNTIME_IMAGE': images['runtime']['tag']},
    'worker_pin': s['worker_pin'] == images['worker']['tag'],
    'source': s['source'] == (G if which == 'new' else 'unmarked'),
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
    'source': (s['source'], os.path.isdir(B+'/installed-source')) in (('unmarked', False), (G, True), ('missing', True)),
}
bad = [k for k, v in checks.items() if not v]
if bad:
    sys.exit('live state is outside the recorded old/new pair ('+', '.join(bad)+'); reconcile by hand, nothing was changed')
print('live state is built from the recorded old and new releases')
PY
}

probe_gene() {  # sha started_epoch image_id: 6 consecutive good 5 s samples within 180 s
  local deadline=$(( $(date +%s) + PROBE_WINDOW )) good=0 snapshot reason=''
  while (( $(date +%s) < deadline )); do
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
  local deadline=$(( $(date +%s) + PROBE_WINDOW )) good=0 reason=''
  while (( $(date +%s) < deadline )); do
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
  local deadline=$(( $(date +%s) + $1 )) snapshot players playing
  while true; do
    snapshot=$(player_snapshot) || die 'could not read the audio node player state; not restarting Red'
    players=$(jget <(echo "$snapshot") players); playing=$(jget <(echo "$snapshot") playing)
    log "audio node: $players players, $playing playing"
    (( players == 0 )) && return 0
    (( $(date +%s) < deadline )) || die "the audio node still has $players players ($playing playing); not restarting Red. If Gene is sitting idle in voice, ask the owner before disconnecting it"
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
du_bytes() { du -sb "$1" | cut -f1; }

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
import os, sys, uuid
root, stage, *hops = sys.argv[1:]
if os.path.ismount(os.path.join(root, 'source')):
    sys.exit(root+'/source is a mount point, so the switch cannot rename it')
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
  local owner file
  owner=$(db_owner)
  for file in "$DB-wal" "$DB-shm" "$DB-journal"; do
    [[ ! -e $file || $(stat -c %u:%g "$file") == "$owner" ]] || die "${file##*/} is not owned by the database owner"
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
    'worker_pin': s['worker_pin'] == 'gene-worker:'+old_g, 'source': s['source'] == 'unmarked',
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
    python3 - "$MANIFEST" "$G" "$F" "$runtime_tag" "$(image_id "$runtime_tag")" "gene-worker:$OLD_G" \
      "$(image_id "gene-worker:$OLD_G")" "$fri_tag" "$(image_id "$fri_tag")" <<'PY'
import json, os, sys
path, g, f, rt, rid, wt, wid, ft, fid = sys.argv[1:10]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump({'gene': g, 'fri': f, 'old': {'runtime': {'tag': rt, 'id': rid}, 'worker': {'tag': wt, 'id': wid},
                                           'fri': {'tag': ft, 'id': fid}}}, out, indent=1)
PY
    mark baseline
  else
    check_manifest
    log 'baseline already recorded; live state still healthy at the old release'
  fi
  echo "$snapshot"
}

stage() {
  need baseline; check_manifest
  has staged && { log 'already staged'; return; }
  local name file
  for name in gene fri; do
    [[ -f $STAGE/$name.tar && -f $STAGE/$name.tar.sha256 ]] || die "upload $STAGE/$name.tar and $name.tar.sha256 first"
    (cd "$STAGE" && sha256sum -c "$name.tar.sha256")
    mset "archives.$name" "$(cut -d' ' -f1 "$STAGE/$name.tar.sha256")"
  done
  [[ ! -e $ROOT/releases/$G ]] || die "$ROOT/releases/$G already exists; inspect it before staging"
  local images=0 tag
  for tag in "$(jget "$MANIFEST" old.runtime.tag)" "$(jget "$MANIFEST" old.worker.tag)" "$(jget "$MANIFEST" old.fri.tag)"; do
    images=$(( images + $(docker image inspect -f '{{.Size}}' "$tag") ))
  done
  local db_size red_size fri_size source_size gene_tar tars
  db_size=$(du_bytes "$ROOT/data/controller"); red_size=$(du_bytes "$ROOT/data/red")
  fri_size=$(du_bytes "$ROOT/data/fri"); source_size=$(du_bytes "$ROOT/source")
  gene_tar=$(stat -c %s "$STAGE/gene.tar")
  tars=$(( gene_tar + $(stat -c %s "$STAGE/fri.tar") ))
  mkdir -p "$ROOT/backups" "$ROOT/releases" "$FRI_ROOT/backups"
  rename_preflight "$ROOT/backups"
  disk_admission "docker=$(docker info -f '{{.DockerRootDir}}'):$images" \
    "staging=$STAGE:$(( 2*tars + 2*db_size ))" \
    "gene_backup=$ROOT/backups:$(( db_size + red_size + source_size ))" \
    "fri_backup=$FRI_ROOT/backups:$(( fri_size + db_size ))" \
    "releases=$ROOT/releases:$gene_tar" "source=$ROOT:$gene_tar"
  for name in gene fri; do
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
    if ! cmp -s "$ROOT/source/$file" "$STAGE/gene-src/$file"; then
      diff -u "$ROOT/source/$file" "$STAGE/gene-src/$file" || true
      [[ ${ALLOW_OPS_DIFF:-} == 1 ]] || die "$file changed; review the diff, then rerun with ALLOW_OPS_DIFF=1"
    fi
  done
  # The cog release, prepared now so downtime only renames it into place.
  [[ ! -e $STAGE/cogs-ready ]] || die "$STAGE/cogs-ready exists from an earlier attempt"
  mkdir -m 755 "$STAGE/cogs-ready"
  cp -r "$STAGE/gene-src/cogs" "$STAGE/cogs-ready/cogs"
  chown -R 0:0 "$STAGE/cogs-ready"
  find "$STAGE/cogs-ready" -type d -exec chmod 755 {} +
  find "$STAGE/cogs-ready" -type f -exec chmod 444 {} +
  mset cogs "$(tree_hash "$STAGE/cogs-ready")"
  log 'building images'
  docker build -q -f "$STAGE/gene-src/ops/Dockerfile" --target runtime \
    --label org.opencontainers.image.revision="$G" -t "gene-runtime:$G" "$STAGE/gene-src"
  docker build -q -f "$STAGE/gene-src/ops/Dockerfile" --target worker \
    --label org.opencontainers.image.revision="$G" -t "gene-worker:$G" "$STAGE/gene-src"
  docker build -q -f "$STAGE/fri-src/ops/Dockerfile" --build-arg FRI_RELEASE_SHA="$F" \
    -t "fri-runtime:$F" "$STAGE/fri-src"
  local image label
  for image in "runtime:gene-runtime:$G" "worker:gene-worker:$G" "fri:fri-runtime:$F"; do
    tag=${image#*:}
    label=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$tag")
    [[ $label == "${tag#*:}" ]] || die "$tag label is $label"
    mset "new.${image%%:*}.tag" "$tag"; mset "new.${image%%:*}.id" "$(image_id "$tag")"
  done
  log 'isolated import checks'
  local isolated=(docker run --rm --network none --user 10001:10001 --read-only --tmpfs /tmp --cap-drop ALL
                  --security-opt no-new-privileges:true)
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
  "${isolated[@]}" "fri-runtime:$F" python -c 'import json, sys
from fri.fri_protocol import compatible
assert compatible(json.loads(sys.argv[1])), "Fri rejects the candidate controller contract"
print("fri accepts the contract")' "$contract"
  log 'audio player snapshot works'
  player_snapshot
  mark staged
}

rehearse() {  # Migrate copies of the live DB with G, then start the exact rollback image on them.
  need staged; check_manifest
  has rehearsed && { log 'already rehearsed'; return; }
  local old new owner
  old=$(jget "$MANIFEST" old.runtime.id); new=$(jget "$MANIFEST" new.runtime.id); owner=$(db_owner)
  (
    test=$STAGE/dbtest
    [[ ! -e $test ]] || { echo "FAIL: $test exists; remove it first" >&2; exit 1; }
    trap 'rm -rf "$test"' EXIT  # The copies hold member data; never leave them behind.
    install -d -m 700 -o "${owner%:*}" -g "${owner#*:}" "$test"
    for copy in forward downgrade; do db_copy "$test" "$copy.sqlite3"; done
    run=(docker run --rm --network none --user "$owner" --read-only --tmpfs /tmp --cap-drop ALL
         --security-opt no-new-privileges:true --env-file "$ROOT/secrets/controller.env" -v "$test:/data")
    start='import sys
from fastapi.testclient import TestClient
from gene.api import create_app
client = TestClient(create_app())
assert client.get("/health").status_code == 200
print(sys.argv[1], "controller starts; status keys:", sorted(client.app.state.store.status()))'
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
    "${run[@]}" -e GENE_DB=/data/downgrade.sqlite3 "$new" python -c "$start" new
    "${run[@]}" "$new" python -m gene.fri_reply_checks purge /data/downgrade.sqlite3
    if "${run[@]}" "$new" python -c 'import gene.profile_downgrade' 2>/dev/null; then
      "${run[@]}" "$new" python -m gene.profile_downgrade /data/downgrade.sqlite3
    else
      echo 'no gene.profile_downgrade in G: the old release already has every profile migration'
    fi
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
  # line is the ownership record: resume unpauses only while the latest pause in the journal is ours.
  local paused busy waited=0
  paused=$(state_value settings.paused)
  [[ $paused == true || $paused == false ]] || die "unexpected paused setting: $paused"
  if [[ $paused == false ]]; then
    mark intent:pause
    set_paused true
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
  has drained || mark drained
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
  mark gene_attempted
  SWITCHING=1
  trap on_gene_error ERR
  gene_disk
  mark intent:cogs
  rename_tree "$STAGE/cogs-ready" "$ROOT/releases/$G"
  quiet 0  # A fresh player snapshot right before Red stops.
  mark intent:red_stop
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
  started=$(date +%s)
  mark intent:start_new
  repair_sidecars
  compose_gene up -d --no-deps --force-recreate controller red
  probe_gene "$G" "$started" "$(jget "$MANIFEST" new.runtime.id)"
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
    "${run[@]}" python -m gene.fri_reply_checks purge /data/gene.sqlite3
    if "${run[@]}" python -c 'import gene.profile_downgrade' 2>/dev/null; then
      "${run[@]}" python -m gene.profile_downgrade /data/gene.sqlite3
    fi
    mark done:downgrade
  fi
  log 'rollback: pins'
  restore_file "$B/release.env" "$ROOT/release.env"
  restore_file "$B/worker.env" "$ROOT/secrets/worker.env"
  log 'rollback: source'
  if [[ -f $ROOT/source/.release-sha && $(cat "$ROOT/source/.release-sha") == "$G" ]]; then
    local failed=$B/failed-source
    [[ ! -e $failed ]] || failed=$B/failed-source-$(date +%s%N)
    rename_tree "$ROOT/source" "$failed"
  fi
  if [[ ! -e $ROOT/source ]]; then
    [[ -d $B/installed-source ]] || die 'neither the new nor the old source is in place; operator recovery required'
    rename_tree "$B/installed-source" "$ROOT/source"
  fi
  [[ -d $ROOT/source && ! -f $ROOT/source/.release-sha ]] || die 'old source not in place'
  log 'rollback: old controller and Red'
  if [[ $(container_image "$CONTROLLER") == "$old_id" && $(container_image "$RED") == "$old_id" ]] \
     && container_running "$CONTROLLER" && container_running "$RED" && probe_gene "$OLD_G" 0 "$old_id"; then
    log 'old controller and Red already running'
  else
    local started
    started=$(date +%s)
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
  has gene_rolled_back && die 'Gene was rolled back'
  if has fri_switched; then probe_fri "$F" "$(jget "$MANIFEST" new.fri.id)"; return; fi
  mark intent:fri
  if ! "$ROOT/venv/bin/python" -m gene.fri_release "$F"; then
    repair_sidecars
    echo 'FAIL: Fri release failed. State for reconciliation:' >&2
    state
    local latest
    latest=$(find "$FRI_ROOT/backups" -maxdepth 1 -name "*-$F" -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)
    if [[ -n $latest && -f $latest/failure.json ]]; then cat "$latest/failure.json"; fi
    die 'workers stay stopped; rollback-gene restores the old Fri first, from any partial state'
  fi
  repair_sidecars
  probe_fri "$F" "$(jget "$MANIFEST" new.fri.id)"
  mark fri_switched
}

evals() {
  need fri_switched; check_manifest
  local suite failed=()
  for suite in requests sass controls; do
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
  (( ${#failed[@]} == 0 )) || die "eval suites failed: ${failed[*]}; decide with the owner whether to roll back"
}

verify_off() {
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
  systemctl start gene-worker gene-deployer
  systemctl is-active -q gene-worker || die 'gene-worker did not start'
  systemctl is-active -q gene-deployer || die 'gene-deployer did not start'
  if owns_pause; then
    set_paused false
    mark done:unpause
  fi
  audit maintainer_services_resumed "$1"
}

write_result() {  # The deployer's receipt for this release; a repeat accepts the same receipt.
  python3 - "$ROOT/maintainer-$G-result.json" "$G" "$OLD_G" "$F" "$OLD_F" "$(backup_dir)" <<'PY'
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

resume() {
  need gene_recorded fri_switched; check_manifest
  has gene_rolled_back && die 'Gene was rolled back; use resume-old'
  has resumed && { log 'already resumed'; return; }
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
  need drained
  if ! has intent:services_old; then
    verify old
    mark intent:services_old
  else
    verify old any-work
  fi
  resume_services "$OLD_G"
  has resumed_old || mark resumed_old
}

cleanup() {
  has resumed || has resumed_old || die 'resume first'
  rm -rf "$STAGE/fri-src" "$STAGE/gene-src" "$STAGE/cogs-ready" "$STAGE/dbtest" "$STAGE"/*.tar "$STAGE"/*.tar.sha256
  log "kept: journal, manifest, baseline and evals in $STAGE; backup $(backup_dir 2>/dev/null || echo none); all images"
}

batch_lock() {
  # Host-wide. Children inherit the descriptor, so a command a killed batch shell left running (a
  # compose up, a coordinator, a DB helper) keeps the lock until it ends, and nothing races it.
  exec 8>> "$ROOT/batch-release.lock"
  flock -n 8 || die "another batch-release command, or one a killed run left behind, still is running; find it with: fuser -v $ROOT/batch-release.lock"
}

[[ $PHASE == status ]] || batch_lock
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
  status) state; if [[ -f $JOURNAL ]]; then cat "$JOURNAL"; fi ;;
  rollback-gene) need gene_attempted; check_manifest; rollback_gene ;;
  resume-old) resume_old ;;
  *) die "unknown phase $PHASE" ;;
esac
