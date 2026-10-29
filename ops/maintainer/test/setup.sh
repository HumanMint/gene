#!/usr/bin/env bash
# Fresh mock host: Gene OLD_G and Fri OLD_F running and healthy, the controller DB created by
# the old code as its own user (10001), as on the real host. Needs env.sh's variables.
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$H/../../.." && pwd)
R=$BATCH_ROOT FR=$BATCH_FRI_ROOT
rm -rf "$MOCK"; mkdir -p "$MOCK"; chmod 755 "$MOCK"
mkdir -p "$R"/{secrets,data/controller,data/red,data/fri,source/ops,source/src,releases/"$OLD_G"/cogs,backups,venv/bin} "$FR/backups" "$MOCK/docker"
# The live release's own manifests, so stage compares the candidate's with what is really installed (gene #116).
git -C "$REPO" archive "$OLD_G" pyproject.toml uv.lock | tar -x -C "$R/source"
git -C "$REPO" archive "$OLD_G" ops/compose.yml ops/fri-compose.yml ops/gene-worker.service ops/gene-deployer.service | tar -x -C "$R/source"
echo old > "$R/source/src/marker"
[[ $OLD_SOURCE == unmarked ]] || echo "$OLD_SOURCE" > "$R/source/.release-sha"
printf 'GENE_RELEASE_SHA=%s\nGENE_ROOT=/srv/gene\nGENE_RUNTIME_IMAGE=gene-runtime:%s\n' "$OLD_G" "$OLD_G" > "$R/release.env"
chmod 644 "$R/release.env"
printf 'SECRET_TOKEN=do-not-print\nGENE_WORKER_IMAGE=gene-worker:%s\n' "$OLD_G" > "$R/secrets/worker.env"; chmod 600 "$R/secrets/worker.env"
cat > "$R/secrets/controller.env" <<'X'
GENE_GUILD_ID=444
GENE_OWNER_ID=555
GENE_RUNTIME_TOKEN=runtime-do-not-print-000000000000000
GENE_WORKER_TOKEN=worker-do-not-print-0000000000000000
GENE_DEPLOY_TOKEN=deploy-do-not-print-0000000000000000
GENE_FRI_TOKEN=fri-do-not-print-00000000000000000000
FRI_CHANNEL_IDS=777
X
printf 'server:\n  port: 2333\nlavalink:\n  server:\n    password: do-not-print\n' > "$R/secrets/audio.yml"
echo '{"FRI_ENV_FILE": "/srv/gene/secrets/fri.env", "FRI_DATA_DIR": "/srv/gene/data/fri"}' > "$MOCK/fri-env-template.json"
printf 'FRI_RELEASE_SHA=%s\nFRI_RUNTIME_IMAGE=fri-runtime:%s\nFRI_ENV_FILE=/srv/gene/secrets/fri.env\nFRI_DATA_DIR=/srv/gene/data/fri\n' \
  "$OLD_F" "$OLD_F" > "$FR/release.env"
echo "{\"sha\": \"$OLD_F\", \"previous\": null}" > "$FR/release.json"
echo red-state > "$R/data/red/settings.json"
now=$(date +%s)
echo "{\"at\": $now, \"guild_connected\": true, \"guild_id\": 444, \"presence\": \"online\", \"sha\": \"$OLD_G\", \"controls_running\": true}" > "$R/data/red/gene-health.json"
echo "{\"at\": $now, \"sha\": \"$OLD_F\", \"guild_id\": \"444\", \"guild_connected\": true, \"presence\": \"online\", \"agent_heartbeat\": {\"running\": true, \"failed\": false}}" > "$R/data/fri/fri-health.json"
cp "$H/fri_release_stub.py" "$R/venv/bin/python"; chmod +x "$R/venv/bin/python"
chown 10001:10001 "$R/data/controller"
mapfile -t controller_env < <(grep -v '^#' "$R/secrets/controller.env")
env -i PATH=/usr/bin:/bin PYTHONPATH="$OLD_SRC/src" PYTHONDONTWRITEBYTECODE=1 GENE_DB="$R/data/controller/gene.sqlite3" \
  OLD_G="$OLD_G" OLD_F="$OLD_F" "${controller_env[@]}" \
  setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import os, sqlite3
from gene.api import create_app
create_app()
with sqlite3.connect(os.environ["GENE_DB"]) as db:
    db.execute("UPDATE settings SET value=? WHERE key=\"active_sha\"", (os.environ["OLD_G"],))
    db.execute("UPDATE settings SET value=? WHERE key=\"previous_sha\"", ("1520659"+"0"*33,))
    db.execute("UPDATE settings SET value=\"false\" WHERE key=\"paused\"")
    db.execute("INSERT INTO settings VALUES (\"fri_active_sha\", ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (os.environ["OLD_F"],))
'
python3 - "$MOCK" "$OLD_G" "$OLD_F" <<'PY'
import json, sys
M, old_g, old_f = sys.argv[1:4]
ids = {'gene-runtime:'+old_g: 'sha256:oldruntime', 'gene-worker:'+old_g: 'sha256:oldworker', 'fri-runtime:'+old_f: 'sha256:oldfri',
       # Older releases' images, which cleanup prunes, and ones it keeps: not a release tag, or still in use.
       'gene-runtime:'+'1'*40: 'sha256:staleruntime', 'gene-worker:'+'1'*40: 'sha256:staleworker',
       'fri-runtime:'+'2'*40: 'sha256:stalefri', 'fri-runtime:'+'3'*40: 'sha256:usedfri', 'gene-runtime:bootstrap': 'sha256:bootstrap'}
images = {tag: {'id': i, 'label': tag.split(':')[1], 'size': 1000} for tag, i in ids.items()}
containers = {'gene-controller-1': {'image': 'sha256:oldruntime', 'running': True, 'health': 'healthy'},
              'gene-red-1': {'image': 'sha256:oldruntime', 'running': True, 'health': 'healthy'},
              'fri-runtime-1': {'image': 'sha256:oldfri', 'running': True, 'health': 'healthy'},
              'fri-legacy-1': {'image': 'sha256:usedfri', 'running': False, 'health': 'unhealthy'}}
json.dump({'images': images, 'containers': containers}, open(M+'/docker.json', 'w'))
PY
printf 'gene-worker\ngene-deployer\n' > "$MOCK/units"
