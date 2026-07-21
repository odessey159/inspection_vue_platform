"""Map YOLO class names and aliases to canonical object names for Rule RAG."""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only in lean local envs.
    yaml = None

from ..settings import OBJECT_ALIASES_PATH

_alias_map_cache: dict[str, str] | None = None


def load_alias_map(alias_path: Path | None = None) -> dict[str, str]:
    """
    Return alias -> canonical_name mapping.
    Example: electrical_box -> 配电箱

    Result is cached at module level; pass alias_path to bypass the cache.
    """
    global _alias_map_cache
    if alias_path is None and _alias_map_cache is not None:
        return _alias_map_cache
    candidates: list[Path] = []
    if alias_path is not None:
        candidates.append(Path(alias_path))
    candidates.append(Path(OBJECT_ALIASES_PATH))
    # Fall back when Docker-style env paths are loaded on a host checkout.
    candidates.append(Path(__file__).resolve().parents[2] / "config" / "object_aliases.yaml")

    resolved: Path | None = None
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except OSError:
            continue
        if path.exists():
            resolved = path
            break
    if resolved is None:
        return {}

    data = _load_alias_yaml(resolved)
    alias_to_canonical: dict[str, str] = {}

    for canonical, aliases in data.items():
        alias_to_canonical[str(canonical).strip().lower()] = str(canonical)

        for alias in aliases or []:
            alias_to_canonical[str(alias).strip().lower()] = str(canonical)

    if alias_path is None:
        _alias_map_cache = alias_to_canonical

    return alias_to_canonical


def _load_alias_yaml(path: Path) -> dict[str, list[str]]:
    if yaml is not None:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}

    data: dict[str, list[str]] = {}
    current_key = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":") and not line.startswith("-"):
            current_key = line[:-1].strip()
            data.setdefault(current_key, [])
            continue
        if current_key and line.startswith("-"):
            alias = line[1:].strip()
            if alias:
                data[current_key].append(alias)
    return data


def normalize_object_name(name: str, alias_map: dict[str, str] | None = None) -> str:
    if not name:
        return ""

    mapping = alias_map if alias_map is not None else load_alias_map()
    key = name.strip().lower()
    return mapping.get(key, name.strip())


def normalize_yolo_classes(classes: list[str]) -> list[str]:
    alias_map = load_alias_map()

    normalized: list[str] = []
    for cls in classes:
        norm = normalize_object_name(cls, alias_map)
        if norm and norm not in normalized:
            normalized.append(norm)

    return normalized
