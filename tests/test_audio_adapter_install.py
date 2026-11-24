"""The image bundle is pinned independently of the unchanged host environment."""
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audio_bundle_installer', ROOT / 'scripts/install_audio_adapter.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def pin():
    contents = b'# synthetic module\n'
    return {'schema': 2, 'repository': installer.REPOSITORY, 'sha': 'a' * 40, 'version': '0.1.0',
            'modules': {name: hashlib.sha256(contents).hexdigest() for name in installer.MODULES}}


def bundle(path, pin, requires=''):
    package = path / 'red_audio_adapter'
    package.mkdir(parents=True)
    (path / '.lock').touch()  # uv's target environment lock, not importable code.
    for name in pin['modules']:
        (package / name).write_bytes(b'# synthetic module\n')
    metadata = path / 'red_audio_adapter-0.1.0.dist-info'
    metadata.mkdir()
    (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: red-audio-adapter\nVersion: 0.1.0\n' + requires)
    (metadata / 'direct_url.json').write_text(json.dumps({
        'url': pin['repository'], 'vcs_info': {'vcs': 'git', 'commit_id': pin['sha']}}))


def test_fresh_install_selects_only_the_verified_bundle(tmp_path, monkeypatch, pin):
    target, site = tmp_path / 'bundle', tmp_path / 'site'
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'uv':
            bundle(Path(args[args.index('--target') + 1]), pin)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    installer.install(pin, target, site)
    assert (site / '_gene_audio_adapter.pth').read_text() == str(target) + '\n'
    assert '--no-deps' in calls[0] and '--no-cache' in calls[0]
    assert calls[0][-1] == 'red-audio-adapter @ git+' + installer.REPOSITORY + '@' + pin['sha']
    assert (site / '_gene_audio_adapter.pth').stat().st_mode & 0o777 == 0o644
    installer.install(pin, target, site)
    assert len(calls) == 2  # cached verification does not run another installer/chmod


@pytest.mark.parametrize('change', ['module', 'provenance', 'requirement', 'unrelated'])
def test_a_bad_cached_bundle_leaves_the_existing_pointer_unchanged(tmp_path, monkeypatch, pin, change):
    target, site = tmp_path / 'bundle', tmp_path / 'site'
    bundle(target, pin, 'Requires-Dist: httpx>=99\n' if change == 'requirement' else '')
    site.mkdir()
    pointer = site / '_gene_audio_adapter.pth'
    pointer.write_text('/synthetic/previous-bundle\n')
    if change == 'module':
        (target / 'red_audio_adapter/audio_cache.py').write_text('# changed\n')
    elif change == 'provenance':
        (target / 'red_audio_adapter-0.1.0.dist-info/direct_url.json').write_text('{}')
    elif change == 'unrelated':
        (target / 'sitecustomize.py').write_text('raise AssertionError\n')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k: pytest.fail('must not fetch'))
    with pytest.raises(ValueError):
        installer.install(pin, target, site)
    assert pointer.read_text() == '/synthetic/previous-bundle\n'


def test_a_failed_download_never_selects_a_partial_bundle(tmp_path, monkeypatch, pin):
    target, site = tmp_path / 'bundle', tmp_path / 'site'
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(installer.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(pin, target, site)
    assert not target.exists() and not site.exists()
    assert not list(tmp_path.glob('.audio-adapter-*'))


def test_invalid_pin_and_indirect_target_are_refused_before_fetch(tmp_path, monkeypatch, pin):
    path = tmp_path / 'pin.json'
    path.write_text(json.dumps(pin | {'repository': 'https://example.invalid/other'}))
    with pytest.raises(ValueError):
        installer.load_pin(path)
    target = tmp_path / 'bundle'
    target.symlink_to(tmp_path / 'missing')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k: pytest.fail('must not fetch'))
    with pytest.raises(ValueError):
        installer.install(pin, target, tmp_path / 'site')
    assert target.is_symlink()
    with pytest.raises(ValueError):
        installer.install(pin, tmp_path / 'bundle\nimport os', tmp_path / 'site')


def test_host_service_modules_import_without_the_audio_package(tmp_path):
    code = '''import importlib.abc,sys
class BlockAdapter(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name=='red_audio_adapter' or name.startswith('red_audio_adapter.'):
   raise ModuleNotFoundError('deliberately unavailable',name=name)
sys.meta_path.insert(0,BlockAdapter())
import gene.api,gene.worker,gene.deploy
assert not any(n=='red_audio_adapter' or n.startswith('red_audio_adapter.') for n in sys.modules)
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, capture_output=True, text=True,
                            env={'HOME': str(tmp_path), 'TMPDIR': str(tmp_path),
                                 'GENE_DB': str(tmp_path / 'controller.sqlite3'),
                                 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stderr
