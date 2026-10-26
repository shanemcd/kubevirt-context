"""Filesystem ownership, atomic writes, and exclusive workspace operations."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile

from .errors import KvctxError

MARKER = ".kvctx.json"
_MARKER_CONTENT = {"application": "kubevirt-context", "version": 1}


def atomic_write(path: Path, content: str) -> None:
    """Replace a file using a unique, private temporary file on the same device.

    Never follow a destination symlink, or loosen permissions on existing
    configuration (which may contain credentials for other MCP servers).
    """
    if path.is_symlink():
        raise KvctxError(f"refusing to replace a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def is_workspace(home: Path) -> bool:
    """Only an explicit ownership marker identifies a workspace."""
    marker = home / MARKER
    if marker.is_file() and not marker.is_symlink():
        try:
            return json.loads(marker.read_text(encoding="utf-8")) == _MARKER_CONTENT
        except (OSError, ValueError):
            return False
    return False


def validate_home(home: Path) -> None:
    """Reject roots, home and its ancestors, and existing source checkouts."""
    user_home = Path.home().resolve()
    resolved = home.resolve()
    if resolved == Path("/") or resolved == user_home or resolved in user_home.parents:
        raise KvctxError(f"refusing to use a protected directory as the workspace: {home}")
    for directory in (resolved, *resolved.parents):
        git = directory / ".git"
        if git.exists() or git.is_symlink():
            raise KvctxError(f"refusing to use a source checkout as the workspace: {home}")


def ensure_workspace(home: Path) -> None:
    """Claim an empty directory or validate an explicitly marked workspace."""
    validate_home(home)
    if home.exists() and not is_workspace(home):
        if not home.is_dir() or any(home.iterdir()):
            raise KvctxError(f"not a kvctx workspace (directory must be empty): {home}")
    home.mkdir(parents=True, exist_ok=True)
    for relative in ("sources", "sources/kubevirt", "sources/staging", "sources/repositories.tsv", ".codegraph"):
        if (home / relative).is_symlink():
            raise KvctxError(f"refusing to write through a workspace symlink: {home / relative}")
    if not (home / MARKER).exists():
        atomic_write(home / MARKER, json.dumps(_MARKER_CONTENT) + "\n")


@contextmanager
def workspace_lock(home: Path):
    """Serialize mutations, including clean, without creating the workspace.

    The sibling lock survives clean. Do not unlink it: another process may be
    holding the same inode while a third process opens the path.
    """
    validate_home(home)
    path = home.parent / f".{home.name}.kvctx.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        raise KvctxError(f"cannot open workspace lock: {path}") from None
    with os.fdopen(fd, "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise KvctxError(f"another kvctx command is using {home}; try again when it finishes") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
