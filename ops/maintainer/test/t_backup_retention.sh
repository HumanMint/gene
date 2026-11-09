# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
release
python3 - "$BATCH_ROOT" "$BATCH_FRI_ROOT" <<'PY'
import json, os, pathlib, sys, time
gene = pathlib.Path(sys.argv[1])
manifest = json.loads(next((gene/'staging').glob('batch-*/manifest.json')).read_text())
previous = gene/'staging/batch-previous'
previous.mkdir()
manifest.update(gene=manifest['old_gene'], fri=manifest['old_fri'])
(previous/'manifest.json').write_text(json.dumps(manifest))
(previous/'journal').write_text('resumed\n')
fri = pathlib.Path(sys.argv[2])
record = json.loads((fri/'release.json').read_text())
(pathlib.Path(record['backup'])/'before.json').write_text(json.dumps({'backup': str(fri/'backups/old-01')}))
for root in sys.argv[1:]:
    for i in range(15):
        p = pathlib.Path(root)/'backups'/('old-'+str(i).zfill(2))
        p.mkdir()
        (p/'data').write_text('synthetic')
        os.utime(p, (time.time()-30*86400+i,)*2)
PY
# Both backup roots must refuse symlinks before deleting anything in either tree.
apply_refused() {
  if bash "$S" retain-backups "$G" "$F" --apply > "$MOCK/refused" 2>&1; then
    fail 'retention accepted unsafe or incomplete backup state'
  fi
  [[ -d $BATCH_ROOT/backups/old-00 && -d $BATCH_FRI_ROOT/backups/old-00 ]] || fail 'partial deletion on refusal'
}
for directory in "$BATCH_ROOT/backups" "$BATCH_FRI_ROOT/backups"; do
  mv "$directory" "$MOCK/external-backups"
  ln -s "$MOCK/external-backups" "$directory"
  apply_refused
  [[ -f $MOCK/external-backups/old-00/data ]] || fail 'deleted outside the backup root'
  unlink "$directory"
  mv "$MOCK/external-backups" "$directory"
done
before=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["backup"]+"/before.json")' "$BATCH_FRI_ROOT/release.json")
mv "$before" "$MOCK/before.saved"
apply_refused
printf '{}\n' > "$before"
apply_refused
printf '{"backup":""}\n' > "$before"
apply_refused
mv "$MOCK/before.saved" "$before"
run 0 retain-backups 0
output_has 'old-00'
[[ -d $BATCH_ROOT/backups/old-00 ]] || fail 'dry run deleted backup'
bash "$S" retain-backups "$G" "$F" --apply > "$MOCK/retained" 2>&1 || fail 'apply failed'
[[ ! -e $BATCH_ROOT/backups/old-00 ]] || fail 'apply kept eligible backup'
[[ -d $BATCH_FRI_ROOT/backups/old-01 ]] || fail 'removed previous Fri backup'
[[ -d $BATCH_ROOT/backups/old-14 ]] || fail 'apply removed recent backup'
[[ -d $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["backup"])' "$ST/manifest.json") ]] || fail 'removed rollback backup'
finish
