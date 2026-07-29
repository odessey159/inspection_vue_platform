"""Project runtime paths, JSON helpers, and cross-platform path resolution."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..settings import PROJECTS_DIR


def ensure_project_dirs(project_id: int) -> dict[str, Path]:
    root = PROJECTS_DIR / str(project_id)
    artifacts = root / "artifacts"
    manifests = root / "manifests"
    scenes = root / "scenes"
    summaries = root / "summaries"
    extracted = root / "extracted"
    evidence_frames = root / "evidence_frames"
    for path in (root, artifacts, manifests, scenes, summaries, extracted, evidence_frames):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "root": root,
        "artifacts": artifacts,
        "manifests": manifests,
        "scenes": scenes,
        "summaries": summaries,
        "extracted": extracted,
        "evidence_frames": evidence_frames,
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_windows_absolute(path_text: str) -> bool:
    """True for ``C:\\...`` / ``C:/...`` even when running on POSIX."""
    if len(path_text) >= 3 and path_text[0].isalpha() and path_text[1] == ":" and path_text[2] in "\\/":
        return True
    return path_text.startswith("\\\\")


def _posix_text(path_text: str) -> str:
    return path_text.replace("\\", "/")


def _project_subdir_relative(path_text: str) -> str | None:
    """
    Recover ``scenes/scene.json``-style tails from foreign absolute paths.

    Host Windows DB rows often store ``D:\\...\\projects\\15\\scenes\\scene.json``;
    inside Linux/Docker that string is not a native absolute path.
    """
    normalized = _posix_text(path_text)
    markers = (
        "/scenes/",
        "/artifacts/",
        "/manifests/",
        "/summaries/",
        "/extracted/",
        "/evidence_frames/",
    )
    lowered = normalized.lower()
    for marker in markers:
        index = lowered.rfind(marker)
        if index >= 0:
            return normalized[index + 1 :]
    return None


def to_project_relative_path(artifacts_dir: str | Path, path_value: str | Path) -> str:
    """Store portable relative paths (POSIX separators) under the project root."""
    root = Path(artifacts_dir)
    absolute = Path(path_value)
    try:
        relative = absolute.resolve().relative_to(root.resolve())
        return _posix_text(str(relative))
    except Exception:
        recovered = _project_subdir_relative(str(path_value))
        if recovered:
            return recovered
        return _posix_text(str(path_value))


def resolve_project_path(artifacts_dir: str | Path, path_value: str | Path | None, default_relative: str) -> Path:
    """
    Resolve a project file path portably across Windows host ↔ Linux/Docker.

    Prefer an existing file under ``artifacts_dir`` (including remapped Windows
    absolute DB values), then a native absolute path, then ``default_relative``.
    """
    root = Path(artifacts_dir)
    default_path = root / default_relative
    if not path_value:
        return default_path

    raw = str(path_value).strip()
    if not raw:
        return default_path

    candidate = Path(raw)
    windows_abs = _is_windows_absolute(raw)

    # Portable relative values already stored as scenes/... or artifacts/...
    if not candidate.is_absolute() and not windows_abs:
        return root / candidate

    recovered = _project_subdir_relative(raw)
    remapped = root / recovered if recovered else None
    if remapped is not None and remapped.exists():
        return remapped

    # Native absolute path present on this OS (typical local Windows/Linux run).
    if candidate.is_absolute() and candidate.exists():
        return candidate

    if remapped is not None:
        if default_path.exists() and remapped != default_path and not remapped.exists():
            return default_path
        return remapped

    if default_path.exists():
        return default_path

    if candidate.is_absolute() or windows_abs:
        return candidate
    return root / candidate


def path_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def remove_paths(paths: list[Path]) -> int:
    removed_bytes = 0
    for path in paths:
        if not path.exists():
            continue
        removed_bytes += path_size_bytes(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return removed_bytes
