<script setup lang="ts">
import { computed } from "vue";
import { formatStatusLabel } from "../lib/format";
import type { ProjectSummary } from "../types";

const props = defineProps<{
  projects: ProjectSummary[];
  currentProjectId: number | null;
  currentProject: ProjectSummary | null;
  statusLabel: string;
  loading: boolean;
  rtspWatchTestMode: boolean;
  rtspWatchTestMaxSeconds: number;
}>();

const emit = defineEmits<{
  refresh: [];
  selectProject: [projectId: number | null];
  clearRtspRecordings: [];
  resetRuntime: [];
  updateRtspWatchTestMode: [enabled: boolean];
}>();

const projectSummary = computed(() => {
  if (!props.currentProject) {
    return "导入 rosbag 后会预生成 inspection.mp4、真实点云场景和轨迹标注。";
  }
  return [
    props.currentProject.name,
    `状态 ${formatStatusLabel(props.currentProject.status)}`,
    props.currentProject.pose_topic ? `位姿 ${props.currentProject.pose_topic}` : "位姿待确认",
  ].join(" · ");
});

function onProjectChange(event: Event) {
  const target = event.target as HTMLSelectElement | null;
  if (!target?.value) {
    emit("selectProject", null);
    return;
  }
  emit("selectProject", Number(target.value));
}

function onRtspWatchTestModeChange(event: Event) {
  const target = event.target as HTMLInputElement | null;
  emit("updateRtspWatchTestMode", Boolean(target?.checked));
}
</script>

<template>
  <header class="topbar">
    <div class="topbar-brand">
      <p class="eyebrow">Industrial Inspection / Spatial Review Desk</p>
      <h1>巡检隐患三维工作台</h1>
      <p class="topbar-summary">{{ projectSummary }}</p>
    </div>

    <div class="topbar-actions">
      <label class="field compact-field">
        <span>当前项目</span>
        <select :value="currentProjectId ?? ''" @change="onProjectChange">
          <option value="">选择项目</option>
          <option v-for="project in projects" :key="project.id" :value="project.id">
            {{ project.name }}
          </option>
        </select>
      </label>

      <div class="status-pill">
        <span class="status-dot"></span>
        <span>{{ statusLabel }}</span>
      </div>

      <button class="ghost-button" @click="$emit('refresh')">刷新索引</button>
      <label
        class="field compact-field rtsp-test-toggle"
        :title="`测试模式下单次录制上限 ${Math.round(rtspWatchTestMaxSeconds / 60)} 分钟，超过 5 条录制时会删除最早的一条后再录`"
      >
        <span>RTSP 测试模式</span>
        <input
          type="checkbox"
          :checked="rtspWatchTestMode"
          :disabled="loading"
          @change="onRtspWatchTestModeChange"
        />
      </label>
      <button class="ghost-button danger-button" :disabled="loading" @click="$emit('clearRtspRecordings')">
        清空 RTSP 录制
      </button>
      <button class="ghost-button danger-button" :disabled="loading" @click="$emit('resetRuntime')">清空 .runtime</button>
    </div>
  </header>
</template>
