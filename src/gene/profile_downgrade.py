"""Prepare the controller database for a release without member profiles.

Run with the controller stopped and backed up: python -m gene.profile_downgrade <database>
"""

import sys
from pathlib import Path

from .store import Store


def main(argv=None):
    path, = sys.argv[1:] if argv is None else argv
    Store(Path(path)).downgrade_member_profiles()
    print('Member profile notes removed; saved exchanges stay in the conversation they were said in.')


if __name__ == '__main__':
    main()
