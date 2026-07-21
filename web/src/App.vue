<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import AnalysisPanel from "./components/AnalysisPanel.vue";
import FindingDetailPanel from "./components/FindingDetailPanel.vue";
import FindingsPanel from "./components/FindingsPanel.vue";
import ImportPanel from "./components/ImportPanel.vue";
import MetricsPanel from "./components/MetricsPanel.vue";
import SceneViewport from "./components/SceneViewport.vue";
import VideoEvidencePanel from "./components/VideoEvidencePanel.vue";
import WorkbenchHeader from "./components/WorkbenchHeader.vue";
import {
  analyzeProject,
  getBootstrap,
  getFindings,
  getProjects,
  getRules,
  getSceneOptional,
  importProject,
  patchFinding,
  rebuildScene,
  resetRuntime,
  clearRtspRecordings,
  updateRtspWatchTestMode,
} from "./lib/api";
import { formatStatusLabel } from "./lib/format";
import {
  analysisBusyLabel,
  analysisCompleteNotice,
  analysisFailureMessage,
  isProviderLikeMode,
  resolveAnalysisMode,
  resolveRequestedModel,
} from "./lib/analysisMode";
import { DEFAULT_ANALYSIS_MODE } from "./lib/analysisConfig";
import { mergeFindingsSnapshot, projectsMonitorEqual } from "./lib/findingsSync";
import type {
  AnalysisMode,
  BootstrapResponse,
  FindingResponse,
  ProjectSummary,
  ReviewStatus,
  RuleResponse,
  RtspVehicle,
  RtspPlaybackMode,
  SceneResponse,
} from "./types";

const bootstrap = ref<BootstrapResponse | null>(null);
const projects = ref<ProjectSummary[]>([]);
const currentProjectId = ref<number | null>(null);
const scene = ref<SceneResponse | null>(null);
const rules = ref<RuleResponse[]>([]);
const findings = ref<FindingResponse[]>([]);
const selectedFindingId = ref<number | null>(null);
const activeEvidenceTs = ref<number | null>(null);
const pendingEvidenceSeekTs = ref<number | null>(null);
const videoPlaybackMode = ref<RtspPlaybackMode>("empty");
const videoPlaybackSourceStartTs = ref<number | null>(null);
const videoPlaybackTs = ref<number | null>(null);
const loading = ref(false);
const busyLabel = ref("");
const errorMessage = ref("");
const notice = ref("");
const analysisMode = ref<AnalysisMode>(DEFAULT_ANALYSIS_MODE);
const analysisModel = ref("qwen3.5-plus");
const rtspWatchTestMode = ref(true);
const importForm = reactive({
  name: "巡检车三维复核工作台",
  bag_dir: "",
  standards_dir: "",
});

const importPanelRef = ref<InstanceType<typeof ImportPanel> | null>(null);
const FINDINGS_POLL_MS = 5000;
let findingsPollTimer: number | null = null;
let findingsPollGeneration = 0;

const previewRtspUrl = computed(() => {
  if (currentProject.value) {
    return null;
  }
  const url = importForm.bag_dir.trim();
  return url.toLowerCase().startsWith("rtsp://") ? url : null;
});

const currentProject = computed(() => {
  return projects.value.find((project) => project.id === currentProjectId.value) ?? null;
});

const isCurrentProjectRtsp = computed(() => {
  return Boolean(currentProject.value?.bag_dir.trim().toLowerCase().startsWith("rtsp://"));
});

const playbackFindings = computed(() => {
  if (!isCurrentProjectRtsp.value || videoPlaybackMode.value === "empty") {
    return findings.value;
  }
  const sourceStartTs = videoPlaybackSourceStartTs.value;
  if (sourceStartTs === null) {
    return [];
  }
  return findings.value.filter((finding) => {
    if (videoPlaybackMode.value === "live" && finding.analysis_mode !== "provider_yolo_monitor") {
      return false;
    }
    return finding.time_start_ms >= sourceStartTs;
  });
});

const selectedFinding = computed(() => {
  if (selectedFindingId.value === null) {
    return null;
  }
  return playbackFindings.value.find((finding) => finding.id === selectedFindingId.value) ?? null;
});

const visualRuleCount = computed(() => {
  return rules.value.filter((rule) => rule.visual_detectable).length;
});

/** RTSP live: hide map. Recorded/artifact or non-RTSP: show map for timestamp sync. */
const showSceneMap = computed(() => {
  if (bootstrap.value && bootstrap.value.point_cloud_enabled === false) {
    return false;
  }
  if (!isCurrentProjectRtsp.value) {
    return true;
  }
  return videoPlaybackMode.value !== "live";
});

const sceneActiveTimestampMs = computed(() => {
  return videoPlaybackTs.value ?? activeEvidenceTs.value;
});

const hasRenderableScene = computed(() => {
  if (!scene.value) {
    return false;
  }
  if (scene.value.source_type === "rtsp_placeholder") {
    return false;
  }
  return (scene.value.render_points?.length ?? 0) > 0 || (scene.value.points?.length ?? 0) > 0;
});

const supportedAnalysisModels = computed(() => {
  return bootstrap.value?.supported_analysis_models ?? [];
});

const statusLabel = computed(() => {
  if (busyLabel.value) {
    return busyLabel.value;
  }
  if (currentProject.value) {
    return formatStatusLabel(currentProject.value.status);
  }
  return "等待导入项目";
});

watch(
  () => selectedFindingId.value,
  (findingId) => {
    if (pendingEvidenceSeekTs.value !== null) {
      activeEvidenceTs.value = pendingEvidenceSeekTs.value;
      pendingEvidenceSeekTs.value = null;
      return;
    }
    if (findingId === null) {
      activeEvidenceTs.value = null;
      return;
    }
    const finding = playbackFindings.value.find((item) => item.id === findingId) ?? null;
    activeEvidenceTs.value = finding?.time_start_ms ?? null;
  },
  { immediate: true },
);

watch(
  [
    () => currentProjectId.value,
    () => currentProject.value?.bag_dir,
    () => currentProject.value?.rtsp_recording_active,
    () => currentProject.value?.rtsp_stream_online,
    () => currentProject.value?.status,
    () => videoPlaybackMode.value,
    () => videoPlaybackSourceStartTs.value,
  ],
  () => {
    scheduleFindingsPoll();
  },
  { immediate: true },
);

onMounted(() => {
  void initialize();
});

onUnmounted(() => {
  invalidateFindingsPoll();
});

function isRtspLiveMonitorProject(project: ProjectSummary | null): boolean {
  if (!project) {
    return false;
  }
  const isRtsp = project.bag_dir.trim().toLowerCase().startsWith("rtsp://");
  if (!isRtsp) {
    return false;
  }
  const playbackConfirmsLive =
    project.id === currentProjectId.value && videoPlaybackMode.value === "live";
  // Only poll while the stream is actively live/recording — not merely because a
  // historical analysis_mode of provider_yolo_monitor was persisted. The video
  // component can confirm a reconnect before the project summary is refreshed.
  return Boolean(
    playbackConfirmsLive
    || project.rtsp_recording_active
    || project.rtsp_stream_online
    || project.status === "provider_analyzing",
  );
}

function clearFindingsPoll() {
  if (findingsPollTimer !== null) {
    window.clearInterval(findingsPollTimer);
    findingsPollTimer = null;
  }
}

function invalidateFindingsPoll() {
  findingsPollGeneration += 1;
  clearFindingsPoll();
}

function scheduleFindingsPoll() {
  invalidateFindingsPoll();
  if (!isRtspLiveMonitorProject(currentProject.value) || currentProjectId.value === null) {
    return;
  }
  // Fetch immediately when live playback is detected instead of waiting for the
  // first interval tick. This also refreshes a stale project online status.
  void refreshLiveFindings();
  findingsPollTimer = window.setInterval(() => {
    void refreshLiveFindings();
  }, FINDINGS_POLL_MS);
}

function setNotice(message: string) {
  errorMessage.value = "";
  notice.value = message;
}

function setError(message: string) {
  notice.value = "";
  errorMessage.value = message;
}

let findingsRefreshInFlight = false;

async function refreshLiveFindings() {
  const projectId = currentProjectId.value;
  const generation = findingsPollGeneration;
  if (projectId === null || findingsRefreshInFlight) {
    return;
  }
  findingsRefreshInFlight = true;
  try {
    const [projectPayload, findingsPayload] = await Promise.all([getProjects(), getFindings(projectId)]);
    if (generation !== findingsPollGeneration || projectId !== currentProjectId.value) {
      return;
    }
    if (!projectsMonitorEqual(projects.value, projectPayload)) {
      projects.value = projectPayload;
    }
    const previousSelected = selectedFindingId.value;
    const previousCount = findings.value.length;
    const merged = mergeFindingsSnapshot(findings.value, findingsPayload);
    if (merged.changed) {
      findings.value = merged.findings;
      if (previousSelected !== null && merged.findings.some((finding) => finding.id === previousSelected)) {
        selectedFindingId.value = previousSelected;
      } else if (previousSelected !== null && !merged.findings.some((finding) => finding.id === previousSelected)) {
        // Keep an empty selection on poll refresh instead of jumping to the first finding.
        selectedFindingId.value = null;
      }
      if (merged.findings.length > previousCount && !errorMessage.value) {
        // Avoid clobbering an explicit user error; soft-update only when idle.
        notice.value = `实时监控已更新隐患：${merged.findings.length} 条`;
      }
    }
    if (!isRtspLiveMonitorProject(currentProject.value)) {
      clearFindingsPoll();
    }
  } catch {
    // Keep silent during background polling; user can refresh manually.
  } finally {
    findingsRefreshInFlight = false;
  }
}

async function initialize(preferredProjectId: number | null = currentProjectId.value) {
  setBusy("刷新项目索引...");
  try {
    const [bootstrapPayload, projectPayload] = await Promise.all([getBootstrap(), getProjects()]);
    bootstrap.value = bootstrapPayload;
    rtspWatchTestMode.value = bootstrapPayload.rtsp_watch_test_mode;
    projects.value = projectPayload;
    fillImportSamples(false);

    const nextProjectId = resolveProjectId(preferredProjectId, projectPayload);
    if (nextProjectId === null) {
      clearProjectArtifacts();
      currentProjectId.value = null;
      syncAnalysisSelection(null);
      errorMessage.value = "";
      return;
    }

    await openProject(nextProjectId);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
  }
}

async function openProject(projectId: number | null) {
  const switchedProject = projectId !== currentProjectId.value;
  invalidateFindingsPoll();
  if (projectId === null) {
    currentProjectId.value = null;
    clearProjectArtifacts();
    syncAnalysisSelection(null);
    return;
  }

  currentProjectId.value = projectId;
  const project = projects.value.find((item) => item.id === projectId) ?? null;
  syncAnalysisSelection(project);
  setBusy("加载场景与分析结果...");
  try {
    const [scenePayload, rulesPayload, findingsPayload] = await Promise.all([
      getSceneOptional(projectId, "lidar"),
      getRules(projectId),
      getFindings(projectId),
    ]);
    scene.value = scenePayload;
    rules.value = rulesPayload;
    findings.value = findingsPayload;

    if (switchedProject) {
      selectedFindingId.value = findingsPayload[0]?.id ?? null;
    } else if (
      selectedFindingId.value !== null
      && !findingsPayload.some((finding) => finding.id === selectedFindingId.value)
    ) {
      selectedFindingId.value = null;
    }
    errorMessage.value = "";
  } catch (error) {
    scene.value = null;
    rules.value = [];
    findings.value = [];
    selectedFindingId.value = null;
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
    scheduleFindingsPoll();
  }
}

async function handleImport() {
  loading.value = true;
  notice.value = "";
  errorMessage.value = "";
  const source = importForm.bag_dir.trim();
  if (!source) {
    setError("请先选择巡检小车，或填写 rosbag 目录 / scene.json 路径。");
    loading.value = false;
    return;
  }
  const isRtspImport = source.toLowerCase().startsWith("rtsp://");
  const isSceneImport =
    !isRtspImport &&
    (source.toLowerCase().endsWith("scene.json") || source.toLowerCase().endsWith(".json"));
  setBusy(
    isRtspImport
      ? "关联 RTSP 录制并生成 inspection.mp4..."
      : isSceneImport
        ? "读取 scene.json 并加载点云地图..."
        : "导入 rosbag 并预生成视频 / 场景...",
  );
  try {
    const project = await importProject({
      name: importForm.name.trim() || "巡检车三维复核工作台",
      bag_dir: source,
      standards_dir: importForm.standards_dir.trim(),
      rtsp_transport: isRtspImport ? "tcp" : null,
    });
    await initialize(project.id);
    setNotice(
      isRtspImport
        ? "已关联后台 RTSP 录制；若小车 maps 目录有点云，回放时可与视频时间同步。"
        : isSceneImport
          ? "scene.json 导入完成，点云地图已加载。"
          : "导入完成，inspection.mp4、scene.json 和规则摘要已生成。",
    );
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
    loading.value = false;
  }
}

async function handleClearRtspRecordings() {
  const confirmed = window.confirm("这会清空 rtsp_recordings 目录下的所有 RTSP 录制文件，是否继续？");
  if (!confirmed) {
    return;
  }

  loading.value = true;
  errorMessage.value = "";
  notice.value = "";
  setBusy("清空 RTSP 录制缓存...");
  try {
    const result = await clearRtspRecordings();
    await initialize(currentProjectId.value);
    setNotice(
      `已清空 rtsp_recordings，删除 ${result.deleted_files} 个文件，释放 ${(result.freed_bytes / (1024 * 1024)).toFixed(1)} MB。`,
    );
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
    loading.value = false;
  }
}

async function handleRtspWatchTestModeChange(enabled: boolean) {
  const previous = rtspWatchTestMode.value;
  rtspWatchTestMode.value = enabled;
  loading.value = true;
  errorMessage.value = "";
  try {
    const result = await updateRtspWatchTestMode(enabled);
    rtspWatchTestMode.value = result.test_mode;
    if (bootstrap.value) {
      bootstrap.value = {
        ...bootstrap.value,
        rtsp_watch_test_mode: result.test_mode,
        rtsp_watch_test_max_seconds: result.test_max_seconds,
      };
    }
    notice.value = result.test_mode
      ? `已开启 RTSP 测试模式：单次录制上限 ${Math.round(result.test_max_seconds / 60)} 分钟，超过 5 条录制时会删除最早的一条后再录。`
      : "已关闭 RTSP 测试模式，恢复为录到流结束为止。";
    errorMessage.value = "";
  } catch (error) {
    rtspWatchTestMode.value = previous;
    setError(getErrorMessage(error));
  } finally {
    loading.value = false;
  }
}

async function handleResetRuntime() {
  const firstPass = window.confirm("这会删除整个 .runtime、数据库和所有已导入项目，是否继续？");
  if (!firstPass) {
    return;
  }
  const secondPass = window.confirm("请再次确认：清空后需要重新导入 rosbag 才能恢复工作台内容。\n确定继续吗？");
  if (!secondPass) {
    return;
  }

  loading.value = true;
  errorMessage.value = "";
  notice.value = "";
  setBusy("清空运行缓存并重建数据库...");
  try {
    const result = await resetRuntime();
    projects.value = [];
    currentProjectId.value = null;
    clearProjectArtifacts();
    importForm.bag_dir = "";
    importForm.standards_dir = "";
    importPanelRef.value?.resetStep();
    await initialize(null);
    setNotice(`已清空 .runtime，删除 ${result.removed_project_dirs} 个项目目录，释放 ${(result.removed_bytes / (1024 * 1024)).toFixed(1)} MB。`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
    loading.value = false;
  }
}

function syncAnalysisSelection(project: ProjectSummary | null) {
  if (!project) {
    analysisMode.value = resolveAnalysisMode(null, bootstrap.value);
    analysisModel.value = bootstrap.value?.default_analysis_model ?? supportedAnalysisModels.value[0] ?? "qwen3.5-plus";
    return;
  }

  analysisMode.value = resolveAnalysisMode(project, bootstrap.value);

  const preferredModel = project.analysis_model ?? bootstrap.value?.default_analysis_model ?? supportedAnalysisModels.value[0] ?? "qwen3.5-plus";
  if (supportedAnalysisModels.value.length === 0 || supportedAnalysisModels.value.includes(preferredModel)) {
    analysisModel.value = preferredModel;
  } else {
    analysisModel.value = supportedAnalysisModels.value[0];
  }
}

async function handleAnalyze() {
  if (!currentProject.value) {
    return;
  }
  if (isRtspLiveMonitorProject(currentProject.value)) {
    setError("实时监控进行中，请等待流结束后再运行离线批量分析，以免覆盖实时隐患。");
    return;
  }
  loading.value = true;
  notice.value = "";
  errorMessage.value = "";
  const projectId = currentProject.value.id;
  const requestedMode = analysisMode.value;
  const requestedModel = resolveRequestedModel(requestedMode, analysisModel.value);
  const rtspSource = currentProject.value.bag_dir.trim().toLowerCase().startsWith("rtsp://");
  setBusy(analysisBusyLabel(requestedMode, rtspSource));
  try {
    await analyzeProject(projectId, requestedMode, requestedModel);
    await initialize(projectId);
    const refreshed = projects.value.find((project) => project.id === projectId) ?? null;
    if (refreshed?.status === "provider_failed" && isProviderLikeMode(requestedMode)) {
      setError(analysisFailureMessage(refreshed, requestedMode, requestedModel));
      return;
    }
    setNotice(
      analysisCompleteNotice(
        requestedMode,
        requestedModel,
        refreshed?.findings_count ?? 0,
      ),
    );
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
    loading.value = false;
  }
}

async function handleRebuildScene() {
  if (!currentProject.value) {
    return;
  }
  loading.value = true;
  notice.value = "";
  errorMessage.value = "";
  setBusy("重建全量 LiDAR 结构点云场景...");
  try {
    const result = await rebuildScene(currentProject.value.id);
    await initialize(currentProject.value.id);
    setNotice(`场景已重建：${result.raw_point_count} 个基础点，${result.render_point_count} 个结构显示点。`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
    loading.value = false;
  }
}

async function handleReviewStatus(findingId: number, status: ReviewStatus) {
  setBusy("更新复核状态...");
  try {
    const response = await patchFinding(findingId, {
      review_status: status,
      needs_review: status === "pending",
    });
    findings.value = findings.value.map((finding) => {
      if (finding.id !== findingId) {
        return finding;
      }
      return {
        ...finding,
        review_status: response.review_status,
        reviewer_notes: response.reviewer_notes,
        needs_review: response.needs_review,
      };
    });
    setNotice("复核状态已更新。");
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
  }
}

async function handleSaveNotes(findingId: number, notes: string) {
  setBusy("保存复核备注...");
  try {
    const response = await patchFinding(findingId, {
      reviewer_notes: notes,
    });
    findings.value = findings.value.map((finding) => {
      if (finding.id !== findingId) {
        return finding;
      }
      return {
        ...finding,
        review_status: response.review_status,
        reviewer_notes: response.reviewer_notes,
        needs_review: response.needs_review,
      };
    });
    setNotice("备注已保存。");
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
  }
}

function fillImportSamples(force = true) {
  if (!bootstrap.value) {
    return;
  }
  if (force || !importForm.standards_dir) {
    importForm.standards_dir = bootstrap.value.sample_standards_dir ?? "";
  }
}

function handleSelectVehicle(vehicle: RtspVehicle) {
  importForm.bag_dir = vehicle.rtsp_url;
  if (!importForm.standards_dir) {
    importForm.standards_dir = bootstrap.value?.sample_standards_dir ?? "";
  }
}

function handleSelectRosbag() {
  importForm.bag_dir = bootstrap.value?.sample_scene_path || bootstrap.value?.sample_pcd_path || bootstrap.value?.sample_bag_dir || "";
  if (!importForm.standards_dir) {
    importForm.standards_dir = bootstrap.value?.sample_standards_dir ?? "";
  }
}

function handleBackToVehicleSelect() {
  importForm.bag_dir = "";
}

function clearProjectArtifacts() {
  scene.value = null;
  rules.value = [];
  findings.value = [];
  selectedFindingId.value = null;
  activeEvidenceTs.value = null;
}

function hasPlayableVideo(project: ProjectSummary): boolean {
  return Boolean(project.inspection_video_url || project.rtsp_recorded_video_url);
}

function resolveProjectId(preferredId: number | null, candidates: ProjectSummary[]) {
  const preferred =
    preferredId !== null ? candidates.find((project) => project.id === preferredId) ?? null : null;
  // Keep the user's current project even when RTSP video is not ready yet.
  if (preferred) {
    return preferred.id;
  }
  const withVideo = candidates.find(hasPlayableVideo);
  if (withVideo) {
    return withVideo.id;
  }
  return candidates[0]?.id ?? null;
}

function selectProject(projectId: number | null) {
  void openProject(projectId);
}

function selectFinding(findingId: number, timestampMs?: number) {
  if (timestampMs !== undefined && Number.isFinite(timestampMs)) {
    pendingEvidenceSeekTs.value = timestampMs;
    activeEvidenceTs.value = timestampMs;
  }
  selectedFindingId.value = findingId;
}

function requestSeek(timestampMs: number) {
  activeEvidenceTs.value = timestampMs;
}

function handlePlaybackModeChange(mode: RtspPlaybackMode, sourceStartTs: number | null) {
  const sourceChanged = mode !== videoPlaybackMode.value || sourceStartTs !== videoPlaybackSourceStartTs.value;
  videoPlaybackMode.value = mode;
  videoPlaybackSourceStartTs.value = sourceStartTs;
  if (sourceChanged) {
    selectedFindingId.value = null;
    activeEvidenceTs.value = null;
    pendingEvidenceSeekTs.value = null;
  }
}

function handlePlaybackTimeChange(timestampMs: number | null) {
  videoPlaybackTs.value = timestampMs;
}

function setBusy(label: string) {
  busyLabel.value = label;
}

function clearBusy() {
  busyLabel.value = "";
}

function getErrorMessage(error: unknown) {
  if (error instanceof Error) {
    return error.message;
  }
  return "Unexpected error";
}
</script>

<template>
  <div class="app-shell">
    <WorkbenchHeader
      :projects="projects"
      :current-project-id="currentProjectId"
      :current-project="currentProject"
      :status-label="statusLabel"
      :loading="loading"
      :rtsp-watch-test-mode="rtspWatchTestMode"
      :rtsp-watch-test-max-seconds="bootstrap?.rtsp_watch_test_max_seconds ?? 600"
      @refresh="initialize()"
      @clear-rtsp-recordings="handleClearRtspRecordings"
      @reset-runtime="handleResetRuntime"
      @update-rtsp-watch-test-mode="handleRtspWatchTestModeChange"
      @select-project="selectProject"
    />

    <section class="control-strip">
      <ImportPanel
        ref="importPanelRef"
        :bootstrap="bootstrap"
        :loading="loading"
        :name="importForm.name"
        :bag-dir="importForm.bag_dir"
        :standards-dir="importForm.standards_dir"
        @update-name="importForm.name = $event"
        @update-bag-dir="importForm.bag_dir = $event"
        @update-standards-dir="importForm.standards_dir = $event"
        @select-vehicle="handleSelectVehicle"
        @select-rosbag="handleSelectRosbag"
        @back-to-vehicle-select="handleBackToVehicleSelect"
        @import="handleImport"
      />

      <MetricsPanel
        :project="currentProject"
        :scene="scene"
        :rules-count="rules.length"
        :visual-rule-count="visualRuleCount"
      />

      <AnalysisPanel
        :project="currentProject"
        :scene="scene"
        :loading="loading"
        :live-monitor-active="isRtspLiveMonitorProject(currentProject)"
        :visual-rule-count="visualRuleCount"
        :analysis-mode="analysisMode"
        :analysis-model="analysisModel"
        :supported-models="supportedAnalysisModels"
        :provider-available="currentProject?.provider_available ?? bootstrap?.provider_available ?? false"
        :yolo-available="currentProject?.yolo_available ?? bootstrap?.yolo_available ?? false"
        @analyze="handleAnalyze"
        @rebuild-scene="handleRebuildScene"
        @update:analysis-mode="analysisMode = $event"
        @update:analysis-model="analysisModel = $event"
      />
    </section>

    <div v-if="errorMessage" class="banner banner-error">{{ errorMessage }}</div>
    <div v-if="notice" class="banner banner-success">{{ notice }}</div>

    <main class="workspace-grid">
      <section class="scene-panel">
        <div class="panel-header">
          <div>
            <p class="section-kicker">04 / 三维场景</p>
            <h2>真实点云地图与轨迹热点</h2>
          </div>
          <span class="panel-tag">
            {{
              !showSceneMap
                ? "直播中 · 回放时可查看点云"
                : scene && hasRenderableScene
                  ? `激光场景 · ${scene.hazard_zones.length} 个空间标注`
                  : "等待点云地图"
            }}
          </span>
        </div>

        <div class="scene-frame">
          <SceneViewport
            v-if="showSceneMap"
            :scene-data="hasRenderableScene ? scene : null"
            :selected-finding-id="selectedFindingId"
            :active-timestamp-ms="sceneActiveTimestampMs"
            @select="selectFinding"
            @seek="requestSeek"
          />
          <div v-else class="scene-live-placeholder">
            <p>RTSP 直播中不显示点云地图</p>
            <span>流结束后进入录制回放，可点击轨迹点同步跳转视频时间。</span>
          </div>
        </div>

        <div v-if="showSceneMap && scene?.notes?.length" class="scene-footnote">
          <p v-for="note in scene.notes" :key="note">{{ note }}</p>
        </div>
      </section>

      <section class="ops-panel">
        <VideoEvidencePanel
          :project="currentProject"
          :finding="selectedFinding"
          :requested-seek-ts="activeEvidenceTs"
          :preview-rtsp-url="previewRtspUrl"
          @request-seek="requestSeek"
          @playback-mode-change="handlePlaybackModeChange"
          @playback-time-change="handlePlaybackTimeChange"
        />

        <FindingsPanel
          :project="currentProject"
          :findings="playbackFindings"
          :selected-finding-id="selectedFindingId"
          @select="selectFinding"
          @update-review-status="handleReviewStatus"
        />

        <FindingDetailPanel
          :project="currentProject"
          :project-id="currentProjectId"
          :finding="selectedFinding"
          :requested-seek-ts="activeEvidenceTs"
          @request-seek="requestSeek"
          @save-notes="handleSaveNotes"
          @update-review-status="handleReviewStatus"
        />
      </section>
    </main>
  </div>
</template>
