# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
touch "$MOCK/masked"
systemctl stop gene-worker gene-deployer
release
expect new true down
run 0 resume 0
expect new true down
run 0 rollback-gene 0
run 0 resume-old 0
expect old true down
finish
