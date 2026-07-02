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
}>();

const emit = defineEmits<{
  refresh: [];
  selectProject: [projectId: number | null];
  resetRuntime: [];
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
      <button class="ghost-button danger-button" :disabled="loading" @click="$emit('resetRuntime')">清空 .runtime</button>
    </div>
  </header>
</template>
