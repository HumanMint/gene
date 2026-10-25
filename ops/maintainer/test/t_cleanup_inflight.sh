# shellcheck shell=bash
# A finished release's cleanup, repeated while another pair is staged and drained, keeps every image
# the unfinished pair's manifest names, so its next phases still pass their image checks.
. "$(dirname "$0")/common.sh"
release
expect new false up
# Another pair B is mid-release: staged and drained, with its own new images and an idle worker.
B=$(printf 'd%.0s' {1..40})
other=$BATCH_ROOT/staging/batch-$B-$F
mkdir -p "$other"
python3 - "$other/manifest.json" "$MOCK/docker.json" "$G" "$F" "$B" <<'PY'
import json, sys
path, docker, g, f, b = sys.argv[1:6]
d = json.load(open(docker))
for repo in ('gene-runtime', 'gene-worker'):
    d['images'][repo+':'+b] = {'id': 'sha256:inflight'+repo, 'label': b, 'size': 1000}
json.dump(d, open(docker, 'w'))
ids = {tag: i['id'] for tag, i in d['images'].items()}
pair = lambda gene, fri: {'runtime': {'tag': 'gene-runtime:'+gene, 'id': ids['gene-runtime:'+gene]},
                          'worker': {'tag': 'gene-worker:'+gene, 'id': ids['gene-worker:'+gene]},
                          'fri': {'tag': 'fri-runtime:'+fri, 'id': ids['fri-runtime:'+fri]}}
json.dump({'gene': b, 'fri': f, 'old_gene': g, 'old_fri': f, 'old': pair(g, f), 'new': pair(b, f)}, open(path, 'w'))
PY
printf 'baselined\nstaged\nrehearsed\nintent:pause\ndrained\n' > "$other/journal"
touch -d '10 days ago' "$other/journal"
# Three newer finished releases push B out of the most-recent window.
for n in 5 6 7; do
  done_dir=$BATCH_ROOT/staging/batch-$(printf "$n%.0s" {1..40})-$F
  mkdir -p "$done_dir"; cp "$ST/manifest.json" "$done_dir/manifest.json"; printf 'resumed\n' > "$done_dir/journal"
done
run 0 cleanup 4
images=$(python3 -c 'import json, sys; print(" ".join(sorted(json.load(open(sys.argv[1]))["images"])))' "$MOCK/docker.json")
for tag in "gene-runtime:$B" "gene-worker:$B" "gene-runtime:$G" "gene-worker:$G" "fri-runtime:$F"; do
  [[ " $images " == *" $tag "* ]] || fail "cleanup removed $tag, which an unfinished release needs"
done
finish
