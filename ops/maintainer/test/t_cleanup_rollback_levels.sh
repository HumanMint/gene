# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
release
first=$ST/manifest.json
# Seed the later resumed Fri-only and Gene-only batches without requiring Fri git history.
# The first paired release ran all phases; cleanup reads these manifests, journals and pins.
python3 - "$first" "$MOCK/docker.json" "$BATCH_ROOT" "$BATCH_FRI_ROOT" <<'PY'
import copy, json, os, pathlib, sys
first, docker, root, fri_root = sys.argv[1:]
root, fri_root = pathlib.Path(root), pathlib.Path(fri_root)
manifest, state = (json.load(open(path)) for path in (first, docker))
os.utime(pathlib.Path(first).with_name('journal'), (1, 1))
previous = manifest['new']
gene, fri = manifest['gene'], manifest['fri']
for at, new_gene, new_fri in [(2, gene, 'e' * 40), (3, 'd' * 40, 'e' * 40)]:
    new = copy.deepcopy(previous)
    for role, repo, sha in [('runtime', 'gene-runtime', new_gene), ('worker', 'gene-worker', new_gene),
                            ('fri', 'fri-runtime', new_fri)]:
        tag = repo + ':' + sha
        if tag not in state['images']:
            state['images'][tag] = {'id': 'sha256:cleanup-level-' + role + sha, 'label': sha, 'size': 1000}
        new[role] = {'tag': tag, 'id': state['images'][tag]['id']}
    stage = root / 'staging' / f'batch-{new_gene}-{new_fri}'
    stage.mkdir()
    (stage / 'manifest.json').write_text(json.dumps({'gene': new_gene, 'fri': new_fri,
        'old_gene': gene, 'old_fri': fri, 'old': previous, 'new': new}))
    (stage / 'journal').write_text('resumed\n')
    os.utime(stage / 'journal', (at, at))
    gene, fri, previous = new_gene, new_fri, new
json.dump(state, open(docker, 'w'))
(root / 'release.env').write_text(f'GENE_RELEASE_SHA={gene}\nGENE_RUNTIME_IMAGE={previous["runtime"]["tag"]}\n')
(root / 'secrets/worker.env').write_text(f'GENE_WORKER_IMAGE={previous["worker"]["tag"]}\n')
(fri_root / 'release.env').write_text(f'FRI_RELEASE_SHA={fri}\nFRI_RUNTIME_IMAGE={previous["fri"]["tag"]}\n')
PY
G=$(printf 'd%.0s' {1..40}) F=$(printf 'e%.0s' {1..40})
ST=$BATCH_ROOT/staging/batch-$G-$F; JOURNAL=$ST/journal
run 0 cleanup 3
python3 - "$first" "$MOCK/docker.json" <<'PY' || fail 'cleanup removed an image from the second Gene rollback level'
import json, sys
manifest, docker = (json.load(open(path)) for path in sys.argv[1:])
for side in ('old', 'new'):
    for image in manifest[side].values():
        assert docker['images'].get(image['tag'], {}).get('id') == image['id'], image['tag']
PY
finish
