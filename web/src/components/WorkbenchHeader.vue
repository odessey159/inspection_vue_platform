<script setup lang="ts">
import { computed } from "vue";
import { formatStatusLabel } from "../lib/format";
import type { ProjectSummary, RtspVehicle } from "../types";

const props = defineProps<{
  vehicles: RtspVehicle[];
  currentVehicleId: string;
  currentProject: ProjectSummary | null;
  statusLabel: string;
  loading: boolean;
  rtspWatchTestMode: boolean;
  rtspWatchTestMaxSeconds: number;
}>();

const emit = defineEmits<{
  refresh: [];
  selectVehicle: [vehicleId: string];
  clearRtspRecordings: [];
  resetRuntime: [];
  updateRtspWatchTestMode: [enabled: boolean];
}>();

const projectSummary = computed(() => {
  if (!props.currentVehicleId && !props.currentProject) {
    return "选择巡检小车后，该车的录像、地图和隐患会显示在同一工作区。";
  }
  const vehicle = props.vehicles.find((item) => item.id === props.currentVehicleId);
  const vehicleLabel = vehicle?.name || props.currentProject?.name || props.currentVehicleId;
  if (!props.currentProject) {
    return `${vehicleLabel} · 流可用后自动录制，可预览车端地图与实时流`;
  }
  return [
    vehicleLabel,
    `状态 ${formatStatusLabel(props.currentProject.status)}`,
    props.currentProject.pose_topic ? `位姿 ${props.currentProject.pose_topic}` : "位姿待确认",
  ].join(" · ");
});

function onVehicleChange(event: Event) {
  const target = event.target as HTMLSelectElement | null;
  emit("selectVehicle", target?.value ?? "");
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
        <span>当前小车</span>
        <select :value="currentVehicleId" @change="onVehicleChange">
          <option value="">选择巡检小车</option>
          <option v-for="vehicle in vehicles" :key="vehicle.id" :value="vehicle.id">
            {{ vehicle.name }}
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
