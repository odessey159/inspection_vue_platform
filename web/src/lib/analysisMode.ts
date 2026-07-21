/**
 * Analysis mode resolution, availability gating, and user-facing labels.
 * Policy flags live in analysisConfig.ts.
 */
import type { AnalysisMode } from "../types";
import { DEFAULT_ANALYSIS_MODE, REQUIRE_PROVIDER_AVAILABILITY_FOR_SELECTION } from "./analysisConfig";

export type ProviderAvailability = {
  yolo_available?: boolean;
  provider_available?: boolean;
};

export function isProviderLikeMode(mode: AnalysisMode): boolean {
  return mode === "provider" || mode === "provider_yolo" || mode === "provider_yolo_monitor";
}

export function parseAnalysisMode(value: string | null | undefined): AnalysisMode | null {
  if (
    value === "demo"
    || value === "provider"
    || value === "provider_yolo"
    || value === "provider_yolo_monitor"
  ) {
    return value;
  }
  return null;
}

/** Default mode when the project has no saved preference. */
export function defaultAnalysisMode(source?: ProviderAvailability | null): AnalysisMode {
  if (!REQUIRE_PROVIDER_AVAILABILITY_FOR_SELECTION) {
    return DEFAULT_ANALYSIS_MODE;
  }
  if (source?.yolo_available) {
    return "provider_yolo";
  }
  if (source?.provider_available) {
    return "provider";
  }
  return "demo";
}

/** Pick the effective analysis mode from project state and provider availability. */
export function resolveAnalysisMode(
  project: ({ analysis_mode?: AnalysisMode | null } & ProviderAvailability) | null,
  fallbackSource?: ProviderAvailability | null,
): AnalysisMode {
  const availability: ProviderAvailability = {
    yolo_available: project?.yolo_available ?? fallbackSource?.yolo_available,
    provider_available: project?.provider_available ?? fallbackSource?.provider_available,
  };
  const fallback = defaultAnalysisMode(availability);
  const mode = parseAnalysisMode(project?.analysis_mode ?? null);
  if (!mode) {
    return fallback;
  }
  // Live monitor mode is not a manual selector option; map to provider_yolo for the side-path UI.
  const selectableMode: AnalysisMode = mode === "provider_yolo_monitor" ? "provider_yolo" : mode;
  if (!REQUIRE_PROVIDER_AVAILABILITY_FOR_SELECTION) {
    return selectableMode;
  }
  if (selectableMode === "provider_yolo" && !availability.yolo_available) {
    return fallback;
  }
  if (selectableMode === "provider" && !availability.provider_available) {
    return fallback;
  }
  return selectableMode;
}

export function isProviderModelSelectionDisabled(
  mode: AnalysisMode,
  availability: ProviderAvailability,
  supportedModelCount: number,
): boolean {
  if (!isProviderLikeMode(mode)) {
    return true;
  }
  if (supportedModelCount === 0) {
    return true;
  }
  if (!REQUIRE_PROVIDER_AVAILABILITY_FOR_SELECTION) {
    return false;
  }
  return !availability.provider_available;
}

export function isAnalysisModeOptionDisabled(mode: AnalysisMode, availability: ProviderAvailability): boolean {
  if (!REQUIRE_PROVIDER_AVAILABILITY_FOR_SELECTION) {
    return false;
  }
  if (mode === "provider_yolo") {
    return !availability.yolo_available;
  }
  if (mode === "provider") {
    return !availability.provider_available;
  }
  return false;
}

export function resolveRequestedModel(mode: AnalysisMode, model: string): string | null {
  if (!isProviderLikeMode(mode)) {
    return null;
  }
  const trimmed = model.trim();
  return trimmed || null;
}

export function analysisModeLabel(mode: AnalysisMode): string {
  switch (mode) {
    case "provider_yolo_monitor":
      return "实时 YOLO + 大模型";
    case "provider_yolo":
      return "YOLO + Provider";
    case "provider":
      return "Provider";
    default:
      return "Demo";
  }
}

export function analysisBusyLabel(mode: AnalysisMode, rtspSource: boolean, recordFreshRtsp = false): string {
  if (!isProviderLikeMode(mode)) {
    if (rtspSource) {
      return recordFreshRtsp
        ? "关联最新 RTSP 录制并运行 Demo 分析..."
        : "分析已关联 RTSP 视频并运行 Demo 分析...";
    }
    return "\u8fd0\u884c Demo \u79bb\u7ebf\u5206\u6790...";
  }
  const label = analysisModeLabel(mode);
  if (rtspSource) {
    return recordFreshRtsp
      ? `关联最新 RTSP 录制并运行 ${label} 视频隐患识别...`
      : `分析已关联 RTSP 视频并运行 ${label} 隐患识别...`;
  }
  return `\u8fd0\u884c ${label} \u89c6\u9891\u9690\u60a3\u8bc6\u522b...`;
}

export function analysisCompleteNotice(
  mode: AnalysisMode,
  model: string | null,
  findingsCount: number,
): string {
  if (!isProviderLikeMode(mode)) {
    return "Demo \u5206\u6790\u5b8c\u6210\uff0c\u5de5\u4f5c\u53f0\u8054\u52a8\u5df2\u66f4\u65b0\u3002";
  }
  const label = analysisModeLabel(mode);
  const modelLabel = model?.trim() || "\u9ed8\u8ba4\u6a21\u578b";
  if (findingsCount > 0) {
    return `${label} \u5206\u6790\u5b8c\u6210\uff0c\u5df2\u4f7f\u7528 ${modelLabel} \u540c\u6b65\u9690\u60a3\u5217\u8868\u3001\u89c6\u9891\u65f6\u95f4\u8f74\u548c\u4e09\u7ef4\u7a7a\u95f4\u6807\u6ce8\u3002`;
  }
  return `${label} \u5206\u6790\u5b8c\u6210\uff0c\u5df2\u4f7f\u7528 ${modelLabel} \u626b\u63cf\uff0c\u5f53\u524d\u672a\u8bc6\u522b\u5230\u660e\u786e\u53ef\u89c1\u9690\u60a3\u3002`;
}

export function analysisFailureMessage(
  project: {
    analysis_diagnostics?: string[];
    analysis_notes?: string[];
    status?: string | null;
  } | null,
  mode: AnalysisMode,
  model: string | null,
): string {
  const label = analysisModeLabel(mode);
  const modelLabel = model?.trim() || "\u9ed8\u8ba4\u6a21\u578b";
  const diagnostics = (project?.analysis_diagnostics ?? []).map((item) => item.trim()).filter(Boolean);
  const notes = (project?.analysis_notes ?? []).map((item) => item.trim()).filter(Boolean);
  const detail = diagnostics[0] || notes.find((note) => /fail|error|invalid|missing|not configured|unsupported/i.test(note)) || notes[0];
  if (detail) {
    return `${label} \u5206\u6790\u672a\u5b8c\u6210\uff08${modelLabel}\uff09\uff1a${detail}`;
  }
  const serviceHint =
    mode === "provider_yolo"
      ? "YOLO \u670d\u52a1\u4e0e\u5927\u6a21\u578b API \u914d\u7f6e\u53ca\u7f51\u7edc\u8fde\u901a\u6027"
      : "\u5927\u6a21\u578b API \u914d\u7f6e\u4e0e\u7f51\u7edc\u8fde\u901a\u6027";
  return `${label} \u5206\u6790\u672a\u5b8c\u6210\uff08${modelLabel}\uff09\uff0c\u8bf7\u68c0\u67e5 ${serviceHint}\u3002`;
}

export function analysisRunButtonLabel(mode: AnalysisMode, loading: boolean): string {
  if (loading) {
    return "\u6b63\u5728\u5206\u6790...";
  }
  switch (mode) {
    case "provider_yolo":
      return "\u8fd0\u884c YOLO + Provider \u5206\u6790";
    case "provider":
      return "\u8fd0\u884c Provider \u5206\u6790";
    default:
      return "\u8fd0\u884c Demo \u5206\u6790";
  }
}
