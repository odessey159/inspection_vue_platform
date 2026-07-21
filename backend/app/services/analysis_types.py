from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FindingSeed:
    rule_id: str
    title: str
    time_start_ms: int
    time_end_ms: int
    evidence_frame_ts: list[int]
    description: str
    confidence: float
    severity: str


@dataclass
class AnalysisVideoTarget:
    path: Path
    label: str
    video_start_ts: int
    video_end_ts: int


@dataclass
class AnalysisRunResult:
    status: str
    findings: list[FindingSeed] = field(default_factory=list)
    summary: dict[str, object] = field(default_factory=dict)


def dedupe_analysis_video_targets(targets: list[AnalysisVideoTarget]) -> list[AnalysisVideoTarget]:
    deduped: list[AnalysisVideoTarget] = []
    seen: set[str] = set()
    for target in targets:
        key = str(target.path.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped
