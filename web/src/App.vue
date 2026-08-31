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
  getVehicleScene,
  getVehicleTrajectory,
  importMap,
  importProject,
  patchFinding,
  rebuildScene,
  resetRuntime,
  clearRtspRecordings,
  updateRtspWatchTestMode,
  updateVehicleMapId,
  updateVehicleRtspUrl,
  ensureVehicleWorkspace,
} from "./lib/api";
import { alignSceneTimestampsToVideo, sceneTimelineOverlapsVideo } from "./lib/sceneTime";
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
  vehicle_id: "" as string,
});
const selectedVehicleName = ref("");
const vehicleMapStatus = ref<"idle" | "loading" | "ready" | "missing" | "error">("idle");
const vehicleMapMessage = ref("");
let vehicleSceneRequestId = 0;

const importPanelRef = ref<InstanceType<typeof ImportPanel> | null>(null);
const FINDINGS_POLL_MS = 5000;
let findingsPollTimer: number | null = null;
let findingsPollGeneration = 0;
let openProjectGeneration = 0;

const headerVehicles = computed(() => {
  const list = [...(bootstrap.value?.rtsp_vehicles ?? [])];
  const hasOffline = projects.value.some((project) => project.vehicle_id === "offline");
  if (hasOffline && !list.some((vehicle) => vehicle.id === "offline")) {
    list.push({
      id: "offline",
      name: "离线 rosbag / scene.json",
      rtsp_url: "",
    });
  }
  return list;
});

const currentVehicleId = computed(() => importForm.vehicle_id);

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

/** Show the 3D map whenever point cloud is enabled; trajectory grows from RTSP even while live. */
const showSceneMap = computed(() => {
  if (bootstrap.value && bootstrap.value.point_cloud_enabled === false) {
    return false;
  }
  return true;
});

const sceneActiveTimestampMs = computed(() => {
  if (videoPlaybackMode.value === "live") {
    const stamps = scene.value?.trajectory_timestamps ?? [];
    if (stamps.length) {
      return stamps[stamps.length - 1];
    }
  }
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
  if (currentProject.value?.rtsp_recording_active) {
    return "正在录制 RTSP";
  }
  if (currentProject.value) {
    return formatStatusLabel(currentProject.value.status);
  }
  return "等待选择小车";
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
    await refreshLiveTrajectory();
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

async function refreshLiveTrajectory() {
  const vehicleId = currentProject.value?.vehicle_id || importForm.vehicle_id;
  if (!vehicleId || !scene.value) {
    return;
  }
  try {
    const payload = await getVehicleTrajectory(vehicleId);
    if (!scene.value) {
      return;
    }
    scene.value.trajectory = payload.trajectory;
    scene.value.trajectory_timestamps = payload.trajectory_timestamps;
    scene.value.trajectory_orientations = payload.trajectory_orientations;
    scene.value.scene_quality = {
      ...(scene.value.scene_quality ?? {}),
      trajectory_source: payload.point_count ? payload.source : null,
      trajectory_point_count: payload.point_count,
    };
  } catch {
    // Keep the last overlay if the trajectory endpoint is briefly unavailable.
  }
}

async function initialize(preferredProjectId: number | null = currentProjectId.value) {
  setBusy("刷新工作区...");
  try {
    const [bootstrapPayload, projectPayload] = await Promise.all([getBootstrap(), getProjects()]);
    bootstrap.value = bootstrapPayload;
    rtspWatchTestMode.value = bootstrapPayload.rtsp_watch_test_mode;
    projects.value = projectPayload;
    fillImportSamples(false);

    const preferredVehicleId =
      importForm.vehicle_id
      || projectPayload.find((project) => project.id === preferredProjectId)?.vehicle_id
      || null;
    const nextVehicleId = resolveVehicleId(preferredVehicleId, projectPayload, bootstrapPayload.rtsp_vehicles);
    if (!nextVehicleId) {
      clearProjectArtifacts();
      currentProjectId.value = null;
      syncAnalysisSelection(null);
      errorMessage.value = "";
      return;
    }

    await openVehicleWorkspaceById(nextVehicleId);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
  }
}

async function openProject(projectId: number | null) {
  const generation = ++openProjectGeneration;
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
  if (project?.vehicle_id) {
    importForm.vehicle_id = project.vehicle_id;
  }
  syncAnalysisSelection(project);
  setBusy("加载场景与分析结果...");
  try {
    const [scenePayload, rulesPayload, findingsPayload] = await Promise.all([
      getSceneOptional(projectId, "lidar"),
      getRules(projectId),
      getFindings(projectId),
    ]);
    if (generation !== openProjectGeneration) {
      return;
    }
    scene.value = scenePayload
      ? alignSceneToProjectVideoClock(scenePayload, project)
      : null;
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
    if (generation !== openProjectGeneration) {
      return;
    }
    scene.value = null;
    rules.value = [];
    findings.value = [];
    selectedFindingId.value = null;
    setError(getErrorMessage(error));
  } finally {
    if (generation === openProjectGeneration) {
      clearBusy();
      scheduleFindingsPoll();
    }
  }
}

async function handleImport() {
  loading.value = true;
  notice.value = "";
  errorMessage.value = "";
  const source = importForm.bag_dir.trim();
  if (!source) {
    setError("请填写 rosbag 目录 / scene.json 路径。");
    loading.value = false;
    return;
  }
  if (source.toLowerCase().startsWith("rtsp://")) {
    setError("RTSP 流可用后会自动录制，无需预生成。请直接选择小车。");
    loading.value = false;
    return;
  }
  const isMapImport =
    source.toLowerCase().endsWith("scene.json")
    || source.toLowerCase().endsWith(".pcd")
    || (source.toLowerCase().endsWith(".json") && source.toLowerCase().includes("scene"));
  if (isMapImport) {
    setBusy("导入点云地图并处理...");
    try {
      const candidateVehicleId = importForm.vehicle_id.trim();
      const assignVehicleId = candidateVehicleId && candidateVehicleId !== "offline" ? candidateVehicleId : null;
      const map = await importMap({
        path: source,
        name: importForm.name.trim() || undefined,
        assign_vehicle_id: assignVehicleId,
      });
      const bootstrapPayload = await getBootstrap();
      bootstrap.value = bootstrapPayload;
      if (assignVehicleId) {
        const vehicle = bootstrapPayload.rtsp_vehicles.find((item) => item.id === assignVehicleId);
        if (vehicle) {
          await loadSelectedVehicleMap(vehicle);
        }
      }
      setNotice(`地图已导入到独立目录：${map.id}（${map.name}）。${assignVehicleId ? "已绑定到当前小车。" : "在小车上选择该 map_id 后才会显示。"}`);
    } catch (error) {
      setError(getErrorMessage(error));
    } finally {
      clearBusy();
      loading.value = false;
    }
    return;
  }
  setBusy("导入 rosbag 并预生成视频 / 规则...");
  try {
    const project = await importProject({
      name: importForm.name.trim() || "巡检车三维复核工作台",
      bag_dir: source,
      standards_dir: importForm.standards_dir.trim(),
      vehicle_id: importForm.vehicle_id.trim() || null,
      rtsp_transport: null,
    });
    await initialize(project.id);
    setNotice("导入完成，inspection.mp4 和规则摘要已生成。点云地图请单独导入并绑定 map_id。");
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
    importForm.vehicle_id = "";
    selectedVehicleName.value = "";
    vehicleMapStatus.value = "idle";
    vehicleMapMessage.value = "";
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
    setNotice(`场景已重建并写入独立地图目录：${result.notes?.at(-1) ?? `${result.render_point_count} 个显示点`}。请在小车上绑定 map_id 后显示。`);
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
  importForm.vehicle_id = vehicle.id;
  selectedVehicleName.value = vehicle.name;
  if (!importForm.standards_dir) {
    importForm.standards_dir = bootstrap.value?.sample_standards_dir ?? "";
  }
  void openVehicleWorkspace(vehicle);
}

async function saveVehicleRtspUrl(vehicleId: string, rtspUrl: string) {
  const cleaned = rtspUrl.trim();
  if (!cleaned.toLowerCase().startsWith("rtsp://")) {
    throw new Error("RTSP 地址必须以 rtsp:// 开头");
  }
  const updated = await updateVehicleRtspUrl(vehicleId, cleaned);
  const [bootstrapPayload, projectPayload] = await Promise.all([getBootstrap(), getProjects()]);
  bootstrap.value = bootstrapPayload;
  projects.value = projectPayload;
  const next = bootstrapPayload.rtsp_vehicles.find((item) => item.id === vehicleId) ?? updated;
  if (importForm.vehicle_id === vehicleId) {
    importForm.bag_dir = next.rtsp_url;
    selectedVehicleName.value = next.name;
  }
  importPanelRef.value?.applyUpdatedVehicle(next);
  return next;
}

async function handleSaveVehicleUrl(vehicleId: string, rtspUrl: string) {
  loading.value = true;
  errorMessage.value = "";
  notice.value = "";
  setBusy("保存 RTSP 地址...");
  try {
    const updated = await saveVehicleRtspUrl(vehicleId, rtspUrl);
    setNotice(`已更新 ${updated.name} 的 RTSP 地址，实时预览与后台录制会立即使用新地址。`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    clearBusy();
    loading.value = false;
  }
}

function handleSelectRosbag() {
  importForm.bag_dir = bootstrap.value?.sample_scene_path || bootstrap.value?.sample_pcd_path || bootstrap.value?.sample_bag_dir || "";
  importForm.vehicle_id = "offline";
  selectedVehicleName.value = "离线 rosbag / scene.json";
  vehicleMapStatus.value = "idle";
  vehicleMapMessage.value = "";
  vehicleSceneRequestId += 1;
  if (!importForm.standards_dir) {
    importForm.standards_dir = bootstrap.value?.sample_standards_dir ?? "";
  }
  const project = findProjectForVehicle("offline");
  if (project) {
    void openProject(project.id);
  } else {
    void openProject(null);
  }
}

function handleBackToVehicleSelect() {
  vehicleMapStatus.value = "idle";
  vehicleMapMessage.value = "";
}

function handleHeaderVehicleSelect(vehicleId: string) {
  if (!vehicleId) {
    importForm.bag_dir = "";
    importForm.vehicle_id = "";
    selectedVehicleName.value = "";
    importPanelRef.value?.resetStep();
    void openProject(null);
    return;
  }
  if (vehicleId === "offline") {
    handleSelectRosbag();
    return;
  }
  const vehicle = bootstrap.value?.rtsp_vehicles.find((item) => item.id === vehicleId);
  if (!vehicle) {
    return;
  }
  importPanelRef.value?.applyVehicle(vehicle);
  handleSelectVehicle(vehicle);
}

function findProjectForVehicle(vehicleId: string): ProjectSummary | null {
  return (
    projects.value.find((project) => project.vehicle_id === vehicleId)
    ?? projects.value.find((project) => project.point_topic === vehicleId)
    ?? null
  );
}

function resolveVehicleId(
  preferred: string | null,
  candidates: ProjectSummary[],
  vehicles: RtspVehicle[],
): string | null {
  const known = new Set(vehicles.map((vehicle) => vehicle.id));
  known.add("offline");
  if (preferred && known.has(preferred)) {
    return preferred;
  }
  const withVehicle = candidates.find((project) => project.vehicle_id && known.has(project.vehicle_id));
  return withVehicle?.vehicle_id ?? null;
}

async function openVehicleWorkspace(vehicle: RtspVehicle) {
  try {
    await ensureVehicleWorkspace(vehicle.id);
    projects.value = await getProjects();
  } catch (error) {
    setError(getErrorMessage(error));
  }
  const project = findProjectForVehicle(vehicle.id);
  if (project) {
    await openProject(project.id);
    if (!hasRenderableScene.value) {
      await loadSelectedVehicleMap(vehicle);
    }
    return;
  }
  await openProject(null);
  importForm.vehicle_id = vehicle.id;
  importForm.bag_dir = vehicle.rtsp_url;
  await loadSelectedVehicleMap(vehicle);
}

async function openVehicleWorkspaceById(vehicleId: string) {
  if (vehicleId === "offline") {
    handleSelectRosbag();
    return;
  }
  const vehicle = bootstrap.value?.rtsp_vehicles.find((item) => item.id === vehicleId);
  if (vehicle) {
    importPanelRef.value?.applyVehicle(vehicle);
    await openVehicleWorkspace(vehicle);
    return;
  }
  const project = findProjectForVehicle(vehicleId);
  if (project) {
    await openProject(project.id);
  }
}

async function loadSelectedVehicleMap(vehicle: RtspVehicle) {
  const requestId = ++vehicleSceneRequestId;
  if (!vehicle.map_id) {
    scene.value = null;
    vehicleMapStatus.value = "missing";
    vehicleMapMessage.value = `${vehicle.name} 未绑定点云地图`;
    return;
  }
  vehicleMapStatus.value = "loading";
  vehicleMapMessage.value = `正在按索引加载地图 ${vehicle.map_id}...`;
  errorMessage.value = "";
  notice.value = "";
  setBusy(`加载地图 ${vehicle.map_id}...`);
  try {
    const payload = await getVehicleScene(vehicle.id);
    if (requestId !== vehicleSceneRequestId) {
      return;
    }
    scene.value = alignSceneToProjectVideoClock(payload, currentProject.value);
    vehicleMapStatus.value = "ready";
    vehicleMapMessage.value = `已显示地图 ${vehicle.map_id}（${vehicle.name}）`;
    setNotice(vehicleMapMessage.value);
  } catch (error) {
    if (requestId !== vehicleSceneRequestId) {
      return;
    }
    scene.value = null;
    const message = getErrorMessage(error);
    const missing = /no map_id|not found|404/i.test(message);
    vehicleMapStatus.value = missing ? "missing" : "error";
    vehicleMapMessage.value = missing
      ? `${vehicle.name} 未绑定点云地图`
      : `加载地图失败：${message}`;
    if (!missing) {
      setError(vehicleMapMessage.value);
    }
  } finally {
    if (requestId === vehicleSceneRequestId) {
      clearBusy();
    }
  }
}

async function handleAssignVehicleMap(vehicleId: string, mapId: string | null) {
  loading.value = true;
  errorMessage.value = "";
  try {
    const updated = await updateVehicleMapId(vehicleId, mapId);
    const bootstrapPayload = await getBootstrap();
    bootstrap.value = bootstrapPayload;
    importPanelRef.value?.applyUpdatedVehicle(updated);
    const vehicle = bootstrapPayload.rtsp_vehicles.find((item) => item.id === vehicleId) ?? updated;
    await loadSelectedVehicleMap(vehicle);
    setNotice(mapId ? `已绑定地图 ${mapId}` : "已清除该车的地图绑定");
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    loading.value = false;
  }
}

function alignSceneToProjectVideoClock(
  payload: SceneResponse,
  project: ProjectSummary | null,
) {
  if (!payload.trajectory?.length) {
    return payload;
  }
  if (payload.scene_quality?.trajectory_source === "rtsp_sei") {
    return payload;
  }
  const startTs = project?.video_start_ts ?? project?.bag_start_ts ?? null;
  const endTs = project?.video_end_ts ?? project?.bag_end_ts ?? null;
  if (startTs == null || endTs == null || endTs <= startTs) {
    return payload;
  }
  if (sceneTimelineOverlapsVideo(payload, startTs, endTs)) {
    return payload;
  }
  return alignSceneTimestampsToVideo(payload, startTs, endTs);
}

function clearProjectArtifacts() {
  scene.value = null;
  rules.value = [];
  findings.value = [];
  selectedFindingId.value = null;
  activeEvidenceTs.value = null;
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
    videoPlaybackTs.value = null;
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
    try {
      const parsed = JSON.parse(error.message) as { detail?: unknown };
      if (typeof parsed.detail === "string" && parsed.detail.trim()) {
        return parsed.detail;
      }
    } catch {
      return error.message;
    }
    return error.message;
  }
  return "Unexpected error";
}
</script>

<template>
  <div class="app-shell">
    <WorkbenchHeader
      :vehicles="headerVehicles"
      :current-vehicle-id="currentVehicleId"
      :current-project="currentProject"
      :status-label="statusLabel"
      :loading="loading"
      :rtsp-watch-test-mode="rtspWatchTestMode"
      :rtsp-watch-test-max-seconds="bootstrap?.rtsp_watch_test_max_seconds ?? 600"
      @refresh="initialize()"
      @clear-rtsp-recordings="handleClearRtspRecordings"
      @reset-runtime="handleResetRuntime"
      @update-rtsp-watch-test-mode="handleRtspWatchTestModeChange"
      @select-vehicle="handleHeaderVehicleSelect"
    />

    <section class="control-strip">
      <ImportPanel
        ref="importPanelRef"
        :bootstrap="bootstrap"
        :loading="loading"
        :name="importForm.name"
        :bag-dir="importForm.bag_dir"
        :standards-dir="importForm.standards_dir"
        :selected-vehicle-id="importForm.vehicle_id"
        :selected-vehicle-name="selectedVehicleName"
        :vehicle-map-status="vehicleMapStatus"
        :vehicle-map-message="vehicleMapMessage"
        :project-id="currentProjectId"
        :rtsp-recording-active="Boolean(currentProject?.rtsp_recording_active)"
        :rtsp-stream-online="Boolean(currentProject?.rtsp_stream_online)"
        @update-name="importForm.name = $event"
        @update-bag-dir="importForm.bag_dir = $event"
        @update-standards-dir="importForm.standards_dir = $event"
        @select-vehicle="handleSelectVehicle"
        @select-rosbag="handleSelectRosbag"
        @back-to-vehicle-select="handleBackToVehicleSelect"
        @save-vehicle-url="handleSaveVehicleUrl"
        @assign-vehicle-map="handleAssignVehicleMap"
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
              scene && hasRenderableScene
                ? importForm.vehicle_id && !currentProject
                  ? `车端地图 · ${selectedVehicleName || importForm.vehicle_id}`
                  : `激光场景 · 轨迹 ${scene.trajectory.length} 点 · ${scene.hazard_zones.length} 个空间标注`
                : vehicleMapStatus === "loading"
                  ? "正在加载车端地图"
                  : vehicleMapStatus === "missing"
                    ? "未绑定点云地图"
                    : "等待点云地图"
            }}
          </span>
        </div>

        <div class="scene-frame">
          <SceneViewport
            v-if="showSceneMap && hasRenderableScene"
            :scene-data="scene"
            :selected-finding-id="selectedFindingId"
            :active-timestamp-ms="sceneActiveTimestampMs"
            @select="selectFinding"
            @seek="requestSeek"
          />
          <div v-else-if="showSceneMap" class="scene-live-placeholder">
            <p>该车未绑定点云地图</p>
            <span>点云地图是独立资产。导入后在小车上选择 map_id 才会显示，没有地图不影响巡检。轨迹来自 RTSP 坐标，不会预先画在地图上。</span>
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
