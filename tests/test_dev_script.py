import os
import stat
import subprocess
import unicodedata

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


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    # Tokens may only go into git-ignored files in a git checkout, as .env.local is in Gene and Fri.
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text('*.env\n.env.local\nignored/\n')


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


def test_fri_env_needs_an_existing_fri_checkout(tmp_path):
    path = initialized(tmp_path, **IDS)
    with pytest.raises(SystemExit, match='Clone Fri first'):
        dev.main(['--env', str(path), 'fri-env', str(tmp_path / 'fri' / '.env.local')])
    assert not (tmp_path / 'fri').exists()


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


def tracked_repo(tmp_path, *names):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    for name in names:
        (repo / name).write_text('GENE_FRI_TOKEN=\nGENE_RUNTIME_TOKEN=\n')
        subprocess.run(['git', '-C', str(repo), 'add', name], check=True)
    return repo


def test_init_refuses_templates_and_tracked_files(tmp_path, monkeypatch):
    def wrote(*_):
        raise AssertionError('a token was written')
    repo = tracked_repo(tmp_path, '.env.example', 'settings.env')
    (tmp_path / 'ignored').mkdir()
    (tmp_path / 'ignored' / '.env.example').write_text('GENE_FRI_TOKEN=\n')  # Ignored, but a template.
    targets = [repo / '.env.example', repo / 'settings.env', tmp_path / 'ignored' / '.env.example']
    before = [target.read_text() for target in targets]
    for target in targets:
        with pytest.raises(SystemExit, match='Refusing'):
            dev.main(['--env', str(target), 'init'])
    assert [target.read_text() for target in targets] == before
    monkeypatch.setattr(dev, 'write_private', wrote)  # Never touch this checkout's real template.
    with pytest.raises(SystemExit, match='Refusing'):
        dev.main(['--env', str(dev.ROOT / '.env.example'), 'init'])


def test_token_writes_refuse_a_tracked_file_named_in_another_case(tmp_path):
    # On a case-insensitive disk .ENV.REVIEW is the tracked .env.review, even though the ignore rules
    # match it; refuse on every disk so no token reaches a tracked file through a case alias.
    path = initialized(tmp_path, **IDS)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / '.gitignore').write_text('.env.*\n.ENV.*\n')
    (repo / '.env.review').write_text('GENE_FRI_TOKEN=\nGENE_RUNTIME_TOKEN=\n')
    subprocess.run(['git', '-C', str(repo), 'add', '-f', '.env.review'], check=True)
    alias = repo / '.ENV.REVIEW'
    for command in (['--env', str(alias), 'init'], ['--env', str(path), 'fri-env', str(alias)]):
        with pytest.raises(SystemExit, match='Git tracks it'):
            dev.main(command)
    assert (repo / '.env.review').read_text() == 'GENE_FRI_TOKEN=\nGENE_RUNTIME_TOKEN=\n'


def tracked_status(repo):
    """git status for tracked files only, to compare before and after a refused write."""
    return subprocess.run(['git', '-C', str(repo), 'status', '--porcelain', '--untracked-files=no'],
                          capture_output=True, text=True, check=True).stdout


def test_token_writes_never_reach_a_tracked_file_through_a_non_ascii_case_alias(tmp_path):
    # Git folds only ASCII case, but a case-insensitive disk also makes .env.ä the tracked .env.Ä.
    path = initialized(tmp_path, **IDS)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / '.gitignore').write_text('.env.*\n')
    (repo / '.env.\u00c4').write_text('')
    subprocess.run(['git', '-C', str(repo), 'add', '-f', '.env.\u00c4'], check=True)
    alias = repo / '.env.\u00e4'
    folds = alias.exists()  # The same file on this disk?
    before = tracked_status(repo)
    for command in (['--env', str(alias), 'init'], ['--env', str(path), 'fri-env', str(alias)]):
        try:
            dev.main(command)
            assert not folds, 'wrote through an alias of a tracked file'
        except SystemExit as refusal:
            assert 'Git tracks it' in str(refusal)
        assert tracked_status(repo) == before and (repo / '.env.\u00c4').read_text() == ''


def test_token_writes_refuse_a_file_an_outer_checkout_tracks_under_a_nested_one(tmp_path):
    path = initialized(tmp_path, **IDS)
    outer = tmp_path / 'outer'
    (outer / 'nested').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(outer)], check=True)
    (outer / 'nested' / '.env.local').write_text('GENE_FRI_TOKEN=\n')
    subprocess.run(['git', '-C', str(outer), 'add', 'nested/.env.local'], check=True)
    subprocess.run(['git', 'init', '-q', str(outer / 'nested')], check=True)  # Hides the outer index.
    (outer / 'nested' / '.gitignore').write_text('.env*\n')
    target = outer / 'nested' / '.env.local'
    before = tracked_status(outer)
    for command in (['--env', str(target), 'init'], ['--env', str(path), 'fri-env', str(target)]):
        with pytest.raises(SystemExit, match='Git tracks it'):
            dev.main(command)
    assert target.read_text() == 'GENE_FRI_TOKEN=\n' and tracked_status(outer) == before


def test_token_writes_never_reach_a_tracked_file_through_a_folder_alias(tmp_path):
    # On macOS config/.env.ä can be the tracked Config/.env.Ä: the folder's case and the name's non-ASCII
    # case both differ, so no spelling-based lookup finds it.
    path = initialized(tmp_path, **IDS)
    repo = tmp_path / 'repo'
    (repo / 'Config').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / '.gitignore').write_text('.env.*\n')
    (repo / 'Config' / '.env.\u00c4').write_text('')
    subprocess.run(['git', '-C', str(repo), 'add', '-f', 'Config/.env.\u00c4'], check=True)
    alias = repo / 'config' / '.env.\u00e4'
    folds = alias.exists()
    before = tracked_status(repo)
    for command in (['--env', str(alias), 'init'], ['--env', str(path), 'fri-env', str(alias)]):
        try:
            dev.main(command)
            assert not folds, 'wrote through an alias of a tracked file'
        except SystemExit as refusal:
            assert not folds or 'Git tracks it' in str(refusal)
        assert tracked_status(repo) == before and (repo / 'Config' / '.env.\u00c4').read_text() == ''


def test_token_writes_refuse_when_an_outer_checkout_cannot_be_read(tmp_path):
    # A broken outer checkout is a refusal, not the end of the search: it may track the file.
    path = initialized(tmp_path, **IDS)
    outer = tmp_path / 'outer'
    (outer / 'nested').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(outer)], check=True)
    (outer / 'nested' / '.env.local').write_text('GENE_FRI_TOKEN=\n')
    subprocess.run(['git', '-C', str(outer), 'add', 'nested/.env.local'], check=True)
    subprocess.run(['git', 'init', '-q', str(outer / 'nested')], check=True)
    (outer / 'nested' / '.gitignore').write_text('.env*\n')
    with (outer / '.git' / 'config').open('a') as config:
        config.write('[core\nbroken')
    target = outer / 'nested' / '.env.local'
    for command in (['--env', str(target), 'init'], ['--env', str(path), 'fri-env', str(target)]):
        with pytest.raises(SystemExit, match="couldn't confirm"):
            dev.main(command)
    assert target.read_text() == 'GENE_FRI_TOKEN=\n'


def test_a_folder_named_in_another_unicode_form_still_takes_its_ignored_env_file(tmp_path):
    composed, decomposed = (unicodedata.normalize(form, 'caf\u00e9') for form in ('NFC', 'NFD'))
    repo = tmp_path / composed
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / '.gitignore').write_text('.env.local\n')
    spelled = tmp_path / decomposed / '.env.local'
    if not spelled.parent.exists():
        pytest.skip('this disk keeps the two forms apart, so the other spelling is another folder')
    dev.main(['--env', str(spelled), 'init'])
    assert mode(repo / '.env.local') == 0o600


def test_token_writes_need_an_ignored_file_in_a_working_checkout(tmp_path):
    path = initialized(tmp_path, **IDS)
    broken = tmp_path / 'broken'  # A checkout whose repository is gone: every git command fails.
    broken.mkdir()
    (broken / '.git').write_text(f'gitdir: {tmp_path / "gone"}\n')
    (tmp_path / 'fri').mkdir()
    (tmp_path / 'fri' / 'notes.txt').write_text('')  # Untracked but not ignored: could be committed.
    for target in (broken / 'fri.env', tmp_path / 'fri' / 'notes.txt'):
        with pytest.raises(SystemExit, match='Refusing'):
            dev.main(['--env', str(path), 'fri-env', str(target)])
        with pytest.raises(SystemExit, match='Refusing'):
            dev.main(['--env', str(target), 'init'])
    assert not (broken / 'fri.env').exists() and (tmp_path / 'fri' / 'notes.txt').read_text() == ''


def test_token_writes_refuse_when_git_is_missing(tmp_path):
    path = initialized(tmp_path, **IDS)
    repo = tracked_repo(tmp_path, '.env.example')
    (tmp_path / 'fri').mkdir()
    empty = tmp_path / 'no-git'
    empty.mkdir()
    os.environ['PATH'] = str(empty)  # Without git, nothing can be confirmed.
    for target in (repo / '.env.example', tmp_path / 'fri' / 'fri.env'):
        with pytest.raises(SystemExit, match='Refusing'):
            dev.main(['--env', str(path), 'fri-env', str(target)])
    with pytest.raises(SystemExit, match='Refusing'):
        dev.main(['--env', str(tmp_path / 'new.env'), 'init'])
    assert dev.read_env(repo / '.env.example')['GENE_FRI_TOKEN'] == ''
    assert not (tmp_path / 'fri' / 'fri.env').exists() and not (tmp_path / 'new.env').exists()


def test_fri_env_replaces_every_copy_of_a_key(tmp_path):
    source = dev.read_env(initialized(tmp_path, **IDS))
    target = tmp_path / 'fri' / '.env.local'
    target.parent.mkdir()
    target.write_text('GENE_FRI_TOKEN=stale-1\n# keep me\nDISCORD_TOKEN=fri-bot\n'
                      '  GENE_FRI_TOKEN = stale-2\nGENE_GUILD_ID=1\nGENE_GUILD_ID=2\n')
    dev.main(['--env', str(tmp_path / '.env.local'), 'fri-env', str(target)])
    lines = target.read_text().splitlines()
    # Fri reads stripped KEY=VALUE lines and the last copy of a key wins.
    fri_reads = dict((key.strip(), value.strip()) for key, value in
                     (line.split('=', 1) for line in lines if '=' in line and not line.startswith('#')))
    assert fri_reads['GENE_FRI_TOKEN'] == source['GENE_FRI_TOKEN']
    assert fri_reads['GENE_GUILD_ID'] == IDS['GENE_GUILD_ID'] and fri_reads['DISCORD_TOKEN'] == 'fri-bot'
    assert [line.split('=', 1)[0].strip() for line in lines].count('GENE_FRI_TOKEN') == 1
    assert '# keep me' in lines
