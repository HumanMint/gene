"""Capture operator calling configuration once; recovery reads only protected state."""

import json
import os
import stat
from pathlib import Path


def trusted_directory(path):
    path = Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in ('.',) + path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('Calling snapshot requires trusted directories')
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(directory, name, modes):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) not in modes:
            raise ValueError('Invalid calling snapshot or overlay metadata')
        content = source.read(1048577)
        if len(content) > 1048576:
            raise ValueError('Calling overlay exceeds its size bound')
        return content


def snapshot_overlay(root, state, *, capture=False):
    state = Path(state).absolute()
    directory = trusted_directory(state)
    try:
        try:
            record = json.loads(read_file(directory, 'calling.snapshot.json', {0o600}))
        except FileNotFoundError:
            if not capture:
                raise ValueError('Calling snapshot is missing; refuse to recapture during recovery') from None
            operator = trusted_directory(root)
            try:
                try:
                    content = read_file(operator, 'compose.calling.yml', {0o600, 0o644})
                except FileNotFoundError:
                    content = None
            finally:
                os.close(operator)
            if content is not None:
                write_file(directory, 'calling.snapshot.yml', content)
            record = {'overlay': content is not None}
            write_file(directory, 'calling.snapshot.json', json.dumps(record).encode())
            os.fsync(directory)
        if type(record) is not dict or set(record) != {'overlay'} or type(record['overlay']) is not bool:
            raise ValueError('Invalid calling snapshot record')
        if record['overlay']:
            read_file(directory, 'calling.snapshot.yml', {0o600})
            return state / 'calling.snapshot.yml'
        return None
    finally:
        os.close(directory)


def write_file(directory, name, content):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, 'wb') as output:
        if os.fstat(output.fileno()).st_uid != 0:
            raise ValueError('Calling snapshots must be root-owned')
        os.fchmod(output.fileno(), 0o600)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
