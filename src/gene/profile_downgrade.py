"""Prepare the controller database for a release without member profiles.

Run with the controller stopped and backed up, in the controller's environment so the configured
learning channels (FRI_CHANNEL_IDS, FRI_HISTORY_CHANNEL_IDS) are known:
python -m gene.profile_downgrade <database>
"""

import os
import sys
from pathlib import Path

from .store import Store


def main(argv=None):
    path, = sys.argv[1:] if argv is None else argv
    channels = [c.strip() for name in ('FRI_CHANNEL_IDS', 'FRI_HISTORY_CHANNEL_IDS')
                for c in os.environ.get(name, '').split(',') if c.strip()]
    Store(Path(path)).downgrade_member_profiles(channels)
    print('Member profile notes removed, their removals kept in every channel; saved exchanges stay in the '
          'conversation they were said in.')


if __name__ == '__main__':
    main()
