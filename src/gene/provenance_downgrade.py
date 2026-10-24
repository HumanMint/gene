"""Prepare the controller database for a release without turn-provenance.

Run with the controller stopped and backed up: python -m gene.provenance_downgrade <database>
"""

import sys
from pathlib import Path

from .store import Store


def main(argv=None):
    path, = sys.argv[1:] if argv is None else argv
    Store(Path(path)).downgrade_turn_provenance()
    print('Derived turns, questions and requests removed; agent_summaries uses the older schema.')


if __name__ == '__main__':
    main()
