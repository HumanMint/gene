import os
import stat
import subprocess

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
    path = tmp_path / '.env.local'
    dev.main(['--env', str(path), 'init'])
    if values:
        dev.write_private(path, dev.merged(path.read_text().splitlines(), values))
    return path


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_template_ships_without_service_tokens():
    template = dev.read_env(dev.ROOT / '.env.example')
    assert all(template[key] == '' for key in dev.SERVICE_TOKENS)
    assert all(key in template for key in (*dev.FRI_IDS, *dev.RED_KEYS))


def test_init_writes_a_private_file_with_four_distinct_tokens(tmp_path, capsys):
    path = initialized(tmp_path)
    env = dev.read_env(path)
    tokens = [env[key] for key in dev.SERVICE_TOKENS]
    assert mode(path) == 0o600
    assert len(set(tokens)) == 4 and all(len(token) >= 32 for token in tokens)
    assert env['GENE_CONTROLLER_URL'] == 'http://127.0.0.1:8787'
    assert '# Gene configuration template' in path.read_text()
    assert not any(token in capsys.readouterr().out for token in tokens)


def test_init_keeps_existing_values_and_fills_only_missing_tokens(tmp_path):
    path = initialized(tmp_path, GENE_FRI_TOKEN='', GENE_GUILD_ID='42', CUSTOM='kept')
    before = dev.read_env(path)
    dev.main(['--env', str(path), 'init'])
    after = dev.read_env(path)
    assert after['GENE_FRI_TOKEN'] and after['GENE_FRI_TOKEN'] not in before.values()
    assert {key: after[key] for key in before if key != 'GENE_FRI_TOKEN'} == {
        key: value for key, value in before.items() if key != 'GENE_FRI_TOKEN'}
    dev.main(['--env', str(path), 'init'])
    assert dev.read_env(path) == after


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


def test_fri_env_merges_the_connection_into_a_private_file(tmp_path, capsys):
    source = dev.read_env(initialized(tmp_path, **IDS))
    target = tmp_path / 'fri' / 'fri.env'
    target.parent.mkdir()
    target.write_text('# fri settings\nDISCORD_TOKEN=fri-bot\nGENE_CONTROLLER_URL=http://controller:8787\n')
    dev.main(['--env', str(tmp_path / '.env.local'), 'fri-env', str(target), '--port', '18787'])
    written = dev.read_env(target)
    assert mode(target) == 0o600
    assert written == {'DISCORD_TOKEN': 'fri-bot', 'GENE_CONTROLLER_URL': 'http://127.0.0.1:18787',
                       'GENE_FRI_TOKEN': source['GENE_FRI_TOKEN'], **IDS}
    assert target.read_text().startswith('# fri settings\n')
    assert source['GENE_FRI_TOKEN'] not in capsys.readouterr().out


def test_fri_env_needs_the_ids_fri_reads_at_startup(tmp_path):
    path = initialized(tmp_path)
    with pytest.raises(SystemExit, match='GENE_GUILD_ID, GENE_OWNER_ID, GENE_WORKSHOP_CHANNEL_ID'):
        dev.main(['--env', str(path), 'fri-env', str(tmp_path / 'fri.env')])
    assert not (tmp_path / 'fri.env').exists()


def test_fri_env_refuses_files_git_tracks_and_its_own_source(tmp_path):
    path = initialized(tmp_path, **IDS)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / '.env.example').write_text('GENE_FRI_TOKEN=\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.env.example'], check=True)
    for target in (repo / '.env.example', path):
        before = target.read_text()
        with pytest.raises(SystemExit, match='Refusing'):
            dev.main(['--env', str(path), 'fri-env', str(target)])
        assert target.read_text() == before


def test_merged_rejects_multiline_values():
    with pytest.raises(ValueError):
        dev.merged([], {'GENE_GUILD_ID': '1\nGENE_OWNER_ID=2'})
