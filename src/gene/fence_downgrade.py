"""Prepare the controller database for a release without message fences (gene #149).

An older controller neither reads nor expires `chat_message_fences`, so a rollback to one clears its rows: each holds
a withdrawn message's ID, some its author too, and none may outlive the week they're kept for. The table stays, since
the privacy triggers on `chat_revisions` name it. Run with the controller stopped and backed up:
python -m gene.fence_downgrade <database>
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit(__doc__)
    try:  # Read-write, never create: a missing file is refused, not made.
        with closing(sqlite3.connect(Path(argv[0]).resolve().as_uri()+'?mode=rw', uri=True, timeout=30)) as db:
            with db:
                held = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_message_fences'")
                removed = db.execute('DELETE FROM chat_message_fences').rowcount if held.fetchone() else 0
    except sqlite3.Error as error:
        raise SystemExit(f'Nothing changed. {argv[0]} is not a writable controller database ({error}).') from None
    print(f'Message fences removed: {removed}.')


if __name__ == '__main__':
    main()
