/**
 * Frontend helpers for map trajectory ↔ video clock sync.
 * Mirrors backend align_scene_timestamps_to_video / scene_timeline_overlaps_video.
 */
import type { SceneResponse } from "../types";

/**
 * Linearly remap trajectory timestamps onto a video clock.
 * Same idea as backend align_scene_timestamps_to_video.
 */
export function alignSceneTimestampsToVideo(
  scene: SceneResponse,
  videoStartTs: number,
  videoEndTs: number,
): SceneResponse {
  const startTs = Math.trunc(videoStartTs);
  let endTs = Math.trunc(videoEndTs);
  if (endTs <= startTs) {
    endTs = startTs + 1000;
  }

  const timestamps = scene.trajectory_timestamps ?? [];
  if (!timestamps.length) {
    return {
      ...scene,
      trajectory_timestamps: [startTs, endTs],
    };
  }

  const mapStart = timestamps[0];
  const mapEnd = timestamps[timestamps.length - 1];
  const mapSpan = Math.max(1, mapEnd - mapStart);
  const videoSpan = Math.max(1, endTs - startTs);
  const remap = (ts: number) => Math.round(startTs + ((ts - mapStart) / mapSpan) * videoSpan);

  return {
    ...scene,
    trajectory_timestamps: timestamps.map((ts) => remap(ts)),
    hazard_zones: (scene.hazard_zones ?? []).map((zone) => ({
      ...zone,
      related_pose_ts: zone.related_pose_ts ? remap(zone.related_pose_ts) : zone.related_pose_ts,
    })),
    scene_quality: {
      ...(scene.scene_quality ?? {}),
      time_alignment: {
        mode: "linear_map_to_video",
        map_start_ts: mapStart,
        map_end_ts: mapEnd,
        video_start_ts: startTs,
        video_end_ts: endTs,
      },
    },
  };
}

/** True when scene timeline overlaps the video clock enough for absolute sync. */
export function sceneTimelineOverlapsVideo(
  scene: SceneResponse | null | undefined,
  videoStartTs: number | null | undefined,
  videoEndTs?: number | null,
): boolean {
  const timestamps = scene?.trajectory_timestamps ?? [];
  if (!timestamps.length || videoStartTs == null || !Number.isFinite(videoStartTs)) {
    return false;
  }
  const sceneStart = timestamps[0];
  const sceneEnd = timestamps[timestamps.length - 1];
  const end = videoEndTs != null && Number.isFinite(videoEndTs) && videoEndTs > videoStartTs
    ? videoEndTs
    : videoStartTs + Math.max(1000, sceneEnd - sceneStart);
  return sceneEnd >= videoStartTs && sceneStart <= end;
}
