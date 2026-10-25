#!/usr/bin/env python3
import json, os, sqlite3, sys, time
M, ROOT, FRI = os.environ['MOCK'], os.environ['BATCH_ROOT'], os.environ['BATCH_FRI_ROOT']
assert sys.argv[1:3] == ['-m', 'gene.fri_release'], sys.argv
sha = sys.argv[3]
if os.environ.get('MOCK_FRI_FAIL') == sha:
    sys.exit('mock fri_release failure')
st = json.load(open(M+'/docker.json'))
tag = 'fri-runtime:'+sha
st['containers']['fri-runtime-1'] = {'image': st['images'][tag]['id'], 'running': True, 'health': 'healthy'}
json.dump(st, open(M+'/docker.json', 'w'))
env = json.load(open(M+'/fri-env-template.json'))
open(FRI+'/release.env', 'w').write(''.join(f'{k}={v}\n' for k, v in {**env, 'FRI_RELEASE_SHA': sha, 'FRI_RUNTIME_IMAGE': tag}.items()))
before = json.load(open(FRI+'/release.json'))
json.dump({'sha': sha, 'previous': before['sha']}, open(FRI+'/release.json', 'w'))
h = json.load(open(ROOT+'/data/fri/fri-health.json'))
h.update(sha=sha, at=time.time())
json.dump(h, open(ROOT+'/data/fri/fri-health.json', 'w'))
with sqlite3.connect(ROOT+'/data/controller/gene.sqlite3') as db:
    db.execute("INSERT INTO settings VALUES ('fri_active_sha', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sha,))
print(json.dumps({'sha': sha, 'success': True}))
