from __future__ import annotations

from dataclasses import dataclass, field


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
class AnalysisRunResult:
    status: str
    findings: list[FindingSeed] = field(default_factory=list)
    summary: dict[str, object] = field(default_factory=dict)
