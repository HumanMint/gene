. "$(dirname "$0")/common.sh"
run baseline 2 && upload && run stage 4 && run rehearse 2 && run drain 2 && run gene 4 && run fri 3 && run evals 3 && run verify-off 1 && run resume 2 && run cleanup 1
echo "---- final"; bash "$S" status "$G" "$F" | head -1 | python3 -c 'import json,sys; s=json.load(sys.stdin); print(s["settings"], s["gene_env"], s["worker_pin"], s["fri_env"])'
cat "$MOCK/units"; grep -c . "$MOCK/srv/gene/staging/batch-$G/journal"
grep -rl 'do-not-print' "$MOCK"/out-* || echo 'no secret in outputs'
