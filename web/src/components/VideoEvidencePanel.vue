<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { getRtspPlaybackState } from "../lib/api";
import { formatRelativeSeconds } from "../lib/format";
import type { FindingResponse, ProjectSummary, RtspPlaybackMode, RtspPlaybackState } from "../types";

const props = defineProps<{
  project: ProjectSummary | null;
  finding: FindingResponse | null;
  requestedSeekTs: number | null;
  previewRtspUrl?: string | null;
}>();

const emit = defineEmits<{
  requestSeek: [timestampMs: number];
  playbackModeChange: [mode: RtspPlaybackMode, sourceStartTs: number | null];
  playbackTimeChange: [timestampMs: number | null];
}>();

const videoRef = ref<HTMLVideoElement | null>(null);
const videoError = ref("");
const videoLoading = ref(false);
/** True only after MJPEG first frame (@load); used so live mode waits for playable preview. */
const liveStreamReady = ref(false);
const liveStreamKey = ref(0);
const recordedVideoKey = ref(0);
const playbackState = ref<RtspPlaybackState | null>(null);
let pollTimer: number | null = null;

const activeRtspUrl = computed(() => {
  const projectUrl = props.project?.bag_dir?.trim() ?? "";
  if (projectUrl.toLowerCase().startsWith("rtsp://")) {
    return projectUrl;
  }
  const previewUrl = props.previewRtspUrl?.trim() ?? "";
  if (previewUrl.toLowerCase().startsWith("rtsp://")) {
    return previewUrl;
  }
  return null;
});

const isRtspContext = computed(() => activeRtspUrl.value !== null);

function isLivePlaybackState(state: RtspPlaybackState | null): boolean {
  if (!state) {
    return false;
  }
  return state.recording_active || state.stream_online;
}

function resolveRecordedPlaybackUrl(): string | null {
  const state = playbackState.value;
  if (state?.recorded_video_url) {
    return `${state.recorded_video_url}?v=${recordedVideoKey.value}`;
  }
  if (props.project?.rtsp_recorded_video_url) {
    return `${props.project.rtsp_recorded_video_url}?v=${recordedVideoKey.value}`;
  }
  return null;
}

function resolveLivePlaybackUrl(): string | null {
  const state = playbackState.value;
  const liveUrl = state?.live_url ?? props.project?.rtsp_live_url ?? null;
  if (!liveUrl) {
    return null;
  }
  return `${liveUrl}?v=${liveStreamKey.value}`;
}

function resolveArtifactPlaybackUrl(): string | null {
  return props.project?.inspection_video_url ?? null;
}

function buildProjectPlaybackSeed(): RtspPlaybackState | null {
  if (!props.project || !activeRtspUrl.value) {
    return null;
  }
  return {
    rtsp_url: activeRtspUrl.value,
    storage_key: "",
    // Never seed live flags from project summary — stale true values caused recorded→live flicker.
    recording_active: false,
    stream_online: false,
    live_url: props.project.rtsp_live_url ?? `/api/projects/${props.project.id}/rtsp-live`,
    live_video_start_ts: null,
    recorded_video_url: props.project.rtsp_recorded_video_url,
    recorded_video_start_ts: props.project.video_start_ts,
  };
}

function applyProjectPlaybackSeed() {
  playbackState.value = buildProjectPlaybackSeed();
}

/** Backend reports the RTSP source as online/recording (may still be a transient false start). */
const backendWantsLive = computed(() => {
  if (!isRtspContext.value) {
    return false;
  }
  return isLivePlaybackState(playbackState.value);
});

const livePlaybackUrl = computed(() => {
  if (!backendWantsLive.value) {
    return null;
  }
  return resolveLivePlaybackUrl();
});

const fallbackPlaybackUrl = computed(() => {
  return resolveRecordedPlaybackUrl() ?? resolveArtifactPlaybackUrl();
});

/**
 * Playback source selection:
 * - live only after MJPEG has a real first frame (avoids false-positive flicker)
 * - otherwise prefer project inspection.mp4 (clock matches trajectory timestamps)
 * - then fall back to the latest RTSP recording
 */
const playbackMode = computed<RtspPlaybackMode>(() => {
  if (backendWantsLive.value && liveStreamReady.value && livePlaybackUrl.value) {
    return "live";
  }
  if (resolveArtifactPlaybackUrl()) {
    return "artifact";
  }
  if (resolveRecordedPlaybackUrl()) {
    return "recorded";
  }
  return "empty";
});

const playbackSourceStartTs = computed<number | null>(() => {
  if (playbackMode.value === "live") {
    return playbackState.value?.live_video_start_ts ?? null;
  }
  if (playbackMode.value === "recorded") {
    return playbackState.value?.recorded_video_start_ts ?? props.project?.video_start_ts ?? null;
  }
  if (playbackMode.value === "artifact") {
    return props.project?.video_start_ts ?? null;
  }
  return null;
});

watch(
  [playbackMode, playbackSourceStartTs],
  ([mode, sourceStartTs]) => {
    emit("playbackModeChange", mode, sourceStartTs);
    if (mode === "live") {
      emit("playbackTimeChange", null);
    }
  },
  { immediate: true },
);

const playbackUrl = computed(() => {
  if (playbackMode.value === "live") {
    return livePlaybackUrl.value;
  }
  if (playbackMode.value === "recorded") {
    return resolveRecordedPlaybackUrl();
  }
  if (playbackMode.value === "artifact") {
    return resolveArtifactPlaybackUrl();
  }
  return null;
});

const playbackSourceKey = computed(() => {
  return playbackUrl.value ? `${playbackMode.value}:${playbackUrl.value}` : null;
});

const panelTitle = computed(() => {
  if (playbackMode.value === "live" || (backendWantsLive.value && livePlaybackUrl.value)) {
    return "RTSP 实时流";
  }
  if (playbackMode.value === "recorded") {
    return "RTSP 录制回放";
  }
  if (playbackMode.value === "artifact") {
    return "inspection.mp4";
  }
  return isRtspContext.value ? "RTSP 实时流" : "inspection.mp4";
});

const panelStatusTag = computed(() => {
  if (backendWantsLive.value && livePlaybackUrl.value) {
    if (videoError.value) {
      return "RTSP 连接失败";
    }
    if (!liveStreamReady.value) {
      return "正在连接 RTSP...";
    }
    return playbackState.value?.recording_active
      ? "录制中 · 实时预览"
      : "RTSP 在线 · 实时预览";
  }
  if (playbackMode.value === "recorded") {
    return "录制完成 · 回放";
  }
  if (playbackMode.value === "artifact" && isRtspContext.value) {
    return "离线 · inspection.mp4";
  }
  if (props.finding) {
    return `定位到 ${formatRelativeSeconds(props.project, props.requestedSeekTs ?? props.finding.time_start_ms)}`;
  }
  return "等待选择隐患";
});

const evidenceTimeline = computed(() => {
  if (!props.finding || playbackMode.value === "live") {
    return [];
  }
  return Array.from(
    new Set([props.finding.time_start_ms, ...props.finding.evidence_frame_ts, props.finding.time_end_ms]),
  ).sort((left, right) => left - right);
});

const showVideoShell = computed(() => {
  return Boolean(playbackUrl.value || livePlaybackUrl.value);
});

const showConnectingOverlay = computed(() => {
  return Boolean(backendWantsLive.value && livePlaybackUrl.value && !liveStreamReady.value && !videoError.value);
});

function clearPollTimer() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

function schedulePoll() {
  clearPollTimer();
  if (!activeRtspUrl.value) {
    return;
  }
  pollTimer = window.setInterval(() => {
    void refreshPlaybackState();
  }, 3000);
}

let playbackRefreshInFlight = false;

function playbackStatesEqual(left: RtspPlaybackState | null, right: RtspPlaybackState | null): boolean {
  if (left === right) {
    return true;
  }
  if (!left || !right) {
    return false;
  }
  return (
    left.rtsp_url === right.rtsp_url
    && left.storage_key === right.storage_key
    && left.recording_active === right.recording_active
    && left.stream_online === right.stream_online
    && left.live_url === right.live_url
    && left.live_video_start_ts === right.live_video_start_ts
    && left.recorded_video_url === right.recorded_video_url
    && left.recorded_video_start_ts === right.recorded_video_start_ts
  );
}

async function refreshPlaybackState() {
  const rtspUrl = activeRtspUrl.value;
  const projectId = props.project?.id ?? null;
  if (!rtspUrl) {
    playbackState.value = null;
    clearPollTimer();
    return;
  }
  if (playbackRefreshInFlight) {
    return;
  }
  playbackRefreshInFlight = true;

  try {
    const nextState = await getRtspPlaybackState(rtspUrl, projectId);
    if (activeRtspUrl.value !== rtspUrl || (props.project?.id ?? null) !== projectId) {
      return;
    }
    if (playbackStatesEqual(playbackState.value, nextState)) {
      return;
    }
    const wasLive = isLivePlaybackState(playbackState.value);
    const isLive = isLivePlaybackState(nextState);
    playbackState.value = nextState;

    if (wasLive && !isLive) {
      liveStreamReady.value = false;
      recordedVideoKey.value += 1;
      videoLoading.value = Boolean(
        nextState.recorded_video_url || props.project?.rtsp_recorded_video_url || props.project?.inspection_video_url,
      );
      videoError.value = "";
      await nextTick();
      videoRef.value?.load();
    } else if (!wasLive && isLive) {
      liveStreamReady.value = false;
      liveStreamKey.value += 1;
      videoLoading.value = !fallbackPlaybackUrl.value;
      videoError.value = "";
    } else if (!isLive && !videoError.value) {
      liveStreamReady.value = false;
      videoLoading.value = false;
    }
  } catch (error) {
    if (activeRtspUrl.value !== rtspUrl || (props.project?.id ?? null) !== projectId) {
      return;
    }
    if (!resolveRecordedPlaybackUrl() && !resolveArtifactPlaybackUrl()) {
      videoError.value = error instanceof Error ? error.message : "无法获取 RTSP 播放状态";
    }
    videoLoading.value = false;
  } finally {
    playbackRefreshInFlight = false;
  }
}

watch(
  () => activeRtspUrl.value,
  (nextUrl, previousUrl) => {
    if (previousUrl && nextUrl && previousUrl !== nextUrl) {
      liveStreamReady.value = false;
      liveStreamKey.value += 1;
    }
  },
);

watch(
  [
    () => activeRtspUrl.value,
    () => props.project?.id,
    () => props.project?.inspection_video_url,
    () => props.project?.rtsp_live_url,
    () => props.project?.rtsp_recorded_video_url,
  ],
  () => {
    videoError.value = "";
    clearPollTimer();
    if (!activeRtspUrl.value) {
      playbackState.value = null;
      liveStreamReady.value = false;
      videoLoading.value = Boolean(props.project?.inspection_video_url);
      return;
    }
    applyProjectPlaybackSeed();
    liveStreamReady.value = false;
    videoLoading.value = !resolveRecordedPlaybackUrl() && !resolveArtifactPlaybackUrl();
    void refreshPlaybackState();
    schedulePoll();
  },
  { immediate: true },
);

watch(
  [
    () => playbackUrl.value,
    () => playbackSourceStartTs.value,
    () => props.requestedSeekTs,
    () => playbackMode.value,
  ],
  () => {
    const sourceStartTs = playbackSourceStartTs.value;
    if (
      playbackMode.value === "live"
      || !videoRef.value
      || sourceStartTs === null
      || props.requestedSeekTs === null
    ) {
      return;
    }
    const seconds = Math.max(0, (props.requestedSeekTs - sourceStartTs) / 1000);
    const applySeek = () => {
      if (videoRef.value) {
        seekVideo(seconds);
      }
    };
    if (videoRef.value.readyState >= 1) {
      applySeek();
    } else {
      videoRef.value.addEventListener("loadedmetadata", applySeek, { once: true });
    }
  },
  { immediate: true },
);

watch(
  () => playbackSourceKey.value,
  async (nextKey, previousKey) => {
    if (!nextKey || nextKey === previousKey) {
      return;
    }
    videoError.value = "";
    if (playbackMode.value === "live") {
      return;
    }
    videoLoading.value = true;
    await nextTick();
    videoRef.value?.load();
  },
);

onBeforeUnmount(() => {
  clearPollTimer();
});

function handleVideoError() {
  videoLoading.value = false;
  videoError.value = "视频加载失败，请确认录制文件可访问。";
}

function handleLiveStreamError() {
  liveStreamReady.value = false;
  videoLoading.value = false;
  videoError.value = "实时 RTSP 流加载失败，请确认巡检车在线且后端可访问该 RTSP 地址。";
}

function handleVideoLoaded() {
  videoLoading.value = false;
  videoError.value = "";
}

function handleLiveStreamLoad() {
  liveStreamReady.value = true;
  videoLoading.value = false;
  videoError.value = "";
}

function retryLiveStream() {
  videoError.value = "";
  if (backendWantsLive.value && livePlaybackUrl.value) {
    liveStreamReady.value = false;
    videoLoading.value = !fallbackPlaybackUrl.value;
    liveStreamKey.value += 1;
    return;
  }
  videoLoading.value = true;
  recordedVideoKey.value += 1;
  videoRef.value?.load();
}

function seekVideo(seconds: number) {
  if (!videoRef.value) {
    return;
  }
  const video = videoRef.value;
  if (typeof video.fastSeek === "function") {
    video.fastSeek(seconds);
    return;
  }
  video.currentTime = seconds;
}

let lastPlaybackTimeEmitMs = 0;
const PLAYBACK_TIME_THROTTLE_MS = 200;

function handlePlaybackTimeUpdate(force = false) {
  const sourceStartTs = playbackSourceStartTs.value;
  if (playbackMode.value === "live" || !videoRef.value || sourceStartTs === null) {
    emit("playbackTimeChange", null);
    return;
  }
  const now = performance.now();
  if (!force && now - lastPlaybackTimeEmitMs < PLAYBACK_TIME_THROTTLE_MS) {
    return;
  }
  lastPlaybackTimeEmitMs = now;
  // Absolute timeline: video_start_ts + elapsed — must match trajectory_timestamps.
  const timestampMs = sourceStartTs + videoRef.value.currentTime * 1000;
  emit("playbackTimeChange", timestampMs);
}
</script>

<template>
  <section class="video-panel">
    <div class="panel-header compact-headline">
      <div>
        <p class="section-kicker">05 / 视频证据</p>
        <h2>{{ panelTitle }}</h2>
      </div>
      <span class="panel-tag">{{ panelStatusTag }}</span>
    </div>

    <div v-if="showVideoShell" class="video-frame-shell">
      <!-- Keep one MJPEG <img> mounted while backend wants live; only reveal after first frame. -->
      <img
        v-if="livePlaybackUrl"
        :key="liveStreamKey"
        class="video-player live-stream"
        :class="{ 'live-stream-pending': !liveStreamReady && Boolean(fallbackPlaybackUrl) }"
        :src="livePlaybackUrl"
        alt="RTSP 实时视频流"
        @load="handleLiveStreamLoad"
        @error="handleLiveStreamError"
      />
      <video
        v-if="playbackMode !== 'live' && playbackUrl"
        ref="videoRef"
        :key="playbackSourceKey ?? 'video'"
        class="video-player"
        :src="playbackUrl"
        controls
        playsinline
        preload="metadata"
        @error="handleVideoError"
        @loadeddata="handleVideoLoaded"
        @timeupdate="handlePlaybackTimeUpdate()"
        @seeked="handlePlaybackTimeUpdate(true)"
        @play="handlePlaybackTimeUpdate(true)"
      />
      <div v-if="showConnectingOverlay || videoLoading" class="video-status-hint">
        {{
          showConnectingOverlay
            ? "正在连接 RTSP 实时流..."
            : "正在加载录制视频..."
        }}
      </div>
      <div v-else-if="videoError" class="video-status-hint video-status-error">
        <span>{{ videoError }}</span>
        <button class="ghost-button compact-button" type="button" @click="retryLiveStream">
          重新加载
        </button>
      </div>
      <div v-if="finding" class="video-caption">
        <strong>{{ finding.title }}</strong>
        <span>{{ formatRelativeSeconds(project, finding.time_start_ms) }} - {{ formatRelativeSeconds(project, finding.time_end_ms) }}</span>
      </div>
    </div>
    <div v-else class="empty-box">
      {{
        isRtspContext
          ? "RTSP 在线时自动显示实时流；离线时优先回放最近录制或 inspection.mp4；流重新上线后会自动切回实时预览。"
          : "导入成功后这里会直接播放后端预生成的 inspection.mp4。"
      }}
    </div>

    <div v-if="evidenceTimeline.length" class="timeline-chip-row">
      <button
        v-for="timestamp in evidenceTimeline"
        :key="timestamp"
        class="timeline-chip"
        :class="{ active: timestamp === requestedSeekTs }"
        @click="$emit('requestSeek', timestamp)"
      >
        {{ formatRelativeSeconds(project, timestamp) }}
      </button>
    </div>
  </section>
</template>
