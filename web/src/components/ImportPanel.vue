<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { getRtspPlaybackState } from "../lib/api";
import type { BootstrapResponse, MapSummary, RtspVehicle } from "../types";

const props = defineProps<{
  bootstrap: BootstrapResponse | null;
  loading: boolean;
  name: string;
  bagDir: string;
  standardsDir: string;
  selectedVehicleId?: string;
  selectedVehicleName?: string;
  vehicleMapStatus?: "idle" | "loading" | "ready" | "missing" | "error";
  vehicleMapMessage?: string;
  projectId?: number | null;
  rtspRecordingActive?: boolean;
  rtspStreamOnline?: boolean;
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
  saveVehicleUrl: [vehicleId: string, rtspUrl: string];
  assignVehicleMap: [vehicleId: string, mapId: string | null];
}>();

type ImportStep = "vehicle" | "import";
type ImportSource = "rtsp" | "rosbag";

const step = ref<ImportStep>("vehicle");
const importSource = ref<ImportSource>("rtsp");
const selectedVehicle = ref<RtspVehicle | null>(null);
const editingVehicleId = ref<string | null>(null);
const draftUrl = ref("");
const urlError = ref("");
const urlInputRef = ref<HTMLInputElement | null>(null);

const vehicles = computed(() => props.bootstrap?.rtsp_vehicles ?? []);
const catalogMaps = computed<MapSummary[]>(() => props.bootstrap?.maps ?? []);
const isRtspImport = computed(() => importSource.value === "rtsp");

const polledRecording = ref<boolean | null>(null);
const polledOnline = ref<boolean | null>(null);
const streamStatusReady = ref(false);
let streamStatusTimer: number | null = null;

const recordingActive = computed(() => polledRecording.value ?? props.rtspRecordingActive ?? false);
const streamOnline = computed(() => polledOnline.value ?? props.rtspStreamOnline ?? false);

const rtspStreamState = computed(() => {
  if (recordingActive.value) {
    return "recording";
  }
  if (streamOnline.value) {
    return "online";
  }
  if (!streamStatusReady.value && props.rtspRecordingActive !== true && props.rtspStreamOnline !== true) {
    return "checking";
  }
  return "offline";
});

const rtspStreamStatusLabel = computed(() => {
  switch (rtspStreamState.value) {
    case "recording":
      return "录制中";
    case "online":
      return "RTSP 流在线";
    case "checking":
      return "检测中";
    default:
      return "RTSP 流离线";
  }
});

const rtspStreamHint = computed(() => {
  switch (rtspStreamState.value) {
    case "recording":
      return "已检测到可用流，后台正在自动录制。";
    case "online":
      return "流已上线，后台即将开始录制。";
    case "checking":
      return "正在检测当前小车的 RTSP 流状态。";
    default:
      return "当前没有可用流。流上线后会自动开始录制。";
  }
});

function clearStreamStatusTimer() {
  if (streamStatusTimer !== null) {
    window.clearInterval(streamStatusTimer);
    streamStatusTimer = null;
  }
}

async function refreshStreamStatus() {
  const url = props.bagDir.trim();
  if (!url.toLowerCase().startsWith("rtsp://") || step.value !== "import" || !isRtspImport.value) {
    return;
  }
  try {
    const state = await getRtspPlaybackState(url, props.projectId);
    if (props.bagDir.trim() !== url) {
      return;
    }
    polledRecording.value = Boolean(state.recording_active);
    polledOnline.value = Boolean(state.stream_online || state.recording_active);
    streamStatusReady.value = true;
  } catch {
    streamStatusReady.value = true;
    polledRecording.value = false;
    polledOnline.value = false;
  }
}

function scheduleStreamStatusPoll() {
  clearStreamStatusTimer();
  const url = props.bagDir.trim();
  if (step.value !== "import" || !isRtspImport.value || !url.toLowerCase().startsWith("rtsp://")) {
    polledRecording.value = null;
    polledOnline.value = null;
    streamStatusReady.value = false;
    return;
  }
  polledRecording.value = null;
  polledOnline.value = null;
  streamStatusReady.value = Boolean(props.rtspRecordingActive || props.rtspStreamOnline);
  void refreshStreamStatus();
  streamStatusTimer = window.setInterval(() => {
    void refreshStreamStatus();
  }, 3000);
}

watch(
  () => [step.value, importSource.value, props.bagDir, props.projectId, props.selectedVehicleId],
  () => {
    scheduleStreamStatusPoll();
  },
  { immediate: true },
);

onUnmounted(() => {
  clearStreamStatusTimer();
});

watch(vehicles, (list) => {
  if (!selectedVehicle.value) {
    return;
  }
  const next = list.find((item) => item.id === selectedVehicle.value?.id);
  if (next) {
    selectedVehicle.value = next;
  }
});

const mapStatusLabel = computed(() => {
  switch (props.vehicleMapStatus) {
    case "loading":
      return "地图加载中";
    case "ready":
      return "地图已显示";
    case "missing":
      return "未绑定地图";
    case "error":
      return "地图加载失败";
    default:
      return "";
  }
});

function emitInput(
  event: Event,
  forward: (value: string) => void,
) {
  const target = event.target as HTMLInputElement | null;
  forward(target?.value ?? "");
}

function chooseVehicle(vehicle: RtspVehicle) {
  cancelEdit();
  selectedVehicle.value = vehicle;
  importSource.value = "rtsp";
  step.value = "import";
  emit("selectVehicle", vehicle);
}

async function startEdit(vehicle: RtspVehicle) {
  editingVehicleId.value = vehicle.id;
  draftUrl.value = vehicle.rtsp_url;
  urlError.value = "";
  await nextTick();
  urlInputRef.value?.focus();
  urlInputRef.value?.select();
}

function bindUrlInput(el: unknown, vehicleId: string) {
  if (editingVehicleId.value === vehicleId && el instanceof HTMLInputElement) {
    urlInputRef.value = el;
  }
}

function cancelEdit() {
  editingVehicleId.value = null;
  draftUrl.value = "";
  urlError.value = "";
}

function saveDraftUrl(vehicle: RtspVehicle) {
  const nextUrl = draftUrl.value.trim();
  if (!nextUrl.toLowerCase().startsWith("rtsp://")) {
    urlError.value = "地址必须以 rtsp:// 开头";
    return;
  }
  urlError.value = "";
  emit("saveVehicleUrl", vehicle.id, nextUrl);
}

function saveSelectedUrl() {
  if (!selectedVehicle.value) {
    return;
  }
  const nextUrl = props.bagDir.trim();
  if (!nextUrl.toLowerCase().startsWith("rtsp://")) {
    urlError.value = "地址必须以 rtsp:// 开头";
    return;
  }
  urlError.value = "";
  emit("saveVehicleUrl", selectedVehicle.value.id, nextUrl);
}

function onAssignMap(event: Event) {
  if (!selectedVehicle.value) {
    return;
  }
  const target = event.target as HTMLSelectElement | null;
  const value = (target?.value ?? "").trim();
  emit("assignVehicleMap", selectedVehicle.value.id, value || null);
}

function chooseRosbagImport() {
  cancelEdit();
  selectedVehicle.value = null;
  importSource.value = "rosbag";
  step.value = "import";
  emit("selectRosbag");
}

function backToVehicleSelect() {
  cancelEdit();
  step.value = "vehicle";
  selectedVehicle.value = null;
  importSource.value = "rtsp";
  emit("backToVehicleSelect");
}

function resetStep() {
  step.value = "vehicle";
  selectedVehicle.value = null;
  importSource.value = "rtsp";
  cancelEdit();
}

function applyVehicle(vehicle: RtspVehicle) {
  cancelEdit();
  selectedVehicle.value = vehicle;
  importSource.value = "rtsp";
  step.value = "import";
}

function applyUpdatedVehicle(vehicle: RtspVehicle) {
  if (selectedVehicle.value?.id === vehicle.id) {
    selectedVehicle.value = vehicle;
  }
  if (editingVehicleId.value === vehicle.id) {
    cancelEdit();
  }
}

defineExpose({ resetStep, applyVehicle, applyUpdatedVehicle });
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
        选择巡检小车即可进入该车工作区。检测到 RTSP 流可用后，后台会自动开始录制，无需手动预生成。
      </p>

      <div v-if="vehicles.length" class="vehicle-list">
        <div class="vehicle-list-head">
          <span>编号</span>
          <span>小车名称</span>
          <span>RTSP 地址</span>
          <span>操作</span>
        </div>
        <div
          v-for="(vehicle, index) in vehicles"
          :key="vehicle.id"
          class="vehicle-list-row"
          :class="{
            editing: editingVehicleId === vehicle.id,
            'is-disabled': loading,
          }"
        >
          <span class="vehicle-list-index">{{ String(index + 1).padStart(2, "0") }}</span>
          <span class="vehicle-list-name">{{ vehicle.name }}</span>
          <input
            v-if="editingVehicleId === vehicle.id"
            :ref="(el) => bindUrlInput(el, vehicle.id)"
            v-model="draftUrl"
            class="vehicle-list-url-input"
            type="text"
            spellcheck="false"
            :disabled="loading"
            @keydown.enter.prevent="saveDraftUrl(vehicle)"
            @keydown.esc.prevent="cancelEdit"
          />
          <span v-else class="vehicle-list-url" :title="vehicle.rtsp_url">{{ vehicle.rtsp_url }}</span>
          <div class="vehicle-list-actions">
            <template v-if="editingVehicleId === vehicle.id">
              <button class="vehicle-list-action-btn" type="button" :disabled="loading" @click="saveDraftUrl(vehicle)">
                保存
              </button>
              <button class="vehicle-list-action-btn muted" type="button" :disabled="loading" @click="cancelEdit">
                取消
              </button>
            </template>
            <template v-else>
              <button class="vehicle-list-action-btn muted" type="button" :disabled="loading" @click="startEdit(vehicle)">
                修改
              </button>
              <button class="vehicle-list-action-btn" type="button" :disabled="loading" @click="chooseVehicle(vehicle)">
                选择
              </button>
            </template>
          </div>
        </div>
      </div>
      <p v-else class="hint-text">未检测到可用 RTSP 小车，请检查后端配置。</p>
      <p v-if="urlError && step === 'vehicle'" class="hint-text vehicle-url-error">{{ urlError }}</p>

      <div class="button-row">
        <button class="secondary-button" type="button" :disabled="loading" @click="chooseRosbagImport">
          使用 rosbag / 地图导入
        </button>
        <span class="hint-text">跳过 RTSP：导入 rosbag 生成视频，或导入 scene.json / PCD 到独立地图目录。</span>
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
          <h2>{{ isRtspImport ? "巡检小车工作区" : "导入 rosbag / 点云地图" }}</h2>
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
        <code>{{ selectedVehicle.id }}</code>
        <code>{{ selectedVehicle.rtsp_url }}</code>
        <em v-if="mapStatusLabel" :data-status="vehicleMapStatus">{{ mapStatusLabel }}</em>
        <p v-if="vehicleMapMessage" class="vehicle-map-hint">{{ vehicleMapMessage }}</p>
        <label class="field vehicle-map-bind">
          <span>点云地图索引</span>
          <select
            :value="selectedVehicle.map_id ?? ''"
            :disabled="loading"
            @change="onAssignMap($event)"
          >
            <option value="">不绑定地图</option>
            <option v-for="item in catalogMaps" :key="item.id" :value="item.id">
              {{ item.name }}（{{ item.id }}）
            </option>
          </select>
        </label>
      </div>
      <div v-else class="selected-vehicle-banner rosbag-mode">
        <span>导入方式</span>
        <strong>rosbag 目录或点云地图</strong>
      </div>

      <div class="field-grid">
        <label v-if="!isRtspImport" class="field">
          <span>项目名称</span>
          <input :value="name" placeholder="例如：厂区日检 2026-03-24" @input="emitInput($event, (value) => $emit('updateName', value))" />
        </label>

        <div class="field span-2">
          <span>{{ isRtspImport ? "RTSP URL" : "rosbag 目录 / scene.json / PCD" }}</span>
          <div class="vehicle-url-edit">
            <input
              :value="bagDir"
              :placeholder="isRtspImport ? 'rtsp://127.0.0.1:18554/live' : 'D:\\...\\scene.json 或 rosbag 目录'"
              @input="emitInput($event, (value) => $emit('updateBagDir', value))"
            />
            <button
              v-if="isRtspImport && selectedVehicle"
              class="secondary-button"
              type="button"
              :disabled="loading"
              @click="saveSelectedUrl"
            >
              保存地址
            </button>
          </div>
          <p v-if="urlError && step === 'import'" class="hint-text vehicle-url-error">{{ urlError }}</p>
        </div>

        <label class="field span-2">
          <span>standards 目录</span>
          <input
            :value="standardsDir"
            placeholder="D:\\Projects\\Inspection\\Analysis\\standards"
            @input="emitInput($event, (value) => $emit('updateStandardsDir', value))"
          />
        </label>
      </div>

      <div v-if="isRtspImport" class="button-row">
        <div class="rtsp-stream-status" :data-state="rtspStreamState">
          <span class="status-dot"></span>
          <strong>{{ rtspStreamStatusLabel }}</strong>
        </div>
        <span class="hint-text">{{ rtspStreamHint }}</span>
      </div>
      <div v-else class="button-row">
        <button class="primary-button" type="button" :disabled="loading" @click="$emit('import')">
          {{ loading ? "正在导入..." : "开始预生成" }}
        </button>
        <span class="hint-text">
          导入 rosbag 只生成视频和规则；scene.json / PCD 会进入独立地图目录，处理后再按 map_id 绑定到小车。
        </span>
      </div>

      <div v-if="bootstrap" class="hint-grid">
        <span>检测到 rosbag 目录 {{ bootstrap.detected_bag_dirs.length }} 个</span>
        <span v-if="bootstrap.sample_scene_path || bootstrap.sample_pcd_path">样例地图文件已就绪</span>
        <span>检测到标准目录 {{ bootstrap.detected_standards_dirs.length }} 个</span>
      </div>
    </div>
  </section>
</template>
