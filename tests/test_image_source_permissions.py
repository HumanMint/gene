"""Exercise the Dockerfile's source-mode repair without building or credentials."""

import os
import shlex
import subprocess
from pathlib import Path


def test_private_build_context_keeps_code_readable_and_state_private(tmp_path):
    app = tmp_path / 'app'
    sources = ('src/gene/__init__.py', 'cogs/gene/__init__.py', 'tests/test_api.py')
    private = ('auth/session.json', 'state/controller.sqlite3')
    for name in (*sources, *private):
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic fixture\n')
        path.chmod(0o600)
    executable = app / 'src/gene/tool'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    for path in app.rglob('*'):
        if path.is_dir():
            path.chmod(0o700)
    owners = {path: path.stat().st_uid for path in app.rglob('*')}

    # Execute the actual scoped mode-repair recipe against a restrictive context.
    # A missing repair leaves the assertion below failing on the old Dockerfile.
    recipe = (Path(__file__).parents[1] / 'ops/Dockerfile').read_text()
    for line in recipe.splitlines():
        if line.startswith('RUN chmod '):
            arguments = shlex.split(line.removeprefix('RUN '))
            arguments = [str(app / value.removeprefix('/app/')) if value.startswith('/app/')
                         else value for value in arguments]
            subprocess.run(arguments, check=True, env={'PATH': os.defpath})

    for name in sources:
        path = app / name
        assert path.stat().st_mode & 0o444 == 0o444
        for directory in path.parents:
            if directory == app:
                break
            assert directory.stat().st_mode & 0o111 == 0o111
    assert executable.stat().st_mode & 0o777 == 0o755
    assert all(path.stat().st_uid == owner for path, owner in owners.items())
    for name in private:
        assert (app / name).stat().st_mode & 0o777 == 0o600
        assert (app / name).parent.stat().st_mode & 0o777 == 0o700
