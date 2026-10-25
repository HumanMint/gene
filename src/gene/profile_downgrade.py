"""Prepare the controller database for a release without member profiles.

Run with the controller stopped and backed up, in the controller's environment so the configured
chat and learning channels (FRI_CHANNEL_IDS, FRI_HISTORY_CHANNEL_IDS) are known, or name them:
python -m gene.profile_downgrade <database> [--channels 1,2,3]
"""

import os
import sys
from pathlib import Path

from .store import Store


def main(argv=None):
    path, *rest = sys.argv[1:] if argv is None else argv
    if rest and (len(rest) != 2 or rest[0] != '--channels'):
        raise SystemExit(__doc__)
    named = rest[1] if rest else ''
    channels = [c.strip() for value in (os.environ.get('FRI_CHANNEL_IDS', ''),
                                        os.environ.get('FRI_HISTORY_CHANNEL_IDS', ''), named)
                for c in value.split(',') if c.strip()]
    try:
        Store(Path(path)).downgrade_member_profiles(channels)
    except ValueError as error:
        raise SystemExit(f'Nothing changed. {error}') from None
    print('Member profile notes removed, their removals kept in every channel; saved exchanges stay in the '
          'conversation they were said in.')


if __name__ == '__main__':
    main()
