"""Gene's compatibility paths invoke the separately owned adapter implementation."""

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize('name', ['audio_cache', 'audio_resolver', 'audio_relay_server', 'runtime_audio'])
def test_legacy_import_is_the_external_module_including_patch_targets(name, monkeypatch):
    legacy = importlib.import_module('gene.' + name)
    external = importlib.import_module('red_audio_adapter.' + name)
    assert legacy is external
    monkeypatch.setattr(legacy, '_compatibility_probe', object(), raising=False)
    assert external._compatibility_probe is legacy._compatibility_probe


def test_legacy_resolver_preserves_authenticated_prepare_contract(tmp_path):
    from gene.audio_resolver import create_app

    token = 'synthetic-resolver-' + 'x' * 32
    def download(result, folder, maximum):
        path = folder / 'audio.webm'
        path.write_bytes(b'synthetic complete audio')
        return path
    app = create_app(token, lambda *args, **kwargs: {
        'id': 'aBcDeFgH123', 'url': 'https://rr1.googlevideo.com/synthetic',
        'duration': 60, 'protocol': 'https'}, cache_dir=tmp_path, downloader=download)
    with TestClient(app) as client:
        request = {'identifier': 'aBcDeFgH123'}
        assert client.post('/prepare', json=request).status_code == 401
        assert client.post('/prepare', json=request,
                           headers={'Authorization': 'Bearer ' + token}).json() == {'ready': True}
    assert (tmp_path / 'aBcDeFgH123.webm').read_bytes() == b'synthetic complete audio'


def test_installed_dependency_is_the_exact_recorded_git_source():
    import tomllib
    from importlib.metadata import distribution

    root = Path(__file__).resolve().parents[1]
    pin = json.loads((root / 'docs/audio-adapter.json').read_text())
    project = tomllib.loads((root / 'pyproject.toml').read_text())
    expected = 'red-audio-adapter @ git+https://github.com/HumanMint/red-audio-adapter@' + pin['sha']
    assert expected in project['project']['dependencies']
    lock = tomllib.loads((root / 'uv.lock').read_text())
    package = next(p for p in lock['package'] if p['name'] == 'red-audio-adapter')
    assert package['source']['git'].endswith('#' + pin['sha'])
    installed = json.loads(distribution('red-audio-adapter').read_text('direct_url.json'))
    assert installed['vcs_info']['commit_id'] == pin['sha']
