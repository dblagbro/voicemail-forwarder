"""Durable, non-overwriting publication of the voicemail configuration document.

The old directory entry is retained beside the canonical file. Publication uses
Linux RENAME_NOREPLACE, so an intervening editor's file is never overwritten.
A crash can leave the canonical pathname briefly absent; recovery recognizes
the retained original and completes publication without restoring application data.
"""

import ctypes
import errno
import hashlib
import os
import stat
from pathlib import Path


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


def move_absent(source, target):
    """Atomic same-filesystem move that must not replace any existing entry."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ConfigurationConflict("A publication path is already occupied")
        raise OSError(error, os.strerror(error))
    sync_directory(Path(target).parent)


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


class Publication:
    def __init__(self, canonical, original, desired, token, mode, uid, gid):
        self.canonical = Path(canonical)
        if self.canonical.parent.resolve() != self.canonical.parent:
            raise ValueError("Canonical parent must be a real absolute directory")
        if not token or not all(
            c.isascii() and (c.isalnum() or c == "-") for c in token
        ):
            raise ValueError("Invalid publication token")
        self.original, self.desired = original, desired
        if original == desired:
            raise ValueError("Original and desired documents must differ")
        self.previous = self.canonical.with_name(
            "." + self.canonical.name + ".voicemail-" + token + ".previous"
        )
        self.pending = self.canonical.with_name(
            "." + self.canonical.name + ".voicemail-" + token + ".pending"
        )
        self.mode, self.owner = mode, (uid, gid)

    def _read(self, path):
        try:
            return read_regular(path)[0]
        except FileNotFoundError:
            return None

    def preflight(self):
        if self._read(self.canonical) != self.original:
            raise ConfigurationConflict(
                "Canonical configuration changed before publication"
            )
        if (
            self._read(self.previous) is not None
            or self._read(self.pending) is not None
        ):
            raise ConfigurationConflict("Publication generation already exists")

    def apply(self, hook=None):
        hook = hook or (lambda point: None)
        current = self._read(self.canonical)
        previous = self._read(self.previous)
        pending = self._read(self.pending)
        if previous not in (None, self.original) or pending not in (None, self.desired):
            raise ConfigurationConflict("Retained publication files changed")
        if current == self.desired:
            if previous != self.original:
                raise ConfigurationConflict(
                    "Published configuration has no retained original"
                )
            return
        if current not in (None, self.original):
            raise ConfigurationConflict(
                "Concurrent canonical edit retained without replacement"
            )
        if current is None and previous != self.original:
            raise ConfigurationConflict(
                "Missing canonical document and retained original"
            )
        if current == self.original and previous is not None:
            raise ConfigurationConflict("Ambiguous original document ownership")
        if pending is None:
            write_new(self.pending, self.desired, self.mode, self.owner)
        hook("candidate_file_ready")
        if current == self.original:
            move_absent(self.canonical, self.previous)
            # Preserve the displaced document even if an editor changed it just
            # before the rename. Never replace that edit with prepared bytes.
            displaced = self._read(self.previous)
            if displaced != self.original:
                try:
                    move_absent(self.previous, self.canonical)
                except ConfigurationConflict:
                    pass
                raise ConfigurationConflict("Concurrent displaced edit preserved")
            os.chmod(self.previous, 0o600)
            sync_directory(self.previous.parent)
        hook("original_retained")
        move_absent(self.pending, self.canonical)
        hook("candidate_published")
        if (
            self._read(self.canonical) != self.desired
            or self._read(self.previous) != self.original
        ):
            raise ConfigurationConflict("Configuration changed during publication")


def copy_verified(source, target):
    """Create a private independent copy; never replace an existing backup."""
    data, _ = read_regular(source)
    write_new(target, data)
    restored, _ = read_regular(target)
    if digest(restored) != digest(data) or read_regular(source)[0] != data:
        raise ConfigurationConflict("Backup verification failed or source changed")
    return {"sha256": digest(data), "bytes": len(data)}
