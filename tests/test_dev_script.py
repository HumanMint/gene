import os
import secrets
import stat

import pytest

from scripts import dev

IDS = {'GENE_GUILD_ID': '100000000000000001', 'GENE_OWNER_ID': '100000000000000002',
       'GENE_WORKSHOP_CHANNEL_ID': '100000000000000003'}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    # check and controller export the file into os.environ; keep that out of other tests.
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    for key in list(os.environ):
        if key.startswith(('GENE_', 'FRI_')) or key == 'DISCORD_TOKEN':
            del os.environ[key]


def initialized(tmp_path, **values):
    """.env.local as a newcomer leaves it: init's template, a pasted token per service, then their values."""
    path = tmp_path / '.env.local'
    dev.main(['--env', str(path), 'init'])
    settings = {**{key: secrets.token_urlsafe(48) for key in dev.SERVICE_TOKENS}, **values}
    lines = [line for line in path.read_text().splitlines() if line.split('=', 1)[0] not in settings]
    dev.write_private(path, lines + [f'{key}={value}' for key, value in settings.items()])
    return path


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_template_ships_without_service_tokens():
    template = dev.read_env(dev.ROOT / '.env.example')
    assert all(template[key] == '' for key in dev.SERVICE_TOKENS)
    assert all(key in template for key in (*dev.FRI_IDS, *dev.RED_KEYS))


def test_init_writes_the_template_with_no_tokens_and_says_which_it_needs(tmp_path, capsys):
    path = tmp_path / '.env.local'
    dev.main(['--env', str(path), 'init'])
    assert mode(path) == 0o600
    assert path.read_text() == (dev.ROOT / '.env.example').read_text()
    assert all(dev.read_env(path)[key] == '' for key in dev.SERVICE_TOKENS)
    out = capsys.readouterr().out
    assert all(f'{key}=' in out for key in dev.SERVICE_TOKENS) and 'scripts/dev.py token' in out


def test_init_never_changes_an_existing_file(tmp_path, capsys):
    path = initialized(tmp_path, GENE_FRI_TOKEN='', CUSTOM='kept')
    before = path.read_text()
    capsys.readouterr()
    dev.main(['--env', str(path), 'init'])
    assert path.read_text() == before
    out = capsys.readouterr().out
    assert 'nothing changed' in out and 'GENE_FRI_TOKEN=' in out and 'GENE_RUNTIME_TOKEN=' not in out
    assert not any(value in out for value in dev.read_env(path).values() if len(value) >= 32)


def test_token_prints_a_fresh_random_value_and_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dev.main(['--env', str(tmp_path / '.env.local'), 'token'])
    dev.main(['--env', str(tmp_path / '.env.local'), 'token'])
    first, second = capsys.readouterr().out.split()
    assert first != second and len(first) >= 64
    assert set(first) <= set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_')
    assert list(tmp_path.iterdir()) == []


def test_check_dry_runs_the_controller_without_printing_values(tmp_path, capsys):
    database = tmp_path / 'state' / 'gene.sqlite3'
    path = initialized(tmp_path, GENE_DB=str(database), **IDS)
    dev.main(['--env', str(path), 'check'])
    out = capsys.readouterr().out
    assert 'controller dry run: /health ok, gene-fri/1' in out
    assert 'ok     Fri pairing' in out
    tokens = [value for key, value in dev.read_env(path).items() if key.endswith('TOKEN') and value]
    assert len(tokens) == 4 and not any(token in out for token in tokens)
    assert not database.parent.exists()  # The dry run uses a throwaway database.


@pytest.mark.parametrize('values, message', [
    ({'GENE_DEPLOY_TOKEN': 'short'}, 'at least 32 characters'),
    ({'GENE_WORKER_TOKEN': 'x' * 40, 'GENE_DEPLOY_TOKEN': 'x' * 40}, 'must all differ'),
    ({'GENE_OWNER_ID': '@me'}, 'GENE_OWNER_ID must be a numeric Discord ID'),
    ({'FRI_CHANNEL_IDS': '1,two'}, 'FRI_CHANNEL_IDS must be comma-separated'),
])
def test_check_rejects_invalid_configuration(tmp_path, capsys, values, message):
    path = initialized(tmp_path, **values)
    with pytest.raises(SystemExit):
        dev.main(['--env', str(path), 'check'])
    assert message in capsys.readouterr().out


def test_check_without_a_file_points_to_init(tmp_path):
    with pytest.raises(SystemExit, match='scripts/dev.py init'):
        dev.main(['--env', str(tmp_path / 'missing.env'), 'check'])


def test_apply_resolves_the_database_in_the_checkout_and_lets_the_shell_win(tmp_path):
    os.environ['GENE_OWNER_ID'] = '7'
    dev.apply({'GENE_DB': '.state/gene.sqlite3', 'GENE_OWNER_ID': '8', 'GENE_GUILD_ID': '9'})
    assert os.environ['GENE_DB'] == str(dev.ROOT / '.state' / 'gene.sqlite3')
    assert (os.environ['GENE_OWNER_ID'], os.environ['GENE_GUILD_ID']) == ('7', '9')


def test_fri_env_prints_the_shared_lines_but_never_the_token(tmp_path, capsys):
    path = initialized(tmp_path, **IDS)
    capsys.readouterr()
    dev.main(['--env', str(path), 'fri-env', '--port', '18787'])
    out = capsys.readouterr().out
    assert 'GENE_CONTROLLER_URL=http://127.0.0.1:18787' in out
    assert all(f'{key}={value}' in out for key, value in IDS.items())
    assert 'GENE_FRI_TOKEN=' in out and dev.read_env(path)['GENE_FRI_TOKEN'] not in out
    assert sorted(item.name for item in tmp_path.iterdir()) == ['.env.local']


def test_fri_env_needs_the_ids_fri_reads_at_startup(tmp_path):
    path = initialized(tmp_path)
    with pytest.raises(SystemExit, match='GENE_GUILD_ID, GENE_OWNER_ID, GENE_WORKSHOP_CHANNEL_ID'):
        dev.main(['--env', str(path), 'fri-env'])
