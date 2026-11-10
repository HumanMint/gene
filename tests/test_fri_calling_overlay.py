"""Release snapshots survive operator mutations and keep recovery launch arguments fixed."""

import os
from pathlib import Path

import pytest

from gene.fri_overlay import snapshot_overlay
from tests.test_fri_split import installation, trusted_test_files  # noqa: F401


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("mutation", ["missing", "dangling", "mode", "replaced"])
def test_operator_mutation_after_stop_cannot_strand_recovery(tmp_path, initial, mutation):
    release, _, calls = installation(tmp_path)
    operator = release.root / "compose.calling.yml"
    if initial:
        operator.write_text("services: {runtime: {stop_grace_period: 30s}}")
        operator.chmod(0o600)
    run = release.run

    def mutate(args):
        result = run(args)
        if args[:2] == ["docker", "compose"] and args[-2:] == ["stop", "runtime"]:
            operator.unlink(missing_ok=True)
            if mutation == "dangling":
                operator.symlink_to(tmp_path / "missing")
            elif mutation in ("mode", "replaced"):
                operator.write_text("changed operator data")
                operator.chmod(0o666 if mutation == "mode" else 0o600)
        return result

    release.run = mutate
    release.healthy = lambda sha, *args: sha == "b" * 40
    with pytest.raises(RuntimeError, match="health"):
        release.deploy("c" * 40)
    commands = [a for a in calls if a[:2] == ["docker", "compose"]]
    assert len(commands) == 4
    files = [[a[i + 1] for i, v in enumerate(a) if v == "-f"] for a in commands]
    assert all(f == files[0] for f in files)
    assert len(files[0]) == (2 if initial else 1)
    if initial:
        snapshot = Path(files[0][1])
        assert snapshot != operator and snapshot.read_text().startswith("services:")
    assert __import__("json").loads(release.record.read_text())["sha"] == "b" * 40


@pytest.mark.parametrize(
    "damage", ["link", "directory", "mode", "parent-link", "owner", "parent-writable"]
)
def test_bad_overlay_or_parent_refuses_before_stop(tmp_path, monkeypatch, damage):
    release, _, calls = installation(tmp_path)
    operator = release.root / "compose.calling.yml"
    operator.write_text("services: {}")
    operator.chmod(0o600)
    if damage == "link":
        operator.unlink()
        operator.symlink_to(tmp_path / "missing")
    elif damage == "directory":
        operator.unlink()
        operator.mkdir()
    elif damage == "mode":
        operator.chmod(0o666)
    elif damage == "parent-link":
        real = tmp_path / "real-fri"
        release.root.rename(real)
        release.root.symlink_to(real, target_is_directory=True)
    else:
        target = operator.stat().st_ino if damage == "owner" else release.root.stat().st_ino
        original = os.fstat

        def changed(fd):
            info = original(fd)
            if info.st_ino == target:
                fields = list(info)
                if damage == "owner":
                    fields[4] = 1
                else:
                    fields[0] |= 0o002
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises((OSError, ValueError)):
        release.deploy("c" * 40)
    assert not any(a[:2] == ["docker", "compose"] for a in calls)


def test_descriptor_read_snapshot_is_not_operator_path(tmp_path):
    root, state = tmp_path / "root", tmp_path / "state"
    root.mkdir()
    state.mkdir(mode=0o700)
    operator = root / "compose.calling.yml"
    operator.write_text("original")
    operator.chmod(0o644)
    frozen = snapshot_overlay(root, state, capture=True)
    operator.write_text("replacement")
    assert snapshot_overlay(root, state) == frozen
    assert frozen.read_text() == "original"
    assert frozen.stat().st_mode & 0o777 == 0o600


def test_bootstrap_and_module_are_identical():
    shell = Path("ops/maintainer/batch-release.sh").read_text()
    module = Path("src/gene/fri_overlay.py").read_text()
    assert module in shell


def test_no_overlay_exact_argv(tmp_path):
    release, _, calls = installation(tmp_path)
    release.deploy("c" * 40)
    for a in calls:
        if a[:2] == ["docker", "compose"]:
            assert a[:9] == [
                "docker",
                "compose",
                "--project-name",
                "fri",
                "--env-file",
                str(release.env),
                "-f",
                str(release.compose_file),
                a[8],
            ]
            assert a.count("-f") == 1
