import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from gene.policy import git
from gene.repair_policy import REVIEW_EFFORT, REVIEW_MODEL, digest, validate_repair_checkout
from gene.repair_worker import RepairWork
from gene.worker import SandboxFailure, Worker, run_sandbox
from tests.test_repairs import queued

BROKEN='''def score_votes(up, down, author_id):
    """Self-votes do not count; opposite reactions cancel each other."""
    return len(up - down) - len(down - up)
'''
FIXED=BROKEN.replace('len(up - down) - len(down - up)',
                      'len(up - down - {author_id}) - len(down - up - {author_id})')
REGRESSION='''from cogs.numbers.scoring import score_votes


def test_self_votes_do_not_count():
    assert score_votes({1, 2}, set(), 1) == 1
'''


def fixture_repo(path):
    (path/'cogs/numbers').mkdir(parents=True)
    (path/'tests/features').mkdir(parents=True)
    (path/'cogs/numbers/scoring.py').write_text(BROKEN)
    (path/'tests/features/test_existing.py').write_text(
        'from cogs.numbers.scoring import score_votes\n\ndef test_other_votes():\n    assert score_votes({2}, set(), 1) == 1\n')
    git(path,'init','-q')
    git(path,'add','.')
    git(path,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','Existing vote counter')
    return git(path,'rev-parse','HEAD')


@pytest.mark.parametrize('failure',[None,'new_feature','unreproduced','verifier_denied','frozen_test','review_denied'])
def test_worker_checks_real_failure_and_fix_before_controller_auto_approval(tmp_path,store,monkeypatch,failure):
    original=tmp_path/'base'
    base=fixture_repo(original)
    with store.connection() as db:
        db.execute('UPDATE settings SET value=? WHERE key="active_sha"',(base,))
    job=queued(store)
    worker=Worker.__new__(Worker)
    worker.root=tmp_path/'jobs'
    worker.root.mkdir()
    worker.auth=tmp_path/'auth'
    worker.auth.mkdir()
    worker.image,worker.repo='fixture-image','HumanMint/gene'
    calls=[]
    def api(path,data):
        calls.append((path,data))
        if path.endswith('/heartbeat'):
            store.heartbeat(job['id'],data['lease'],data.get('progress',''))
        elif path.endswith('/usage'):
            store.add_usage(job['id'],data['lease'],data['attempt_id'],data['model'],data['counters'])
        elif path.endswith('/finish'):
            store.finish_build(job['id'],data['lease'],data.get('proposal'),data.get('error',''))
    worker.api=Mock(post=api)
    operations=[]
    def git_wrapper(root,*args,**kwargs):
        operations.append(args)
        if args[0]=='clone':
            shutil.copytree(original,Path(args[-1]))
            return ''
        if args[0]=='push':
            return ''
        return git(root,*args,**kwargs)
    monkeypatch.setattr('gene.worker.git',git_wrapper)
    monkeypatch.setattr('gene.worker.os.geteuid',lambda:1000)
    phases=[]
    def sandbox(root,name,image,command,**kwargs):
        phases.append(name)
        if kwargs.get('auth'):
            if 'reproduce' in name:
                target=root/('tests/features/test_repair_'+job['id']+'.py')
                target.write_text(REGRESSION if failure!='unreproduced' else 'def test_empty():\n    assert True\n')
                report={'decision':'allow','reason':'An existing vote-counting contract is violated.'}
            elif 'repair-review' in name:
                assert kwargs['readonly'] is True and kwargs['schema'].is_file()
                report={'decision':'manual' if failure=='review_denied' else 'allow',
                        'reason':'The precise diff only repairs the documented self-vote rule.'}
            elif 'verify' in name:
                assert kwargs['readonly'] is True
                assert (root/'cogs/numbers/scoring.py').read_text()==BROKEN
                report={'decision':'manual' if failure=='verifier_denied' else 'allow',
                        'reason':'The original docstring excludes the author vote but the function counts it.'}
            else:
                (root/'cogs/numbers/scoring.py').write_text(FIXED)
                if failure=='new_feature':
                    with (root/'cogs/numbers/scoring.py').open('a') as f:
                        f.write('\ndef new_feature():\n    return 123\n')
                if failure=='frozen_test':
                    (root/('tests/features/test_repair_'+job['id']+'.py')).write_text('def test_fake():\n    assert True\n')
                report='Fixed the documented vote-counting bug.'
            kwargs['events'].write_text(json.dumps({'type':'item.completed','item':{
                'type':'agent_message','text':json.dumps(report) if isinstance(report,dict) else report}})+'\n'+
                json.dumps({'type':'turn.completed','usage':{'input_tokens':20,'output_tokens':10}}))
            return subprocess.CompletedProcess(command,0,'','')
        if command[0]=='ruff':
            return subprocess.CompletedProcess(command,0,'','')
        # These are tiny, hand-authored fixture tests. The Linux integration run is networkless.
        result=subprocess.run([sys.executable,*command[1:]],cwd=root,capture_output=True,text=True,
            env={**os.environ,'PYTHONPATH':str(root),'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'})
        if result.returncode and not kwargs.get('allow_failure'):
            raise SandboxFailure(result.stdout+result.stderr)
        return result
    monkeypatch.setattr('gene.worker.run_sandbox',sandbox)
    if failure in ('new_feature','unreproduced','verifier_denied','frozen_test'):
        with pytest.raises(ValueError):
            worker.process(job)
        assert not any(op[0]=='push' for op in operations)
        if failure in ('unreproduced','verifier_denied'):
            assert not any(name.startswith('gene-code-') for name in phases)
    else:
        worker.process(job)
        saved=store.get(job['id'])
        assert saved['state']==('review' if failure=='review_denied' else 'approved')
        evidence=saved['proposal']['repair_verification']
        assert evidence['base_failed'] and evidence['candidate_passed']
        assert evidence['regression_hash']==digest(REGRESSION.encode())
        assert phases.index('gene-verify-'+job['id'])<phases.index('gene-code-'+job['id'])
        assert any(op[0]=='push' for op in operations)
        assert (store.claim_deploy() is None)==(failure=='review_denied')
    attempts=[data for path,data in calls if path.endswith('/usage')]
    assert len(attempts)==len([name for name in phases if any(word in name for word in ('reproduce-','verify-','code-','repair-review-'))])


def test_sandbox_keeps_review_checkout_read_only_and_tests_without_credentials(tmp_path,monkeypatch):
    commands=[]
    def run(args,**kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args,0,'','')
    monkeypatch.setattr('gene.worker.subprocess.run',run)
    run_sandbox(tmp_path,'review','image',['codex','exec'],auth=tmp_path/'auth',readonly=True)
    review=commands[0]
    assert f'type=bind,src={tmp_path},dst=/workspace,readonly' in review
    run_sandbox(tmp_path,'tests','image',['python','-m','pytest'])
    test=commands[2]
    assert '--network=none' in test and all('/codex' not in str(v) for v in test)


@pytest.mark.parametrize('change',[
    '\nimport os\n',
    '\ndef new_command():\n    return 1\n',
    '\nclass NewFeature:\n    pass\n',
])
def test_automatic_scope_rejects_new_imports_functions_or_classes(tmp_path,change):
    base=fixture_repo(tmp_path)
    test='tests/features/test_repair_'+'a'*12+'.py'
    (tmp_path/test).write_text(REGRESSION)
    (tmp_path/'cogs/numbers/scoring.py').write_text(FIXED+change)
    with pytest.raises(ValueError,match='protected'):
        validate_repair_checkout(tmp_path,base,test,digest(REGRESSION.encode()))


def test_automatic_scope_rejects_ignored_helpers_and_changed_permissions(tmp_path):
    base=fixture_repo(tmp_path)
    test='tests/features/test_repair_'+'a'*12+'.py'
    (tmp_path/test).write_text(REGRESSION)
    (tmp_path/'cogs/numbers/scoring.py').write_text(FIXED)
    (tmp_path/'.git/info/exclude').write_text('hidden.py\n')
    (tmp_path/'hidden.py').write_text('print("hidden helper")')
    with pytest.raises(ValueError,match='hidden'):
        validate_repair_checkout(tmp_path,base,test,digest(REGRESSION.encode()))


def test_independent_verifier_uses_fresh_sol_xhigh_readonly_and_counts_failed_inference(tmp_path,monkeypatch):
    worker=Mock(auth=tmp_path/'auth',image='image')
    job={'id':'a'*12,'lease':'lease','base_sha':'b'*40,'repair':{'request_ref':'c'*64}}
    repair=RepairWork(worker,job,tmp_path,tmp_path)
    def run(*args,**kwargs):
        command=args[3]
        assert '--ephemeral' in command and REVIEW_MODEL in command
        assert 'model_reasoning_effort="'+REVIEW_EFFORT+'"' in command
        assert kwargs['readonly'] is True
        raise SandboxFailure('Model failed')
    monkeypatch.setattr('gene.worker.run_sandbox',run)
    with pytest.raises(SandboxFailure):
        repair.infer('verify','Check this repair')
    assert worker.api.post.call_args.args[0].endswith('/usage')
