# shellcheck shell=bash
# cleanup keeps every image a recent release's rollback needs, and every image the live release pins:
# the release before this one (whose rollback restores the 1111/2222 images), and a newer release that went
# live after this one, whose worker has no container to protect its image. Repeating cleanup is safe.
. "$(dirname "$0")/common.sh"
release
expect new false up
# The release before this one: it installed OLD over the 1111/2222 release, and resumed.
earlier=$BATCH_ROOT/staging/batch-$OLD_G-$OLD_F
mkdir -p "$earlier"
python3 - "$earlier/manifest.json" "$MOCK/docker.json" "$OLD_G" "$OLD_F" <<'PY'
import json, sys
path, docker, old_g, old_f = sys.argv[1:5]
ids = {tag: i['id'] for tag, i in json.load(open(docker))['images'].items()}
pair = lambda g, f: {'runtime': {'tag': 'gene-runtime:'+g, 'id': ids['gene-runtime:'+g]},
                     'worker': {'tag': 'gene-worker:'+g, 'id': ids['gene-worker:'+g]},
                     'fri': {'tag': 'fri-runtime:'+f, 'id': ids['fri-runtime:'+f]}}
json.dump({'gene': old_g, 'fri': old_f, 'old_gene': '1'*40, 'old_fri': '2'*40,
           'old': pair('1'*40, '2'*40), 'new': pair(old_g, old_f)}, open(path, 'w'))
PY
printf 'baselined\nresumed\n' > "$earlier/journal"
touch -d '1 day ago' "$earlier/journal"
run 0 cleanup 4
images=$(python3 -c 'import json, sys; print(" ".join(sorted(json.load(open(sys.argv[1]))["images"])))' "$MOCK/docker.json")
for tag in "gene-runtime:$G" "gene-worker:$G" "fri-runtime:$F" "gene-runtime:$OLD_G" "gene-worker:$OLD_G" "fri-runtime:$OLD_F" \
           "gene-runtime:$(printf '1%.0s' {1..40})" "gene-worker:$(printf '1%.0s' {1..40})" "fri-runtime:$(printf '2%.0s' {1..40})"; do
  [[ " $images " == *" $tag "* ]] || fail "cleanup removed $tag, which a recent release's rollback needs"
done
# A newer release went live after this one; its worker is idle, so no container holds its image.
N=$(printf '4%.0s' {1..40})
python3 - "$MOCK/docker.json" "$N" <<'PY'
import json, sys
path, n = sys.argv[1:3]
d = json.load(open(path))
for repo in ('gene-runtime', 'gene-worker', 'fri-runtime'):
    d['images'][repo+':'+n] = {'id': 'sha256:newer'+repo, 'label': n, 'size': 1000}
json.dump(d, open(path, 'w'))
PY
sed -i "s|^GENE_RUNTIME_IMAGE=.*|GENE_RUNTIME_IMAGE=gene-runtime:$N|" "$BATCH_ROOT/release.env"
sed -i "s|^GENE_WORKER_IMAGE=.*|GENE_WORKER_IMAGE=gene-worker:$N|" "$BATCH_ROOT/secrets/worker.env"
sed -i "s|^FRI_RUNTIME_IMAGE=.*|FRI_RUNTIME_IMAGE=fri-runtime:$N|" "$BATCH_FRI_ROOT/release.env"
run 0 cleanup 4
images=$(python3 -c 'import json, sys; print(" ".join(sorted(json.load(open(sys.argv[1]))["images"])))' "$MOCK/docker.json")
for tag in "gene-runtime:$N" "gene-worker:$N" "fri-runtime:$N"; do
  [[ " $images " == *" $tag "* ]] || fail "a repeated cleanup removed $tag, which the live release pins"
done
finish
