import type {
  AnalysisMode,
  BootstrapResponse,
  FindingPatchResponse,
  FindingResponse,
  ImageSceneRebuildResponse,
  ProjectImportPayload,
  ProjectSummary,
  RuntimeResetResponse,
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

export function getProjects() {
  return request<ProjectSummary[]>("/api/projects");
}

export function importProject(payload: ProjectImportPayload) {
  return request<ProjectSummary>("/api/projects/import", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function analyzeProject(projectId: number, mode: AnalysisMode = "demo", model?: string | null) {
  return request<FindingResponse[]>(`/api/projects/${projectId}/analyze`, {
    method: "POST",
    body: JSON.stringify({ mode, model: model || null }),
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
