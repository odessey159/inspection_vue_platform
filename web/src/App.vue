<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
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
  getScene,
  importProject,
  patchFinding,
  rebuildScene,
  resetRuntime,
} from "./lib/api";
import { formatStatusLabel } from "./lib/format";
import type {
  AnalysisMode,
  BootstrapResponse,
  FindingResponse,
  ProjectSummary,
  ReviewStatus,
  RuleResponse,
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
const loading = ref(false);
const busyLabel = ref("");
const errorMessage = ref("");
const notice = ref("");
const analysisMode = ref<AnalysisMode>("demo");
const analysisModel = ref("qwen3.5-plus");

const importForm = reactive({
  name: "巡检车三维复核工作台",
  bag_dir: "",
  standards_dir: "",
});

const currentProject = computed(() => {
  return projects.value.find((project) => project.id === currentProjectId.value) ?? null;
});

const selectedFinding = computed(() => {
  if (selectedFindingId.value === null) {
    return null;
  }
  return findings.value.find((finding) => finding.id === selectedFindingId.value) ?? null;
});

const visualRuleCount = computed(() => {
  return rules.value.filter((rule) => rule.visual_detectable).length;
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
  selectedFinding,
  (finding) => {
    if (pendingEvidenceSeekTs.value !== null) {
      activeEvidenceTs.value = pendingEvidenceSeekTs.value;
      pendingEvidenceSeekTs.value = null;
      return;
    }
    activeEvidenceTs.value = finding?.time_start_ms ?? null;
  },
  { immediate: true },
);

onMounted(() => {
  void initialize();
});

async function initialize(preferredProjectId: number | null = currentProjectId.value) {
  setBusy("刷新项目索引...");
  try {
    const [bootstrapPayload, projectPayload] = await Promise.all([getBootstrap(), getProjects()]);
    bootstrap.value = bootstrapPayload;
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
    errorMessage.value = getErrorMessage(error);
  } finally {
    clearBusy();
  }
}

async function openProject(projectId: number | null) {
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
      getScene(projectId, "lidar"),
      getRules(projectId),
      getFindings(projectId),
    ]);
    scene.value = scenePayload;
    rules.value = rulesPayload;
    findings.value = findingsPayload;

    if (!selectedFindingId.value || !findingsPayload.some((finding) => finding.id === selectedFindingId.value)) {
      selectedFindingId.value = findingsPayload[0]?.id ?? null;
    }
    errorMessage.value = "";
  } catch (error) {
    clearProjectArtifacts();
    selectedFindingId.value = null;
    errorMessage.value = getErrorMessage(error);
  } finally {
    clearBusy();
  }
}

async function handleImport() {
  loading.value = true;
  notice.value = "";
  errorMessage.value = "";
  setBusy("导入 rosbag 并预生成视频 / 场景...");
  try {
    const project = await importProject({
      name: importForm.name.trim() || "巡检车三维复核工作台",
      bag_dir: importForm.bag_dir.trim(),
      standards_dir: importForm.standards_dir.trim(),
    });
    notice.value = "导入完成，inspection.mp4、scene.json 和规则摘要已生成。";
    await initialize(project.id);
  } catch (error) {
    errorMessage.value = getErrorMessage(error);
  } finally {
    clearBusy();
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
    await initialize(null);
    notice.value = `已清空 .runtime，删除 ${result.removed_project_dirs} 个项目目录，释放 ${(result.removed_bytes / (1024 * 1024)).toFixed(1)} MB。`;
  } catch (error) {
    errorMessage.value = getErrorMessage(error);
  } finally {
    clearBusy();
    loading.value = false;
  }
}

async function handleAnalyze() {
  if (!currentProject.value) {
    return;
  }
  loading.value = true;
  notice.value = "";
  errorMessage.value = "";
  const projectId = currentProject.value.id;
  const requestedMode = analysisMode.value;
  const requestedModel = requestedMode === "provider" ? analysisModel.value : null;
  setBusy(requestedMode === "provider" ? "运行 Provider 视频隐患识别..." : "运行 Demo 离线分析...");
  try {
    await analyzeProject(projectId, requestedMode, requestedModel);
    await initialize(projectId);
    const refreshed = projects.value.find((project) => project.id === projectId) ?? null;
    if (requestedMode === "provider") {
      if (refreshed?.status === "provider_failed") {
        notice.value = `Provider 分析结束，但未成功完成任何切片。当前模型：${requestedModel}`;
      } else if ((refreshed?.findings_count ?? 0) > 0) {
        notice.value = `Provider 分析完成，已使用 ${requestedModel} 同步隐患列表、视频时间轴和三维空间标注。`;
      } else {
        notice.value = `Provider 分析完成，已使用 ${requestedModel} 扫描，当前未识别到明确可见隐患。`;
      }
    } else {
      notice.value = "Demo 分析完成，工作台联动已更新。";
    }
  } catch (error) {
    errorMessage.value = getErrorMessage(error);
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
    notice.value = `场景已重建：${result.raw_point_count} 个基础点，${result.render_point_count} 个结构显示点。`;
  } catch (error) {
    errorMessage.value = getErrorMessage(error);
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
    notice.value = "复核状态已更新。";
  } catch (error) {
    errorMessage.value = getErrorMessage(error);
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
    notice.value = "备注已保存。";
  } catch (error) {
    errorMessage.value = getErrorMessage(error);
  } finally {
    clearBusy();
  }
}

function fillImportSamples(force = true) {
  if (!bootstrap.value) {
    return;
  }
  if (force || !importForm.bag_dir) {
    importForm.bag_dir = bootstrap.value.sample_bag_dir ?? "";
  }
  if (force || !importForm.standards_dir) {
    importForm.standards_dir = bootstrap.value.sample_standards_dir ?? "";
  }
}

function clearProjectArtifacts() {
  scene.value = null;
  rules.value = [];
  findings.value = [];
  selectedFindingId.value = null;
  activeEvidenceTs.value = null;
}

function resolveProjectId(preferredId: number | null, candidates: ProjectSummary[]) {
  if (preferredId !== null && candidates.some((project) => project.id === preferredId)) {
    return preferredId;
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

function setBusy(label: string) {
  busyLabel.value = label;
}

function clearBusy() {
  busyLabel.value = "";
}

function syncAnalysisSelection(project: ProjectSummary | null) {
  if (!project) {
    analysisMode.value = bootstrap.value?.provider_available ? "provider" : "demo";
    analysisModel.value = bootstrap.value?.default_analysis_model ?? supportedAnalysisModels.value[0] ?? "qwen3.5-plus";
    return;
  }

  analysisMode.value = project.provider_available ? "provider" : "demo";

  const preferredModel = project.analysis_model ?? bootstrap.value?.default_analysis_model ?? supportedAnalysisModels.value[0] ?? "qwen3.5-plus";
  if (supportedAnalysisModels.value.length === 0 || supportedAnalysisModels.value.includes(preferredModel)) {
    analysisModel.value = preferredModel;
  } else {
    analysisModel.value = supportedAnalysisModels.value[0];
  }
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
      @refresh="initialize()"
      @reset-runtime="handleResetRuntime"
      @select-project="selectProject"
    />

    <section class="control-strip">
      <ImportPanel
        :bootstrap="bootstrap"
        :loading="loading"
        :name="importForm.name"
        :bag-dir="importForm.bag_dir"
        :standards-dir="importForm.standards_dir"
        @update-name="importForm.name = $event"
        @update-bag-dir="importForm.bag_dir = $event"
        @update-standards-dir="importForm.standards_dir = $event"
        @fill-sample="fillImportSamples(true)"
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
        :visual-rule-count="visualRuleCount"
        :analysis-mode="analysisMode"
        :analysis-model="analysisModel"
        :supported-models="supportedAnalysisModels"
        :provider-available="currentProject?.provider_available ?? bootstrap?.provider_available ?? false"
        @analyze="handleAnalyze"
        @rebuild-scene="handleRebuildScene"
        @update:analysis-mode="analysisMode = $event"
        @update:analysis-model="analysisModel = $event"
      />
    </section>

    <div v-if="errorMessage" class="banner banner-error">{{ errorMessage }}</div>
    <div v-else-if="notice" class="banner banner-success">{{ notice }}</div>

    <main class="workspace-grid">
      <section class="scene-panel">
        <div class="panel-header">
          <div>
            <p class="section-kicker">04 / 三维场景</p>
            <h2>真实点云地图与轨迹热点</h2>
          </div>
          <span class="panel-tag">
            {{ scene ? `激光场景 · ${scene.hazard_zones.length} 个空间标注` : "等待场景生成" }}
          </span>
        </div>

        <div class="scene-frame">
          <SceneViewport
            :scene-data="scene"
            :selected-finding-id="selectedFindingId"
            @select="selectFinding"
            @seek="requestSeek"
          />
        </div>

        <div v-if="scene?.notes?.length" class="scene-footnote">
          <p v-for="note in scene.notes" :key="note">{{ note }}</p>
        </div>
      </section>

      <section class="ops-panel">
        <VideoEvidencePanel
          :project="currentProject"
          :finding="selectedFinding"
          :requested-seek-ts="activeEvidenceTs"
          @request-seek="requestSeek"
        />

        <FindingsPanel
          :project="currentProject"
          :findings="findings"
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
