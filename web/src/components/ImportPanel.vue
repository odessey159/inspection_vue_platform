<script setup lang="ts">
import { computed, ref } from "vue";
import type { BootstrapResponse, RtspVehicle } from "../types";

const props = defineProps<{
  bootstrap: BootstrapResponse | null;
  loading: boolean;
  name: string;
  bagDir: string;
  standardsDir: string;
}>();

const emit = defineEmits<{
  updateName: [value: string];
  updateBagDir: [value: string];
  updateStandardsDir: [value: string];
  import: [];
  fillSample: [];
  selectVehicle: [vehicle: RtspVehicle];
  selectRosbag: [];
  backToVehicleSelect: [];
}>();

type ImportStep = "vehicle" | "import";
type ImportSource = "rtsp" | "rosbag";

const step = ref<ImportStep>("vehicle");
const importSource = ref<ImportSource>("rtsp");
const selectedVehicle = ref<RtspVehicle | null>(null);

const vehicles = computed(() => props.bootstrap?.rtsp_vehicles ?? []);
const isRtspImport = computed(() => importSource.value === "rtsp");

function emitInput(
  event: Event,
  forward: (value: string) => void,
) {
  const target = event.target as HTMLInputElement | null;
  forward(target?.value ?? "");
}

function chooseVehicle(vehicle: RtspVehicle) {
  selectedVehicle.value = vehicle;
  importSource.value = "rtsp";
  step.value = "import";
  emit("selectVehicle", vehicle);
}

function chooseRosbagImport() {
  selectedVehicle.value = null;
  importSource.value = "rosbag";
  step.value = "import";
  emit("selectRosbag");
}

function backToVehicleSelect() {
  step.value = "vehicle";
  selectedVehicle.value = null;
  importSource.value = "rtsp";
  emit("backToVehicleSelect");
}

function resetStep() {
  step.value = "vehicle";
  selectedVehicle.value = null;
  importSource.value = "rtsp";
}

defineExpose({ resetStep });
</script>

<template>
  <section class="strip-panel import-panel">
    <div v-if="step === 'vehicle'" class="vehicle-select-step">
      <div class="section-head">
        <div>
          <p class="section-kicker">01 / 数据导入</p>
          <h2>选择巡检小车</h2>
        </div>
      </div>

      <p class="import-intro">
        先选择要接入的巡检小车。后台会在检测到 RTSP 流后自动录制，流结束后可在此导入。
      </p>

      <div v-if="vehicles.length" class="vehicle-list">
        <div class="vehicle-list-head">
          <span>编号</span>
          <span>小车名称</span>
          <span>RTSP 地址</span>
          <span aria-hidden="true" />
        </div>
        <button
          v-for="(vehicle, index) in vehicles"
          :key="vehicle.id"
          type="button"
          class="vehicle-list-row"
          :disabled="loading"
          @click="chooseVehicle(vehicle)"
        >
          <span class="vehicle-list-index">{{ String(index + 1).padStart(2, "0") }}</span>
          <span class="vehicle-list-name">{{ vehicle.name }}</span>
          <span class="vehicle-list-url">{{ vehicle.rtsp_url }}</span>
          <span class="vehicle-list-action">选择</span>
        </button>
      </div>
      <p v-else class="hint-text">未检测到可用 RTSP 小车，请检查后端配置。</p>

      <div class="button-row">
        <button class="secondary-button" type="button" :disabled="loading" @click="chooseRosbagImport">
          使用 rosbag / scene.json 导入
        </button>
        <span class="hint-text">跳过 RTSP，直接导入本地 rosbag 目录或预生成的 scene.json 点云地图。</span>
      </div>

      <div v-if="bootstrap" class="hint-grid">
        <span>检测到 rosbag 目录 {{ bootstrap.detected_bag_dirs.length }} 个</span>
        <span>检测到标准目录 {{ bootstrap.detected_standards_dirs.length }} 个</span>
      </div>
    </div>

    <div v-else class="import-form-step">
      <div class="section-head">
        <div>
          <p class="section-kicker">01 / 数据导入</p>
          <h2>{{ isRtspImport ? "配置 RTSP 导入" : "导入 rosbag / scene.json" }}</h2>
        </div>
        <div class="button-row section-head-actions">
          <button class="ghost-button" type="button" :disabled="loading" @click="backToVehicleSelect">
            返回选择小车
          </button>
        </div>
      </div>

      <div v-if="selectedVehicle" class="selected-vehicle-banner">
        <span>已选小车</span>
        <strong>{{ selectedVehicle.name }}</strong>
        <code>{{ selectedVehicle.rtsp_url }}</code>
      </div>
      <div v-else class="selected-vehicle-banner rosbag-mode">
        <span>导入方式</span>
        <strong>rosbag 目录或 scene.json</strong>
      </div>

      <div class="field-grid">
        <label class="field">
          <span>项目名称</span>
          <input :value="name" placeholder="例如：厂区日检 2026-03-24" @input="emitInput($event, (value) => $emit('updateName', value))" />
        </label>

        <label class="field span-2">
          <span>{{ isRtspImport ? "RTSP URL" : "rosbag 目录 / scene.json" }}</span>
          <input
            :value="bagDir"
            :placeholder="isRtspImport ? 'rtsp://127.0.0.1:18554/live' : 'D:\\...\\scene.json 或 rosbag 目录'"
            :readonly="isRtspImport && !!selectedVehicle"
            @input="emitInput($event, (value) => $emit('updateBagDir', value))"
          />
        </label>

        <label class="field span-2">
          <span>standards 目录</span>
          <input
            :value="standardsDir"
            placeholder="D:\\Projects\\Inspection\\Analysis\\standards"
            @input="emitInput($event, (value) => $emit('updateStandardsDir', value))"
          />
        </label>
      </div>

      <div class="button-row">
        <button class="primary-button" type="button" :disabled="loading" @click="$emit('import')">
          {{ loading ? "正在导入..." : "开始预生成" }}
        </button>
        <span class="hint-text">
          {{
            isRtspImport
              ? "关联后台已录制的 RTSP 片段，并生成 inspection.mp4、占位 scene.json 和规则摘要。"
              : "导入 rosbag 或预生成 scene.json，并加载点云地图（rosbag 还会生成 inspection.mp4）和规则摘要。"
          }}
        </span>
      </div>

      <div v-if="bootstrap" class="hint-grid">
        <span>检测到 rosbag 目录 {{ bootstrap.detected_bag_dirs.length }} 个</span>
        <span v-if="bootstrap.sample_scene_path || bootstrap.sample_pcd_path">样例 scene.json 已就绪</span>
        <span>检测到标准目录 {{ bootstrap.detected_standards_dirs.length }} 个</span>
      </div>
    </div>
  </section>
</template>
