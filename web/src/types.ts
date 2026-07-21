export type ReviewStatus = "pending" | "confirmed" | "rejected";
export type AnalysisMode = "demo" | "provider" | "provider_yolo" | "provider_yolo_monitor";
export type SceneSource = "lidar" | "sfm";

export interface RtspVehicle {
  id: string;
  name: string;
  rtsp_url: string;
}

export interface BootstrapResponse {
  sample_bag_dir: string | null;
  sample_scene_path?: string | null;
  sample_pcd_path?: string | null;
  sample_standards_dir: string | null;
  default_rtsp_url: string;
  default_rtsp_record_seconds: number;
  rtsp_vehicles: RtspVehicle[];
  detected_bag_dirs: string[];
  detected_standards_dirs: string[];
  provider_available: boolean;
  yolo_available: boolean;
  provider_yolo_available: boolean;
  default_analysis_model: string | null;
  supported_analysis_models: string[];
  rtsp_watch_test_mode: boolean;
  rtsp_watch_test_max_seconds: number;
  rtsp_auto_analysis_enabled?: boolean;
  rtsp_auto_analysis_mode?: string | null;
  point_cloud_enabled?: boolean;
}

export interface PointCloudSettingsResponse {
  point_cloud_enabled: boolean;
}

export interface RuntimeResetResponse {
  status: string;
  removed_project_dirs: number;
  removed_bytes: number;
}

export interface RtspRecordingsClearResponse {
  status: string;
  deleted_files: number;
  freed_bytes: number;
}

export interface RtspWatchSettingsResponse {
  test_mode: boolean;
  test_max_seconds: number;
}

export interface RtspPlaybackState {
  rtsp_url: string;
  storage_key: string;
  recording_active: boolean;
  stream_online: boolean;
  live_url: string;
  live_video_start_ts: number | null;
  recorded_video_url: string | null;
  recorded_video_start_ts: number | null;
}

export interface SceneRebuildResponse {
  project_id: number;
  status: string;
  scene_url: string | null;
  colorized: boolean;
  color_source: string;
  raw_point_count: number;
  render_point_count: number;
  updated_at: string;
  notes: string[];
}

export interface ImageSceneRebuildResponse {
  project_id: number;
  status: string;
  scene_source: "sfm";
  selected_image_count: number;
  registered_image_count: number;
  dense_point_count: number;
  aligned: boolean;
  alignment_rmse_m: number | null;
  notes: string[];
}

export type RtspPlaybackMode = "live" | "recorded" | "artifact" | "empty";

export interface ProjectSummary {
  id: number;
  name: string;
  status: string;
  bag_dir: string;
  standards_dir: string;
  video_topic: string | null;
  point_topic: string | null;
  pose_topic: string | null;
  video_start_ts: number | null;
  video_end_ts: number | null;
  point_start_ts: number | null;
  point_end_ts: number | null;
  median_video_gap_ms: number | null;
  median_point_gap_ms: number | null;
  inferred_fps: number | null;
  bag_start_ts: number | null;
  bag_end_ts: number | null;
  bag_duration_ms: number | null;
  message_count: number | null;
  rules_count: number;
  findings_count: number;
  calibration_required: boolean;
  time_offset_ms: number | null;
  scene_url: string | null;
  inspection_video_url: string | null;
  rtsp_live_url: string | null;
  rtsp_recording_active: boolean;
  rtsp_stream_online?: boolean;
  rtsp_recorded_video_url: string | null;
  available_scene_sources: SceneSource[];
  default_scene_source: SceneSource;
  sfm_available: boolean;
  provider_available: boolean;
  yolo_available: boolean;
  provider_yolo_available: boolean;
  analysis_mode: AnalysisMode | null;
  analysis_provider: string | null;
  analysis_model: string | null;
  analysis_notes: string[];
  analysis_diagnostics: string[];
  analysis_updated_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RuleResponse {
  rule_id: string;
  domain: string;
  category: string;
  object_name: string;
  check_item: string;
  checker_scope: string;
  hazard_desc: string;
  legal_basis: string;
  evidence_objects: string[];
  severity: string;
  visual_detectable: boolean;
  source: string;
}

export interface ZoneResponse {
  id: number;
  finding_id: number;
  center: [number, number, number];
  radius_m: number;
  heading: number;
  related_pose_ts: number;
}

export interface FindingResponse {
  id: number;
  finding_uid: string;
  rule_id: string;
  title: string;
  time_start_ms: number;
  time_end_ms: number;
  evidence_frame_ts: number[];
  description: string;
  confidence: number;
  needs_review: boolean;
  review_status: ReviewStatus;
  reviewer_notes: string;
  severity: string;
  analysis_mode: string;
  legal_basis: string;
  hazard_desc: string;
  category: string;
  checker_scope: string;
  visual_detectable: boolean;
  zone: ZoneResponse | null;
}

export interface FindingPatchResponse {
  id: number;
  review_status: ReviewStatus;
  reviewer_notes: string;
  needs_review: boolean;
}

export type ScenePoint = [number, number, number, number] | [number, number, number, number, number, number, number];

export interface SceneResponse {
  project_id: number;
  scene_source: SceneSource;
  reconstruction_method: string;
  points: ScenePoint[];
  full_points: ScenePoint[];
  roof_removed_points: ScenePoint[];
  floor_removed_points: ScenePoint[];
  structure_points: ScenePoint[];
  render_points: ScenePoint[];
  default_point_mode: "roof_removed" | "full";
  trajectory: [number, number, number][];
  trajectory_timestamps: number[];
  trajectory_orientations: [number, number, number, number][];
  bounds: {
    min: [number, number, number];
    max: [number, number, number];
  };
  full_bounds: {
    min: [number, number, number];
    max: [number, number, number];
  };
  roof_removed_bounds: {
    min: [number, number, number];
    max: [number, number, number];
  };
  source_frame_count: number;
  coordinate_frame: "global" | "sensor_local";
  source_type: string;
  raw_point_count: number;
  render_point_count: number;
  structure_point_count: number;
  colorized: boolean;
  color_source: string;
  cut_height_default: number;
  floor_cut_default: number;
  scene_quality: Record<string, unknown>;
  selected_image_count: number;
  registered_image_count: number;
  alignment_status: string;
  alignment_rmse_m: number | null;
  notes: string[];
  hazard_zones: ZoneResponse[];
}

export interface ProjectImportPayload {
  name: string;
  bag_dir: string;
  standards_dir: string;
  rtsp_duration_sec?: number | null;
  rtsp_transport?: "tcp" | "udp" | null;
}
