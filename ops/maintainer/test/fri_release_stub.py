#!/usr/bin/env python3
"""Stands in for /srv/gene/venv/bin/python -m gene.fri_release in the mock host.

Follows gene.fri_release's order of writes: it runs as root, opens the controller DB as root,
stops and starts the runtime through docker compose, and on a failed health check restores
the previous pins itself. MOCK_FRI_FAIL=<sha> fails the health check; MOCK_FRI_CRASH=<sha>
dies right after starting the new runtime, before the record and the DB are written.
"""
import json, os, sqlite3, subprocess, sys, time

ROOT, FRI = os.environ['BATCH_ROOT'], os.environ['BATCH_FRI_ROOT']
assert sys.argv[1:3] == ['-m', 'gene.fri_release'], sys.argv
sha = sys.argv[3]
template = json.load(open(os.environ['MOCK']+'/fri-env-template.json'))
DB = ROOT+'/data/controller/gene.sqlite3'


def environment(release):
    values = {'FRI_RELEASE_SHA': release, 'FRI_RUNTIME_IMAGE': 'fri-runtime:'+release} | template
    return ''.join(k+'='+v+'\n' for k, v in values.items()).encode()


def compose(*args):
    subprocess.run(['docker', 'compose', '--project-name', 'fri', '--env-file', FRI+'/release.env',
                    '-f', ROOT+'/source/ops/fri-compose.yml', *args], check=True)


def write(path, data):
    with open(path+'.tmp', 'wb') as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(data)
    os.replace(path+'.tmp', path)


def audit(outcome, active):
    with sqlite3.connect(DB, timeout=30) as db:
        db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
                   (time.time(), 'maintainer:codex', 'fri_release', sha, outcome))
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('fri_active_sha', active))


label = subprocess.run(['docker', 'image', 'inspect', '-f', '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                        'fri-runtime:'+sha], capture_output=True, text=True, check=True).stdout.strip()
assert label == sha, 'label'
before = json.load(open(FRI+'/release.json'))
assert before['sha'] != sha, 'This Fri release is already active'
old_env = open(FRI+'/release.env', 'rb').read()
if old_env != environment(before['sha']):
    sys.exit('Inconsistent Fri release pins; reconcile before continuing')
with sqlite3.connect('file:'+DB+'?mode=ro', uri=True) as db:
    active = db.execute("SELECT value FROM settings WHERE key='fri_active_sha'").fetchone()
if (active[0] if active else None) != before['sha']:
    sys.exit('Inconsistent controller Fri release; reconcile before continuing')
backup = f'{FRI}/backups/{time.time_ns()}-{sha}'
os.makedirs(backup, mode=0o700)
compose('stop', 'runtime')
write(FRI+'/release.env', environment(sha))
compose('up', '-d', '--no-deps', '--force-recreate', 'runtime')
if os.environ.get('MOCK_FRI_CRASH') == sha:
    os._exit(137)
if os.environ.get('MOCK_FRI_FAIL') == sha:
    compose('stop', 'runtime')
    write(FRI+'/release.env', old_env)
    compose('up', '-d', '--no-deps', '--force-recreate', 'runtime')
    audit('failed; current state preserved; recovered=True', before['sha'])
    json.dump({'recovered': True, 'state_restored': False}, open(backup+'/failure.json', 'w'))
    sys.exit('New Fri failed its health checks')
audit('deployed', sha)
json.dump({'sha': sha, 'previous': before['sha'], 'legacy_sha': None, 'backup': backup, 'at': time.time()},
          open(FRI+'/release.json', 'w'))
print(json.dumps({'sha': sha, 'backup': backup, 'success': True}))
