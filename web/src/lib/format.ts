import type { AnalysisMode, ProjectSummary } from "../types";
import { analysisModeLabel, parseAnalysisMode } from "./analysisMode";

const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function formatDateTime(timestamp: number | null | undefined): string {
  if (!timestamp) {
    return "--";
  }
  return dateTimeFormatter.format(timestamp);
}

export function formatDurationMs(durationMs: number | null | undefined): string {
  if (!durationMs) {
    return "--";
  }
  const totalSeconds = Math.max(0, Math.round(durationMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}

export function formatCompactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "--";
  }
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "--";
  }
  return `${Math.round(value * 100)}%`;
}

export function formatRelativeSeconds(
  project: ProjectSummary | null | undefined,
  timestamp: number | null | undefined,
): string {
  if (!project?.video_start_ts || timestamp === null || timestamp === undefined) {
    return "--";
  }
  return `${((timestamp - project.video_start_ts) / 1000).toFixed(1)} s`;
}

export function formatTimestampWindow(
  start: number | null | undefined,
  end: number | null | undefined,
): string {
  if (!start || !end) {
    return "--";
  }
  return `${formatDateTime(start)} - ${formatDateTime(end)}`;
}

export function formatCoordinate(values: number[] | null | undefined): string {
  if (!values || values.length < 3) {
    return "--";
  }
  return values.slice(0, 3).map((value) => value.toFixed(2)).join(", ");
}

export function formatStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case "watching":
      return "等待 RTSP 流";
    case "indexed":
      return "已完成预生成";
    case "indexing":
      return "正在导入";
    case "rtsp_recording":
      return "正在录制 RTSP";
    case "rtsp_failed":
      return "RTSP 录制失败";
    case "demo_analyzed":
      return "Demo 分析完成";
    case "provider_analyzing":
      return "Provider 分析中";
    case "provider_analyzed":
      return "Provider 分析完成";
    case "provider_failed":
      return "Provider 分析失败";
    case "analysis_pending_provider":
      return "等待模型分析";
    case "rebuilt":
      return "场景已重建";
    case "failed":
      return "导入失败";
    default:
      return status || "等待导入";
  }
}

export function formatAnalysisMode(mode: AnalysisMode | string | null | undefined): string {
  const parsed = parseAnalysisMode(typeof mode === "string" ? mode : null);
  if (parsed) {
    return analysisModeLabel(parsed);
  }
  if (mode === "demo") {
    return "Demo";
  }
  return "--";
}
