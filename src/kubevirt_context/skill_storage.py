"""Ownership manifests for skills copied into an agent's skill directory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import KvctxError
from .storage import atomic_write

SKILL_MANIFEST = ".kvctx-skill.json"


def skills_destination(dest: str | None) -> Path:
    root = Path(dest).expanduser().resolve() if dest else (Path.home() / ".agents" / "skills").resolve()
    home = Path.home().resolve()
    if root == home or root in home.parents:
        raise KvctxError(f"refusing to manage skills in protected directory: {root}")
    return root


def _entries(directory: Path) -> dict[str, str | None]:
    """Fingerprint the whole tree, including empty directories and extra files."""
    entries: dict[str, str | None] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if relative == SKILL_MANIFEST:
            continue
        if path.is_symlink():
            raise KvctxError(f"skill contains a symlink: {path}")
        if path.is_dir():
            entries[relative] = None
        elif path.is_file():
            entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            raise KvctxError(f"skill contains a non-regular file: {path}")
    return entries


def write_skill_manifest(directory: Path, name: str) -> None:
    manifest = {
        "application": "kubevirt-context",
        "version": 1,
        "skill": name,
        "entries": _entries(directory),
    }
    atomic_write(directory / SKILL_MANIFEST, json.dumps(manifest, indent=2) + "\n")


def owned_skills(root: Path, force: bool) -> list[Path]:
    """Preflight all owned skills before deleting any; never infer ownership by name."""
    if not root.exists():
        return []
    if not root.is_dir():
        raise KvctxError(f"not a skills directory: {root}")
    owned = []
    modified = []
    for directory in sorted(root.iterdir()):
        # A replaced installation must not redirect inspection or deletion.
        if directory.is_symlink():
            if directory.name.startswith("kubevirt-"):
                raise KvctxError(f"refusing to uninstall a symlink: {directory}")
            continue
        if not directory.is_dir():
            continue
        marker = directory / SKILL_MANIFEST
        if not marker.exists() and not marker.is_symlink():
            continue
        if marker.is_symlink() or not marker.is_file():
            raise KvctxError(f"invalid skill ownership manifest: {marker}")
        try:
            manifest = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise KvctxError(f"invalid skill ownership manifest: {marker}") from None
        if not (
            isinstance(manifest, dict)
            and manifest.get("application") == "kubevirt-context"
            and manifest.get("version") == 1
            and manifest.get("skill") == directory.name
            and directory.name.startswith("kubevirt-")
            and isinstance(manifest.get("entries"), dict)
            and isinstance(manifest["entries"].get("SKILL.md"), str)
        ):
            raise KvctxError(f"invalid skill ownership manifest: {marker}")
        if not force:
            try:
                unchanged = _entries(directory) == manifest["entries"]
            except KvctxError:
                unchanged = False
            if not unchanged:
                modified.append(directory.name)
        owned.append(directory)
    if modified:
        raise KvctxError(
            f"locally modified skills: {', '.join(modified)}; "
            "nothing removed (use --force to remove modified installations)"
        )
    return owned
