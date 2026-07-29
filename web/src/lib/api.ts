import type {
  AnalysisMode,
  BootstrapResponse,
  FindingPatchResponse,
  FindingResponse,
  ImageSceneRebuildResponse,
  PointCloudSettingsResponse,
  ProjectImportPayload,
  ProjectSummary,
  RuntimeResetResponse,
  RtspPlaybackState,
  RtspRecordingsClearResponse,
  RtspWatchSettingsResponse,
  RuleResponse,
  SceneRebuildResponse,
  SceneResponse,
  SceneSource,
} from "../types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function getBootstrap() {
  return request<BootstrapResponse>("/api/bootstrap");
}

export function resetRuntime() {
  return request<RuntimeResetResponse>("/api/runtime", {
    method: "DELETE",
  });
}

export function clearRtspRecordings() {
  return request<RtspRecordingsClearResponse>("/api/rtsp-recordings", {
    method: "DELETE",
  });
}

export function updateRtspWatchTestMode(testMode: boolean) {
  return request<RtspWatchSettingsResponse>("/api/rtsp-watch-settings", {
    method: "PATCH",
    body: JSON.stringify({ test_mode: testMode }),
  });
}

export function getPointCloudSettings() {
  return request<PointCloudSettingsResponse>("/api/point-cloud-settings");
}

export function updatePointCloudEnabled(enabled: boolean) {
  return request<PointCloudSettingsResponse>("/api/point-cloud-settings", {
    method: "PATCH",
    body: JSON.stringify({ point_cloud_enabled: enabled }),
  });
}

export function getRtspPlaybackState(rtspUrl: string, projectId?: number | null) {
  const params = new URLSearchParams({ rtsp_url: rtspUrl });
  if (projectId) {
    params.set("project_id", String(projectId));
  }
  return request<RtspPlaybackState>(`/api/rtsp-playback-state?${params.toString()}`);
}

/** Load a vehicle onboard map (transport-compacted scene.json) for preview. */
export function getVehicleScene(vehicleId: string) {
  return request<SceneResponse>(`/api/rtsp-vehicles/${encodeURIComponent(vehicleId)}/scene`);
}

export function getProjects() {
  return request<ProjectSummary[]>("/api/projects");
}

export function importProject(payload: ProjectImportPayload) {
  return request<ProjectSummary>("/api/projects/import", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function analyzeProject(
  projectId: number,
  mode: AnalysisMode = "demo",
  model?: string | null,
  recordFreshRtsp = false,
) {
  return request<FindingResponse[]>(`/api/projects/${projectId}/analyze`, {
    method: "POST",
    body: JSON.stringify({ mode, model: model || null, record_fresh_rtsp: recordFreshRtsp }),
  });
}

export function getRules(projectId: number) {
  return request<RuleResponse[]>(`/api/projects/${projectId}/rules`);
}

export function getFindings(projectId: number) {
  return request<FindingResponse[]>(`/api/projects/${projectId}/findings`);
}

export function getScene(projectId: number, source: SceneSource = "lidar") {
  return request<SceneResponse>(`/api/projects/${projectId}/scene?source=${source}`);
}

export async function getSceneOptional(projectId: number, source: SceneSource = "lidar"): Promise<SceneResponse | null> {
  const response = await fetch(`/api/projects/${projectId}/scene?source=${source}`, {
    headers: {
      "Content-Type": "application/json",
    },
  });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed: ${response.status}`);
  }
  return response.json() as Promise<SceneResponse>;
}

export function rebuildScene(projectId: number) {
  return request<SceneRebuildResponse>(`/api/projects/${projectId}/rebuild-scene`, {
    method: "POST",
  });
}

export function rebuildSceneFromImages(projectId: number) {
  return request<ImageSceneRebuildResponse>(`/api/projects/${projectId}/rebuild-scene-from-images`, {
    method: "POST",
  });
}

export function patchFinding(
  findingId: number,
  payload: {
    review_status?: string;
    reviewer_notes?: string;
    needs_review?: boolean;
  },
) {
  return request<FindingPatchResponse>(`/api/findings/${findingId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function frameUrl(projectId: number, timestampMs: number) {
  return `/api/projects/${projectId}/video-frames/${timestampMs}`;
}
