# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
run 0 baseline 0
upload
for name in gene fri; do
  cp "$ST/$name.tar" "$MOCK/$name.good.tar"
  git -C "$REPO" archive "$OLD_G" > "$ST/$name.tar"
  (cd "$ST" && sha256sum "$name.tar" > "$name.tar.sha256")
  run 1 stage 2
  output_has 'archive commit is missing or does not match requested SHA'
  [[ ! -d $ST/gene-src && ! -d $ST/fri-src ]] || fail 'extracted an unverified archive'
  cp "$MOCK/$name.good.tar" "$ST/$name.tar"
  (cd "$ST" && sha256sum "$name.tar" > "$name.tar.sha256")
done
mkdir "$MOCK/plain"; echo synthetic > "$MOCK/plain/file"
tar -cf "$ST/gene.tar" -C "$MOCK/plain" .
(cd "$ST" && sha256sum gene.tar > gene.tar.sha256)
run 1 stage 2
output_has 'archive commit is missing or does not match requested SHA'
journal_lacks staged
finish
