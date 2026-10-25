. "$(dirname "$0")/common.sh"
prep
run gene 1 && run fri 1 && run resume 0
echo '== manual rollback after resume'
run rollback-gene 12
summary
run resume-old 1; summary
