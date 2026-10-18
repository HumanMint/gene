"""Load only this audio release's private resolver credential, then start it."""

import os
import runpy
from pathlib import Path

folder = Path(__file__).resolve().parent
for line in (folder / 'resolver.env').read_text().splitlines():
    key, value = line.split('=', 1)
    if key == 'GENE_RESOLVER_TOKEN':
        os.environ[key] = value
os.environ['GENE_AUDIO_CACHE'] = str(folder / 'media')
runpy.run_path(str(folder / 'audio_resolver.py'), run_name='__main__')
