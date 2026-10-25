#!/usr/bin/env bash
# Batched maintainer release: Gene control plane + cogs, then Fri. Run as root on the host:
#   batch-release.sh <phase> <full gene sha> <full fri sha>
# Phases in order: baseline stage rehearse drain gene fri evals verify-off resume cleanup.
# Recovery: rollback-gene (reconciles from actual state, safe to repeat), then resume-old.
# Prints no secrets: env files are passed to Docker, copied or compared, never read into output.
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

# Python inside the running controller container: UID 10001, the controller's code and /data.
ctl_py() { docker exec -i "$CONTROLLER" python - "$@"; }

settings_transition() {  # from_active from_previous to_active to_previous action; a no-op if already there
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

set_paused() {  # true|false
  ctl_py "$1" <<'PY'
import sys
from pathlib import Path
from gene.store import Store
Store(Path('/data/gene.sqlite3')).set_paused(sys.argv[1] == 'true', 'maintainer:claude')
print('development paused =', sys.argv[1])
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

state() {  # Read-only snapshot of running, pinned and recorded state; no row contents or secrets.
  local worker_pin=unknown sha
  for sha in "$G" "$OLD_G"; do
    if [[ $(grep -Fxc -- "GENE_WORKER_IMAGE=gene-worker:$sha" "$ROOT/secrets/worker.env" || true) == 1 ]]; then
      worker_pin=gene-worker:$sha
    fi
  done
  local health http=0
  health=$(docker inspect -f '{{.State.Health.Status}}' "$CONTROLLER" 2>/dev/null || echo none)
  http=$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8787/health || true)
  python3 - "$DB" "$GENE_HEALTH" "$FRI_HEALTH" "$ROOT/release.env" "$FRI_ROOT/release.env" "$FRI_ROOT/release.json" \
    "$worker_pin" "$health" "$http" "$(container_image "$CONTROLLER")" "$(container_image "$RED")" "$(container_image "$FRI")" \
    "$(container_running "$RED" && echo true || echo false)" "$(container_running "$FRI" && echo true || echo false)" <<'PY'
import json, sqlite3, sys, time
(db, gene_health, fri_health, release_env, fri_env, fri_record, worker_pin, health, http,
 controller_image, red_image, fri_image, red_running, fri_running) = sys.argv[1:15]
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
with sqlite3.connect('file:'+db+'?mode=ro', uri=True, timeout=30) as conn:
    settings = dict(conn.execute("SELECT key,value FROM settings WHERE key IN "
                                 "('active_sha','previous_sha','paused','fri_active_sha')"))
    busy = conn.execute("SELECT count(*) FROM jobs WHERE state IN ('building','deploying')").fetchone()[0]
gene, fri, genv, fenv = load(gene_health), load(fri_health), env(release_env), env(fri_env)
beat = fri.get('agent_heartbeat') or {}
now = time.time()
print(json.dumps({
    'settings': settings, 'busy_jobs': busy,
    'gene_env': {k: genv.get(k) for k in ('GENE_RELEASE_SHA', 'GENE_RUNTIME_IMAGE')}, 'worker_pin': worker_pin,
    'fri_env': {k: fenv.get(k) for k in ('FRI_RELEASE_SHA', 'FRI_RUNTIME_IMAGE')},
    'fri_record_sha': load(fri_record).get('sha'),
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

verify() {  # old|new: everything running, pinned and recorded agrees with one release
  python3 - "$(state)" "$1" "$MANIFEST" "$BASELINE" "$G" "$F" "$OLD_G" "$OLD_F" <<'PY'
import json, sys
s, which, manifest, baseline = json.loads(sys.argv[1]), sys.argv[2], json.load(open(sys.argv[3])), json.load(open(sys.argv[4]))
G, F, OLD_G, OLD_F = sys.argv[5:9]
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
                   and s['fri']['guild_id'] == baseline['fri']['guild_id'] and s['fri']['presence'] == baseline['fri']['presence']),
    'idle': s['busy_jobs'] == 0}
bad = [k for k, v in checks.items() if not v]
if bad:
    print(json.dumps(s))
    sys.exit(which+' release is not consistent: '+', '.join(bad))
print(which, 'release consistent')
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

player_snapshot() {  # The audio node's own player counts, from Lavalink's stats message.
  local config=$ROOT/secrets/audio.yml
  docker run -i --rm --network "container:$AUDIO" --user "$(stat -c %u:%g "$config")" --read-only --tmpfs /tmp \
    --cap-drop ALL -v "$config:/audio.yml:ro" "$(jget "$MANIFEST" old.runtime.tag)" python - <<'PY'
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

quiet() {  # wait_seconds: Red may restart once no player exists, or none has played for the whole window.
  local deadline=$(( $(date +%s) + $1 )) snapshot players playing ever_played=0
  while true; do
    snapshot=$(player_snapshot) || die 'could not read the audio node player state; not restarting Red'
    players=$(jget <(echo "$snapshot") players); playing=$(jget <(echo "$snapshot") playing)
    log "audio node: $players players, $playing playing"
    (( players == 0 )) && return 0
    (( playing > 0 )) && ever_played=1
    if (( $(date +%s) >= deadline )); then
      (( $1 > 0 && ever_played == 0 )) && { log 'players stayed idle for the whole window; treating them as idle connections'; return 0; }
      die 'Gene has active audio players; not restarting Red'
    fi
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
    'worker_pin': s['worker_pin'] == 'gene-worker:'+old_g,
    'controller': s['controller']['health'] == 'healthy' and s['controller']['http'] == '200' and s['controller']['image'] == runtime_id,
    'red': s['red']['running'] and s['red']['image'] == runtime_id,
    'gene_health': s['gene']['sha'] == old_g and s['gene']['age'] < 25 and s['gene']['guild_connected'] is True
                   and s['gene']['controls_running'] is True and s['gene']['presence'] is not None,
    'fri_env': s['fri_env']['FRI_RELEASE_SHA'] == old_f and fri_label == old_f, 'fri_record': s['fri_record_sha'] == old_f,
    'fri_container': s['fri_container']['running'] and s['fri_container']['image'] == fri_id,
    'fri_active_sha': s['settings'].get('fri_active_sha') in (None, old_f),
    'fri_health': s['fri']['sha'] == old_f and s['fri']['age'] < 30 and s['fri']['guild_connected'] is True
                  and s['fri']['running'] is True and not s['fri']['failed']}
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
  local db_size red_size fri_size source_size tars
  db_size=$(du_bytes "$ROOT/data/controller"); red_size=$(du_bytes "$ROOT/data/red")
  fri_size=$(du_bytes "$ROOT/data/fri"); source_size=$(du_bytes "$ROOT/source")
  tars=$(( $(stat -c %s "$STAGE/gene.tar") + $(stat -c %s "$STAGE/fri.tar") ))
  mkdir -p "$ROOT/backups" "$FRI_ROOT/backups"
  disk_admission "docker=$(docker info -f '{{.DockerRootDir}}'):$images" \
    "staging=$STAGE:$(( 2*tars + 2*db_size ))" \
    "gene_backup=$ROOT/backups:$(( db_size + red_size + source_size ))" \
    "fri_backup=$FRI_ROOT/backups:$(( fri_size + db_size ))"
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
  local old new
  old=$(jget "$MANIFEST" old.runtime.tag); new=gene-runtime:$G
  (
    test=$STAGE/dbtest
    [[ ! -e $test ]] || { echo "FAIL: $test exists; remove it first" >&2; exit 1; }
    trap 'rm -rf "$test"' EXIT  # The copies hold member data; never leave them behind.
    mkdir -m 700 "$test"
    for copy in forward downgrade; do
      python3 -c 'import sqlite3, sys
src = sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close()' "$DB" "$test/$copy.sqlite3"
    done
    chown -R 10001:10001 "$test"
    run=(docker run --rm --network none --user 10001:10001 --read-only --tmpfs /tmp
         --env-file "$ROOT/secrets/controller.env" -v "$test:/data")
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

fence() {  # Pause new work, let current work finish, then stop the worker and deployer.
  # Checked against the live setting every time, so a fence after resume pauses again.
  if [[ $(jget <(state) settings.paused) != true ]]; then
    mark intent:pause
    set_paused true
    has paused_by_release || mark paused_by_release
  fi
  local waited=0
  until [[ $(jget <(state) busy_jobs) == 0 && ! -e $ROOT/deploy-result.json ]]; do
    (( waited < WAIT_WINDOW )) || die 'work is still building or deploying after 30 minutes; development stays paused'
    log 'waiting for current work to finish'; sleep 30; waited=$(( waited + 30 ))
  done
  systemctl stop gene-worker gene-deployer
  if systemctl is-active -q gene-worker || systemctl is-active -q gene-deployer; then die 'worker or deployer still active'; fi
  [[ $(jget <(state) busy_jobs) == 0 ]] || die 'a job started while stopping the units'
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

gene() {
  need staged rehearsed drained; check_manifest
  if has gene_recorded; then verify_gene_only; return; fi
  has gene_attempted && die 'an earlier Gene attempt is in the journal; finish rollback-gene, then stage again in a fresh directory'
  fence  # Re-verified, not trusted from the drain marker.
  [[ ! -e $ROOT/releases/$G ]] || die "$ROOT/releases/$G appeared after staging"
  [[ $(tree_hash "$STAGE/cogs-ready") == "$(jget "$MANIFEST" cogs)" ]] || die 'staged cogs changed'
  disk_admission "gene_backup=$ROOT/backups:$(( $(du_bytes "$ROOT/data/controller") + $(du_bytes "$ROOT/data/red") ))"
  quiet "$WAIT_WINDOW"
  local B
  B=$ROOT/backups/maintainer-batch-$G-$(date +%s)
  mkdir -m 700 "$B"
  mset backup "$B"
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
  mark intent:cogs
  mv "$STAGE/cogs-ready" "$ROOT/releases/$G"
  quiet 0  # A fresh player snapshot right before Red stops.
  mark intent:red_stop
  compose_gene stop red
  ! container_running "$RED" || die 'Red did not stop'
  tar -C "$ROOT/data" -czf "$B/red.tar.gz" red
  chmod 600 "$B"/*.sqlite3 "$B"/*.env "$B"/*.json "$B"/*.tar.gz
  mark intent:source_swap
  mv "$ROOT/source" "$B/installed-source"
  mv "$STAGE/gene-src" "$ROOT/source"
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
print('Gene switched to', g[:7], '; Fri still', s['fri']['sha'][:7])
PY
}

old_fri_pinned() {  # The old Fri's code and config are the ones recorded, pinned and running.
  # Health is checked separately: a broken new controller can degrade an old Fri's heartbeat.
  python3 - "$(state)" "$OLD_F" "$(jget "$MANIFEST" old.fri.tag)" "$(jget "$MANIFEST" old.fri.id)" <<'PY'
import json, sys
s, old_f, tag, image = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
ok = (s['fri_record_sha'] == old_f and s['fri_env'] == {'FRI_RELEASE_SHA': old_f, 'FRI_RUNTIME_IMAGE': tag}
      and s['settings'].get('fri_active_sha') in (None, old_f) and s['fri_container']['running']
      and s['fri_container']['image'] == image)
sys.exit(0 if ok else 1)
PY
}

restore_old_fri() {  # Before any Gene downgrade, the Fri that ran against the old controller must be back.
  if old_fri_pinned; then log "Fri $OLD_F is pinned and running; its health is checked once Gene is restored"; return 0; fi
  local record
  record=$(jget "$FRI_ROOT/release.json" sha)
  [[ $record == "$F" ]] || die "Fri state is neither the old nor the new release (record $record); reconcile Fri by hand before any Gene rollback"
  log "restoring Fri $OLD_F"
  mark intent:fri_restore
  "$ROOT/venv/bin/python" -m gene.fri_release "$OLD_F"
  probe_fri "$OLD_F" "$(jget "$MANIFEST" old.fri.id)"
  old_fri_pinned || die 'Fri restore finished but its state does not match the old release'
  mark fri_restored
}

rollback_gene() {
  # Reconciles from actual state, so it is safe to repeat. errexit stays on: any failure stops here.
  trap - ERR
  SWITCHING=0
  local B prev
  B=$(backup_dir)
  prev=$(jget "$BASELINE" settings.previous_sha)
  log 'rollback: fence development'
  fence
  log 'rollback: Fri first'
  restore_old_fri
  local old_id
  old_id=$(jget "$MANIFEST" old.runtime.id)
  if has intent:start_new && ! has done:downgrade; then
    log 'rollback: stop the new controller and Red, then downgrade the database with G tools'
    compose_gene stop controller red
    if container_running "$CONTROLLER" || container_running "$RED"; then die 'controller or Red still running'; fi
    local run=(docker run --rm --network none --user 10001:10001 --read-only --tmpfs /tmp --env-file "$ROOT/secrets/controller.env"
               -v "$ROOT/data/controller:/data" "gene-runtime:$G")
    "${run[@]}" python -m gene.fri_reply_checks purge /data/gene.sqlite3
    if "${run[@]}" python -c 'import gene.profile_downgrade' 2>/dev/null; then
      "${run[@]}" python -m gene.profile_downgrade /data/gene.sqlite3
    fi
    mark done:downgrade
  fi
  log 'rollback: pins'
  cmp -s "$B/release.env" "$ROOT/release.env" || cp -p "$B/release.env" "$ROOT/release.env"
  cmp -s "$B/worker.env" "$ROOT/secrets/worker.env" || cp -p "$B/worker.env" "$ROOT/secrets/worker.env"
  cmp -s "$B/release.env" "$ROOT/release.env" && cmp -s "$B/worker.env" "$ROOT/secrets/worker.env" || die 'pins not restored'
  log 'rollback: source'
  if [[ -f $ROOT/source/.release-sha && $(cat "$ROOT/source/.release-sha") == "$G" ]]; then
    local failed=$B/failed-source
    [[ ! -e $failed ]] || failed=$B/failed-source-$(date +%s%N)
    mv "$ROOT/source" "$failed"
  fi
  if [[ ! -e $ROOT/source ]]; then
    [[ -d $B/installed-source ]] || die 'neither the new nor the old source is in place; operator recovery required'
    mv "$B/installed-source" "$ROOT/source"
  fi
  [[ -d $ROOT/source && ! -f $ROOT/source/.release-sha ]] || die 'old source not in place'
  log 'rollback: old controller and Red'
  if [[ $(container_image "$CONTROLLER") == "$old_id" && $(container_image "$RED") == "$old_id" ]] \
     && container_running "$CONTROLLER" && container_running "$RED" && probe_gene "$OLD_G" 0 "$old_id"; then
    log 'old controller and Red already running'
  else
    local started
    started=$(date +%s)
    compose_gene up -d --no-deps --force-recreate controller red
    probe_gene "$OLD_G" "$started" "$old_id"
  fi
  log 'rollback: release settings'
  settings_transition "$G" "$OLD_G" "$OLD_G" "$prev" maintainer_rollback
  verify old
  has gene_rolled_back || mark gene_rolled_back
  log 'Gene rolled back. Workers stay stopped and development stays paused; run resume-old when settled.'
}

fri() {
  need gene_recorded; check_manifest
  if has fri_switched; then probe_fri "$F" "$(jget "$MANIFEST" new.fri.id)"; return; fi
  mark intent:fri
  if ! "$ROOT/venv/bin/python" -m gene.fri_release "$F"; then
    echo 'FAIL: Fri release failed. State for reconciliation:' >&2
    state
    local latest
    latest=$(find "$FRI_ROOT/backups" -maxdepth 1 -name "*-$F" -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)
    [[ -n $latest && -f $latest/failure.json ]] && cat "$latest/failure.json"
    die 'workers stay stopped until Fri is reconciled (rollback-gene restores old Fri first)'
  fi
  probe_fri "$F" "$(jget "$MANIFEST" new.fri.id)"
  mark fri_switched
}

evals() {
  need fri_switched; check_manifest
  local suite
  for suite in requests sass controls; do
    log "eval suite $suite"
    if docker exec "$FRI" python -m fri.fri_eval --suite "$suite" > "$STAGE/eval-$suite.jsonl" 2>&1; then
      echo "suite $suite passed"
    else
      echo "suite $suite FAILED"
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

resume_services() {  # gene_sha
  systemctl start gene-worker gene-deployer
  systemctl is-active -q gene-worker || die 'gene-worker did not start'
  systemctl is-active -q gene-deployer || die 'gene-deployer did not start'
  if has paused_by_release; then set_paused false; fi
  audit maintainer_services_resumed "$1"
}

resume() {
  need gene_recorded fri_switched; check_manifest
  has gene_rolled_back && die 'Gene was rolled back; use resume-old'
  has resumed && { log 'already resumed'; return; }
  verify new
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
  need gene_rolled_back; check_manifest
  verify old
  resume_services "$OLD_G"
  mark resumed_old
}

cleanup() {
  has resumed || has resumed_old || die 'resume first'
  rm -rf "$STAGE/fri-src" "$STAGE/dbtest" "$STAGE"/*.tar "$STAGE"/*.tar.sha256
  log "kept: journal, manifest, baseline and evals in $STAGE; backup $(backup_dir 2>/dev/null || echo none); all images"
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
  rollback-gene) need gene_attempted; check_manifest; rollback_gene ;;
  resume-old) resume_old ;;
  *) die "unknown phase $PHASE" ;;
esac
