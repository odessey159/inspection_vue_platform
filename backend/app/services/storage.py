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


def resolve_project_path(artifacts_dir: str | Path, path_value: str | Path | None, default_relative: str) -> Path:
    root = Path(artifacts_dir)
    if path_value:
        candidate = Path(path_value)
        if candidate.is_absolute():
            return candidate
        return root / candidate
    return root / default_relative


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
