"""Gene's coding worker on Claude: a fake `claude` binary emits canned stream-json inside a fake `docker run`."""

import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from gene.repair_policy import REVIEW_EFFORT, REVIEW_MODEL
from gene.repair_worker import RepairWork
from gene.usage import parse_usage
from gene.worker import NeedsAttention, Worker
from tests.test_repairs import proposal, queued

KEY = 'sk-test-not-a-real-key'
FAKE = '''#!{python}
import json, os, pathlib, sys
spec = json.loads(pathlib.Path(os.environ['FAKE_SCENARIO']).read_text())
step = spec.pop(0)
pathlib.Path(os.environ['FAKE_SCENARIO']).write_text(json.dumps(spec))
sys.stdin.read()
for name, text in step.get('edit', {{}}).items():
    pathlib.Path(name).write_text(text)
for event in step['events']:
    print(json.dumps(event))
sys.exit(step.get('exit', 0))
'''


def result(cost=0.5, **extra):
    return {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 3, 'result': 'Done.',
            'total_cost_usd': cost, 'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 20,
            'cache_read_input_tokens': 70, 'output_tokens': 5, 'output_tokens_details': {'thinking_tokens': 2}},
            **extra}


API_ERROR = result(0, is_error=True, api_error='rate_limit', api_error_status=429, result='API Error: 429')
CODEX = {'type': 'turn.completed', 'usage': {'input_tokens': 9, 'output_tokens': 1}}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Run the in-container command from `docker run` locally, in the worktree, with fake CLIs on PATH."""
    bin_dir, scenario = tmp_path / 'bin', tmp_path / 'scenario.json'
    bin_dir.mkdir()
    for name in ('claude', 'codex'):  # run through the interpreter: CI's in-image /tmp may be noexec
        (bin_dir / name).write_text(FAKE.format(python=sys.executable))
    calls = []
    real = subprocess.run

    def run(args, **kwargs):
        if args[:2] == ['docker', 'rm']:
            return subprocess.CompletedProcess(args, 0, '', '')
        calls.append(args)
        image = args.index('test-image')
        command, root = args[image + 1:], next(a for a in args if a.endswith(',dst=/workspace')
                                                or a.endswith(',dst=/workspace,readonly')).split(',')[1][4:]
        if command[0] not in ('claude', 'codex'):
            return subprocess.CompletedProcess(args, 0, '', '')
        return real([sys.executable, str(bin_dir / command[0]), *command[1:]], cwd=root, env={'FAKE_SCENARIO': str(scenario)},
                    **kwargs)
    monkeypatch.setattr('gene.worker.subprocess.run', run)
    monkeypatch.setattr('gene.worker.os.geteuid', lambda: 1000)
    return lambda *steps: scenario.write_text(json.dumps(list(steps))), calls


def make_worker(tmp_path, *, claude=True, codex=True):
    worker = Worker.__new__(Worker)
    worker.root, worker.auth = tmp_path / 'jobs', tmp_path / 'codex-home'
    worker.root.mkdir()
    worker.auth.mkdir()
    if codex:
        (worker.auth / 'auth.json').write_text('{}')
    worker.claude_key = tmp_path / 'claude-key'
    if claude:
        worker.claude_key.write_text(KEY)
    worker.repo, worker.image, worker.api = 'HumanMint/gene', 'test-image', Mock()
    return worker


def feature(tmp_path, monkeypatch, worker):
    def git(root, *args):
        if args[0] == 'clone':
            (tmp_path / 'jobs/job/checkout').mkdir()
        return 'b' * 40 if args[0] == 'rev-parse' else ''
    monkeypatch.setattr('gene.worker.git', git)
    monkeypatch.setattr('gene.worker.validate_checkout', lambda root: ['cogs/feature.py'])
    job = {'id': 'job', 'lease': 'lease', 'model': 'gpt-6.2-sol', 'effort': 'high', 'base_sha': 'a' * 40,
           'body': 'Add a feature', 'source_url': '', 'memories': []}
    worker.api.post = Mock(side_effect=lambda path, data=None: job if path == '/worker/claim' else
                           {'refresh_pending': False} if path == '/worker/models/state' else None)
    return job


def posts(worker, suffix):
    return [c.args[1] for c in worker.api.post.call_args_list if c.args[0].endswith(suffix)]


def test_claude_feature_run_uses_read_only_key_tight_tools_and_reports_cost(tmp_path, monkeypatch, sandbox):
    script, calls = sandbox
    script({'events': [result(1.25)], 'edit': {'feature.txt': 'x'}})
    worker = make_worker(tmp_path)
    worker.process(feature(tmp_path, monkeypatch, worker))
    args = calls[0]
    command = args[args.index('test-image') + 1:]
    assert command[:3] == ['claude', '-p', '--output-format']
    assert command[command.index('--model') + 1] == 'claude-opus-5-5'
    assert command[command.index('--effort') + 1] == 'high'
    assert command[command.index('--max-turns') + 1] == '60'
    assert command[command.index('--max-budget-usd') + 1] == '5.00'
    assert command[command.index('--tools') + 1] == command[command.index('--allowedTools') + 1] == \
        'Read,Edit,Write,Glob,Grep'
    assert json.loads(command[command.index('--settings') + 1]) == {'apiKeyHelper': 'cat /claude/key'}
    assert f'type=bind,src={worker.claude_key},dst=/claude/key,readonly' in args
    assert not any('/codex' in a for a in args) and '--network=none' not in args
    assert all(KEY not in a for a in args)  # the key itself is never an argument
    env = [args[i + 1] for i, a in enumerate(args) if a == '-e']
    assert {'CLAUDE_CONFIG_DIR=/tmp/claude', 'DISABLE_AUTOUPDATER=1',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1'} <= set(env)
    assert not any('key' in e.lower() for e in env)
    [usage] = posts(worker, '/usage')
    assert usage['provider'] == 'claude' and usage['cost_micros'] == 1_250_000
    assert usage['model'] == 'claude-opus-5-5'
    assert usage['counters'] == {'input_tokens': 100, 'cached_input_tokens': 70, 'output_tokens': 5,
                                 'reasoning_output_tokens': 2, 'turns': 3}
    assert posts(worker, '/finish')[0]['proposal']['summary'] == 'Done.'


def test_without_a_claude_key_the_codex_path_is_unchanged(tmp_path, monkeypatch, sandbox):
    script, calls = sandbox
    script({'events': [CODEX]})
    worker = make_worker(tmp_path, claude=False)
    worker.process(feature(tmp_path, monkeypatch, worker))
    args = calls[0]
    assert args[args.index('test-image') + 1:] == [
        'codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
        '--dangerously-bypass-approvals-and-sandbox', '--json', '--color', 'never',
        '--model', 'gpt-6.2-sol', '-c', 'model_reasoning_effort="high"', '-']
    image = args.index('test-image')
    assert args[image - 3:image] == ['-e', 'CODEX_HOME=/codex', '-i']
    assert not any('/claude/key' in a or 'CLAUDE_' in a for a in args)
    [usage] = posts(worker, '/usage')
    assert set(usage) == {'lease', 'attempt_id', 'model', 'counters'} and usage['model'] == 'gpt-6.2-sol'


@pytest.mark.parametrize('over', [result(5.01), result(4.0, subtype='error_max_budget_usd', is_error=True)])
def test_spending_cap_stops_the_job_for_the_owner_without_another_attempt(tmp_path, monkeypatch, sandbox, over):
    script, calls = sandbox
    script({'events': [over], 'exit': 1 if over['is_error'] else 0})
    worker = make_worker(tmp_path)
    feature(tmp_path, monkeypatch, worker)
    assert worker.tick() is True
    assert len(calls) == 1 and len(posts(worker, '/usage')) == 1
    finish = posts(worker, '/finish')[0]
    assert finish['outcome'] == 'needs_attention' and 'spending cap' in finish['error']


def test_the_cap_is_per_job_across_check_repair_attempts(tmp_path, monkeypatch, sandbox):
    script, calls = sandbox
    script({'events': [result(3.0)]}, {'events': [result(2.5)]})
    worker = make_worker(tmp_path)
    job = feature(tmp_path, monkeypatch, worker)
    inner, fails = __import__('gene.worker').worker.subprocess.run, [1]

    def run(args, **kwargs):  # the first pytest run fails, so the model gets a second attempt
        if 'pytest' in args and fails:
            return subprocess.CompletedProcess(args, fails.pop(), 'A test failed', '')
        return inner(args, **kwargs)
    monkeypatch.setattr('gene.worker.subprocess.run', run)
    with pytest.raises(NeedsAttention):
        worker.process(job)
    second = calls[1][calls[1].index('test-image') + 1:]
    assert second[second.index('--max-budget-usd') + 1] == '2.00'
    assert len(calls) == 2


def test_api_failure_on_an_untouched_tree_reruns_that_stage_once_on_codex(tmp_path, monkeypatch, sandbox):
    script, calls = sandbox
    script({'events': [API_ERROR], 'exit': 1}, {'events': [CODEX], 'edit': {'feature.txt': 'x'}})
    worker = make_worker(tmp_path)
    worker.process(feature(tmp_path, monkeypatch, worker))
    assert [c[c.index('test-image') + 1] for c in calls[:2]] == ['claude', 'codex']
    assert any('dst=/codex' in a for a in calls[1]) and not any('/claude/key' in a for a in calls[1])
    usage = posts(worker, '/usage')
    assert [u.get('provider') for u in usage] == ['claude', None]
    assert 'proposal' in posts(worker, '/finish')[0]


@pytest.mark.parametrize('codex', [True, False])
def test_api_failure_after_edits_or_without_codex_never_reruns(tmp_path, monkeypatch, sandbox, codex):
    script, calls = sandbox
    script({'events': [API_ERROR], 'exit': 1, 'edit': {'half-done.txt': 'x'} if codex else {}})
    worker = make_worker(tmp_path, codex=codex)
    feature(tmp_path, monkeypatch, worker)
    assert worker.tick() is True
    assert len(calls) == 1
    finish = posts(worker, '/finish')[0]
    assert (finish.get('outcome') == 'needs_attention') is codex
    assert ('after editing files' in finish['error']) is codex


def test_non_api_claude_failure_is_not_handed_to_codex(tmp_path, monkeypatch, sandbox):
    script, calls = sandbox
    script({'events': [result(0.1, subtype='error_max_turns', is_error=True)], 'exit': 1})
    worker = make_worker(tmp_path)
    feature(tmp_path, monkeypatch, worker)
    worker.tick()
    assert len(calls) == 1 and posts(worker, '/finish')[0].get('outcome') is None


@pytest.mark.parametrize('phase,readonly', [('verify', True), ('repair-review', True), ('reproduce', False)])
def test_repair_stages_on_claude_use_schema_stage_tools_and_record_the_answering_model(
        tmp_path, sandbox, phase, readonly):
    script, calls = sandbox
    decision = {'decision': 'allow', 'reason': 'The documented contract is violated.'}
    script({'events': [result(0.4, structured_output=decision, result=json.dumps(decision))]})
    worker = make_worker(tmp_path)
    job = {'id': 'a' * 12, 'lease': 'lease', 'base_sha': 'b' * 40, 'repair': {'request_ref': 'c' * 64}}
    root = tmp_path / 'checkout'
    root.mkdir()
    verdict = RepairWork(worker, job, root, tmp_path).infer(phase, 'Check this repair', readonly=readonly)
    assert verdict == {**decision, 'model': 'claude-opus-5-5', 'effort': REVIEW_EFFORT}
    args = calls[0]
    command = args[args.index('test-image') + 1:]
    assert command[command.index('--tools') + 1] == ('Read,Glob,Grep' if readonly else 'Read,Edit,Write,Glob,Grep')
    assert command[command.index('--effort') + 1] == 'xhigh'
    assert command[command.index('--max-budget-usd') + 1] == '2.00'
    assert json.loads(command[command.index('--json-schema') + 1])['required'] == ['decision', 'reason']
    assert (f'type=bind,src={root},dst=/workspace,readonly' in args) is readonly
    assert not any('repair-schema' in a for a in args)
    [usage] = posts(worker, '/usage')
    assert usage['provider'] == 'claude' and usage['cost_micros'] == 400_000


@pytest.mark.parametrize('model,approved', [(REVIEW_MODEL, True), ('claude-opus-5-5', True),
                                            ('claude-haiku-5-5', False)])
def test_old_and_claude_evidence_validate_other_reviewers_do_not(store, model, approved):
    job = queued(store)
    candidate = proposal(job)
    for stage in ('verification', 'review'):
        candidate['repair_verification'][stage]['model'] = model
    store.finish_build(job['id'], job['lease'], candidate)
    assert (store.get(job['id'])['state'] == 'approved') is approved


def test_usage_reads_the_claude_result_event():
    usage = parse_usage([json.dumps({'type': 'assistant'}), json.dumps(result())])
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.turns) == (100, 70, 5, 3)
    assert parse_usage([json.dumps({'type': 'result', 'usage': {'output_tokens': 1}})]).input_tokens is None
