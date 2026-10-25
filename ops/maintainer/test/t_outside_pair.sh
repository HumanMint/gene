# shellcheck shell=bash
# Live state outside the recorded pair (a third Gene release appeared): rollback refuses and changes nothing.
. "$(dirname "$0")/common.sh"
prep
run 0 gene 1
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$(printf 'a%.0s' {1..40})/" "$BATCH_ROOT/release.env"
before=$(cd "$MOCK" && sha256sum docker.json units srv/gene/release.env srv/gene/secrets/worker.env srv/fri/release.env srv/fri/release.json)
run 1 rollback-gene 1
output_has 'outside the recorded old/new pair (release.env)'
after=$(cd "$MOCK" && sha256sum docker.json units srv/gene/release.env srv/gene/secrets/worker.env srv/fri/release.env srv/fri/release.json)
[[ $before == "$after" ]] || fail 'the refused rollback changed something'
journal_lacks gene_rolled_back
finish
