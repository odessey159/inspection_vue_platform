import type { FindingResponse, ProjectSummary, ZoneResponse } from "../types";

function sameNumberArray(left: number[], right: number[]): boolean {
  if (left === right) {
    return true;
  }
  if (left.length !== right.length) {
    return false;
  }
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) {
      return false;
    }
  }
  return true;
}

function sameZone(left: ZoneResponse | null, right: ZoneResponse | null): boolean {
  if (left === right) {
    return true;
  }
  if (!left || !right) {
    return false;
  }
  return (
    left.id === right.id
    && left.finding_id === right.finding_id
    && left.radius_m === right.radius_m
    && left.heading === right.heading
    && left.related_pose_ts === right.related_pose_ts
    && left.center[0] === right.center[0]
    && left.center[1] === right.center[1]
    && left.center[2] === right.center[2]
  );
}

/** Keep object identity when finding content is unchanged to avoid Vue list remounts. */
export function findingsEqual(left: FindingResponse, right: FindingResponse): boolean {
  return (
    left.id === right.id
    && left.finding_uid === right.finding_uid
    && left.rule_id === right.rule_id
    && left.title === right.title
    && left.time_start_ms === right.time_start_ms
    && left.time_end_ms === right.time_end_ms
    && left.description === right.description
    && left.confidence === right.confidence
    && left.needs_review === right.needs_review
    && left.review_status === right.review_status
    && left.reviewer_notes === right.reviewer_notes
    && left.severity === right.severity
    && left.analysis_mode === right.analysis_mode
    && left.legal_basis === right.legal_basis
    && left.hazard_desc === right.hazard_desc
    && left.category === right.category
    && left.checker_scope === right.checker_scope
    && left.visual_detectable === right.visual_detectable
    && sameNumberArray(left.evidence_frame_ts, right.evidence_frame_ts)
    && sameZone(left.zone, right.zone)
  );
}

export type FindingsMergeResult = {
  findings: FindingResponse[];
  changed: boolean;
  addedCount: number;
};

/**
 * Merge poll results into the current findings list.
 * Unchanged items keep their previous object references so list/detail stay stable.
 */
export function mergeFindingsSnapshot(
  previous: FindingResponse[],
  next: FindingResponse[],
): FindingsMergeResult {
  if (previous.length === 0) {
    return { findings: next, changed: next.length > 0, addedCount: next.length };
  }
  if (next.length === 0) {
    return { findings: next, changed: previous.length > 0, addedCount: 0 };
  }

  const previousById = new Map(previous.map((finding) => [finding.id, finding]));
  let changed = previous.length !== next.length;
  let addedCount = 0;
  const merged = next.map((finding, index) => {
    const existing = previousById.get(finding.id);
    if (!existing) {
      changed = true;
      addedCount += 1;
      return finding;
    }
    if (previous[index]?.id !== finding.id) {
      changed = true;
    }
    if (findingsEqual(existing, finding)) {
      return existing;
    }
    changed = true;
    return finding;
  });

  if (!changed) {
    return { findings: previous, changed: false, addedCount: 0 };
  }
  return { findings: merged, changed: true, addedCount };
}

/** Cheap project-list fingerprint for live poll; avoids full JSON.stringify. */
export function projectMonitorSignature(project: ProjectSummary): string {
  return [
    project.id,
    project.vehicle_id ?? "",
    project.status,
    project.findings_count,
    project.rtsp_recording_active ? 1 : 0,
    project.rtsp_stream_online ? 1 : 0,
    project.rtsp_recorded_video_url ?? "",
    project.rtsp_live_url ?? "",
    project.inspection_video_url ?? "",
    project.updated_at ?? "",
  ].join("|");
}

export function projectsMonitorEqual(left: ProjectSummary[], right: ProjectSummary[]): boolean {
  if (left === right) {
    return true;
  }
  if (left.length !== right.length) {
    return false;
  }
  for (let index = 0; index < left.length; index += 1) {
    if (projectMonitorSignature(left[index]) !== projectMonitorSignature(right[index])) {
      return false;
    }
  }
  return true;
}
