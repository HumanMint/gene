"""Load this immutable relay's private settings, then start the download service."""

import os
import runpy
from pathlib import Path

folder = Path(__file__).resolve().parent
allowed = {'GENE_RESOLVER_TOKEN', 'GENE_RELAY_ONLY', 'GENE_RESOLVER_BIND',
           'GENE_RELAY_PEER', 'GENE_RESOLVER_PORT', 'GENE_AUDIO_CACHE'}
for line in (folder / 'resolver.env').read_text().splitlines():
    key, value = line.split('=', 1)
    if key in allowed:
        os.environ[key] = value
runpy.run_module('gene.audio_resolver', run_name='__main__')
