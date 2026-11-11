"""Private, non-overwriting snapshot files; symlinks/hardlinks are rejected."""

import hashlib
import os
import stat


class ConfigurationConflict(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ConfigurationConflict(
                "Configuration is not a singly-linked regular file"
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read()
        return data, metadata
    finally:
        os.close(fd)


def write_new(path, data, mode=0o600, owner=None):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        if owner is not None:
            os.fchown(fd, *owner)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(path.parent)


def copy_verified(source, target):
    """Create a private independent copy; never replace an existing backup."""
    data, _ = read_regular(source)
    write_new(target, data)
    restored, _ = read_regular(target)
    if digest(restored) != digest(data) or read_regular(source)[0] != data:
        raise ConfigurationConflict("Backup verification failed or source changed")
    return {"sha256": digest(data), "bytes": len(data)}
