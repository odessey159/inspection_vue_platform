<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { formatRelativeSeconds } from "../lib/format";
import type { FindingResponse, ProjectSummary } from "../types";

const props = defineProps<{
  project: ProjectSummary | null;
  finding: FindingResponse | null;
  requestedSeekTs: number | null;
}>();

const emit = defineEmits<{
  requestSeek: [timestampMs: number];
}>();

const videoRef = ref<HTMLVideoElement | null>(null);

const evidenceTimeline = computed(() => {
  if (!props.finding) {
    return [];
  }
  return Array.from(
    new Set([props.finding.time_start_ms, ...props.finding.evidence_frame_ts, props.finding.time_end_ms]),
  ).sort((left, right) => left - right);
});

watch(
  () => [props.project?.inspection_video_url, props.project?.video_start_ts, props.requestedSeekTs],
  () => {
    if (!videoRef.value || !props.project?.video_start_ts || props.requestedSeekTs === null) {
      return;
    }
    const seconds = Math.max(0, (props.requestedSeekTs - props.project.video_start_ts) / 1000);
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
  () => props.project?.inspection_video_url,
  () => {
    videoRef.value?.load();
  },
);

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
</script>

<template>
  <section class="video-panel">
    <div class="panel-header compact-headline">
      <div>
        <p class="section-kicker">05 / 视频证据</p>
        <h2>inspection.mp4</h2>
      </div>
      <span class="panel-tag">
        {{ finding ? `定位到 ${formatRelativeSeconds(project, requestedSeekTs ?? finding.time_start_ms)}` : "等待选择隐患" }}
      </span>
    </div>

    <div v-if="project?.inspection_video_url" class="video-frame-shell">
      <video
        ref="videoRef"
        class="video-player"
        :src="project.inspection_video_url"
        controls
        preload="auto"
      />
      <div v-if="finding" class="video-caption">
        <strong>{{ finding.title }}</strong>
        <span>{{ formatRelativeSeconds(project, finding.time_start_ms) }} - {{ formatRelativeSeconds(project, finding.time_end_ms) }}</span>
      </div>
    </div>
    <div v-else class="empty-box">
      导入成功后这里会直接播放后端预生成的 inspection.mp4。
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
