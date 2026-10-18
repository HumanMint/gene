"""Build Gene's external source against the pinned Lavalink libraries."""

import hashlib
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVA = Path('/opt/homebrew/opt/openjdk@21/bin')
if not (JAVA / 'javac').exists():
    JAVA = Path(shutil.which('javac') or 'javac').resolve().parent
URL = 'https://github.com/Cog-Creators/Lavalink-Jars/releases/download/3.7.13%2Bred.5/Lavalink.jar'
DIGEST = '64411785e471e926a2329af600563d6fc2a33428ab2c8f6705dab358460a9bbc'


def build(folder: Path):
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    jar = folder / 'Lavalink.jar'
    if not jar.exists():
        urllib.request.urlretrieve(URL, jar)
    if hashlib.sha256(jar.read_bytes()).hexdigest() != DIGEST:
        raise ValueError('Lavalink checksum does not match the pinned release')
    build_dir = folder / 'plugin-build'
    classes, libraries = build_dir / 'classes', build_dir / 'lib'
    classes.mkdir(parents=True, exist_ok=True)
    libraries.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar) as archive:
        for name in archive.namelist():
            if name.startswith('BOOT-INF/lib/') and name.endswith('.jar'):
                (libraries / Path(name).name).write_bytes(archive.read(name))
    subprocess.run([str(JAVA / 'javac'), '--release', '17', '-cp', str(libraries / '*'),
                    '-d', str(classes), *map(str, (ROOT / 'audio-plugin/src').rglob('*.java'))], check=True)
    metadata = classes / 'lavalink-plugins'
    metadata.mkdir(exist_ok=True)
    (metadata / 'gene-youtube.properties').write_text('path=gene.audio\nname=gene-youtube\nversion=0.1.0\n')
    output = folder / 'gene-youtube.jar'
    subprocess.run([str(JAVA / 'jar'), '--create', '--file', str(output), '-C', str(classes), '.'], check=True)
    shutil.rmtree(build_dir)
    return output


if __name__ == '__main__':
    print(build(ROOT / '.audio'))
