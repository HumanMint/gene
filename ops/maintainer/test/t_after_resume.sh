# shellcheck shell=bash
# A rollback after a completed release and resume.
. "$(dirname "$0")/common.sh"
release
expect new false up
run 0 rollback-gene 3
journal_has fri_restored
expect old true down
run 0 resume-old 1
expect old false up
finish
