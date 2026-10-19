import json
from unittest.mock import Mock

import pytest

from gene.worker import SandboxFailure, Worker


@pytest.mark.parametrize('failures', [1, 3])
def test_failed_checks_are_repaired_before_publication_and_every_attempt_is_counted(tmp_path, monkeypatch, failures):
    worker = Worker.__new__(Worker)
    worker.root, worker.auth = tmp_path, tmp_path / 'private-auth'
    worker.repo, worker.image = 'HumanMint/gene', 'test-image'
    worker.api = Mock()
    operations, prompts = [], []

    def git(root, *args):
        operations.append(args)
        if args[0] == 'clone':
            (tmp_path / 'job/checkout').mkdir()
        return 'b' * 40 if args[0] == 'rev-parse' else ''

    def sandbox(root, name, image, command, **kwargs):
        if kwargs.get('auth'):
            prompts.append(kwargs['prompt'])
            kwargs['events'].write_text(json.dumps({'type': 'turn.completed', 'usage': {
                'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 10}}))
        elif 'pytest' in command and len(prompts) <= failures:
            raise SandboxFailure('A meaningful feature test failed')

    monkeypatch.setattr('gene.worker.git', git)
    monkeypatch.setattr('gene.worker.run_sandbox', sandbox)
    monkeypatch.setattr('gene.worker.validate_checkout', lambda root: ['cogs/feature.py'])
    monkeypatch.setattr('gene.worker.os.geteuid', lambda: 1000)
    job = {'id': 'job', 'lease': 'lease', 'model': 'gpt-6-astra', 'effort': 'medium',
           'base_sha': 'a' * 40, 'body': 'Add a feature', 'source_url': '', 'memories': []}
    if failures == 3:
        with pytest.raises(SandboxFailure):
            worker.process(job)
        assert not any(args[0] == 'push' for args in operations)
        assert not any(call.args[0].endswith('/finish') for call in worker.api.post.call_args_list)
    else:
        worker.process(job)
        assert any(args[0] == 'push' for args in operations)
        assert worker.api.post.call_args.args[1]['proposal']['tests_passed']
    usage = [call.args[1] for call in worker.api.post.call_args_list if call.args[0].endswith('/usage')]
    assert len(usage) == len(prompts) == min(failures + 1, 3)
    assert len({item['attempt_id'] for item in usage}) == len(usage)
    assert all(item['counters']['input_tokens'] == 100 for item in usage)
    assert 'meaningful feature test failed' in prompts[1]
    assert 'untrusted test output' in prompts[1]


def test_no_change_preserves_agent_explanation_without_publication(tmp_path, monkeypatch):
    from gene.policy import NoFeatureChanges
    worker = Worker.__new__(Worker)
    worker.root, worker.auth = tmp_path, tmp_path / 'auth'
    worker.repo, worker.image, worker.api = 'HumanMint/gene', 'image', Mock()
    git = Mock()
    monkeypatch.setattr('gene.worker.git', git)
    monkeypatch.setattr('gene.worker.os.geteuid', lambda: 1000)
    def sandbox(*args, **kwargs):
        kwargs['events'].write_text(json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': 'History import needs a maintainer change. No code was changed.'}}))
    monkeypatch.setattr('gene.worker.run_sandbox', sandbox)
    monkeypatch.setattr('gene.worker.validate_checkout', Mock(side_effect=NoFeatureChanges))
    worker.process({'id': 'job', 'lease': 'lease', 'model': 'gpt-6.1-sol', 'effort': 'high',
                    'base_sha': 'a' * 40, 'body': 'Import history', 'source_url': '', 'memories': []})
    finish = worker.api.post.call_args.args[1]
    assert finish['outcome'] == 'needs_attention'
    assert 'maintainer change' in finish['error']
    assert 'proposal' not in finish
    assert not any(call.args[1] == 'push' for call in git.call_args_list)
