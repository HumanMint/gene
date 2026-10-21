"""Trusted boundaries for the owner's standing authorization of existing-feature repairs."""

import ast
import hashlib
import re

from .policy import git, validate_checkout, validate_commit
from .reviews import request_reference

POLICY = 'existing-features-v1'
ACTOR = 'policy:' + POLICY
REVIEW_MODEL, REVIEW_EFFORT = 'gpt-6.1-sol', 'xhigh'
PROTECTED = {'__init__', 'cog_check', 'interaction_check', 'authorized', 'eligible', 'api',
             'actor', 'health', 'before_health', 'red_delete_data_for_user', 'forgetme',
             'music_command', 'cog_load', 'cog_unload', 'is_private', 'key'}


def regression_path(job_id):
    if not re.fullmatch(r'[a-f0-9]{12}', job_id):
        raise ValueError('Invalid repair identifier')
    return f'tests/features/test_repair_{job_id}.py'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def authorized(job, record, proposal):
    """Only the trusted worker's complete, commit-bound evidence can use this policy."""
    evidence = proposal.get('repair_verification') or {}
    if not isinstance(evidence, dict):
        return False
    return bool(record and record['policy'] == POLICY and
        record['request_ref'] == request_reference(job) and
        record['base_sha'] == proposal.get('base_sha') and
        evidence.get('policy') == POLICY and evidence.get('sha') == proposal.get('sha') and
        evidence.get('base_sha') == record['base_sha'] and
        evidence.get('request_ref') == record['request_ref'] and
        evidence.get('regression_path') == regression_path(job['id']) and
        re.fullmatch(r'[a-f0-9]{64}', str(evidence.get('regression_hash', ''))) and
        all(evidence.get(k) is True for k in ('baseline_passed', 'base_failed', 'candidate_passed')) and
        all(isinstance(evidence.get(k), dict) and evidence[k].get('decision') == 'allow' and
            evidence[k].get('model') == REVIEW_MODEL and evidence[k].get('effort') == REVIEW_EFFORT and
            isinstance(evidence[k].get('reason'), str) and 10 <= len(evidence[k]['reason']) <= 2000
            for k in ('verification', 'review')))


def surface(source):
    """Freeze imports, globals, signatures, decorators, classes and authorization helpers.

    This is an additional conservative scope check, not a Python security sandbox.
    An independent reviewer still assesses the changed function bodies.
    """
    tree = ast.parse(source)
    calls = {ast.dump(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    imports = [ast.dump(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    definitions = [(n.name, ast.dump(n.args), [ast.dump(d) for d in n.decorator_list])
                   for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    protected = [ast.dump(n) for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in PROTECTED]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in PROTECTED:
            node.body = [ast.Pass()]
    return (ast.dump(tree), imports, definitions, protected), calls


def validate_repair_commit(root, base, sha, test_path, test_hash):
    paths = validate_commit(root, base, sha)
    return validate_sources(root, base, paths, lambda name: git(root, 'show', f'{sha}:{name}', raw=True),
                            test_path, test_hash)


def validate_repair_checkout(root, base, test_path, test_hash):
    paths = validate_checkout(root)
    if any(name != test_path for name in git(root, 'ls-files', '--others', '-z').split('\0') if name):
        raise ValueError('Automatic repairs cannot add source files or hidden test helpers')
    return validate_sources(root, base, paths, lambda name: (root/name).read_bytes(), test_path, test_hash)


def validate_sources(root, base, paths, read, test_path, test_hash):
    if test_path not in paths or len(paths) > 6:
        raise ValueError('A small repair and its frozen regression test are required')
    changed_code = False
    for name in paths:
        after_bytes = read(name)
        after = after_bytes.decode()
        if name == test_path:
            if digest(after_bytes) != test_hash:
                raise ValueError('The verified regression test changed')
            continue
        if not name.startswith('cogs/') or not name.endswith('.py') or name.endswith('/__init__.py'):
            raise ValueError('Automatic repairs only modify existing cog Python functions and their one new regression test')
        before = git(root, 'show', f'{base}:{name}')  # New/deleted source files fail closed.
        old_surface, old_calls = surface(before)
        new_surface, new_calls = surface(after)
        if old_surface != new_surface or not new_calls <= old_calls:
            raise ValueError('Repair changes protected structure, permissions or capabilities; owner review required')
        if len(before) > 100000 or len(after) > 100000:
            raise ValueError('Repair file is too large for automatic review')
        changed_code = True
    if not changed_code:
        raise ValueError('No existing feature was repaired')
    return paths
