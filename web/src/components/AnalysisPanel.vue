<script setup lang="ts">
import { computed } from "vue";
import {
  analysisRunButtonLabel,
  isAnalysisModeOptionDisabled,
  isProviderModelSelectionDisabled,
  parseAnalysisMode,
  type ProviderAvailability,
} from "../lib/analysisMode";
import { formatStatusLabel } from "../lib/format";
import type { AnalysisMode, ProjectSummary, SceneResponse } from "../types";

const TEXT = {
  sectionKicker: "03 / \u5bf9\u9f50\u4e0e\u5206\u6790",
  titleNeedConfirm: "\u9700\u8981\u786e\u8ba4\u65f6\u95f4\u504f\u79fb",
  titleReadyAnalyze: "\u79bb\u7ebf\u5206\u6790\uff08\u652f\u7ebf\uff09",
  liveMonitorHint: "RTSP \u4e3b\u7ebf\uff1a\u5b9e\u65f6 YOLO monitor \u6bb5\u5185\u81ea\u52a8\u9001\u5927\u6a21\u578b\uff0c\u9690\u60a3\u4f1a\u81ea\u52a8\u5237\u65b0",
  noProjectSelected: "\u672a\u9009\u62e9\u9879\u76ee",
  analyzableRules: "\u53ef\u5206\u6790\u89c4\u5219",
  ruleSuffix: "\u6761",
  sceneArtifact: "\u573a\u666f\u4ea7\u7269",
  waitingScene: "\u7b49\u5f85 scene.json \u751f\u6210",
  analysisMode: "\u5206\u6790\u6a21\u5f0f",
  providerOption: "Provider / \u767e\u70bc",
  providerYoloOption: "YOLO + Provider",
  demoOption: "Demo",
  analysisModel: "\u767e\u70bc\u6a21\u578b",
  noModels: "\u6682\u65e0\u53ef\u9009\u6a21\u578b",
  rebuilding: "\u573a\u666f\u5904\u7406\u4e2d...",
  rebuildScene: "\u91cd\u5efa\u5168\u91cf LiDAR \u7ed3\u6784\u573a\u666f",
} as const;

const props = defineProps<{
  project: ProjectSummary | null;
  scene: SceneResponse | null;
  loading: boolean;
  liveMonitorActive?: boolean;
  visualRuleCount: number;
  analysisMode: AnalysisMode;
  analysisModel: string;
  supportedModels: string[];
  providerAvailable: boolean;
  yoloAvailable: boolean;
}>();

const emit = defineEmits<{
  analyze: [];
  rebuildScene: [];
  "update:analysisMode": [mode: AnalysisMode];
  "update:analysisModel": [model: string];
}>();

const canAnalyze = computed(() => {
  return Boolean(
    props.project
    && props.scene
    && props.scene.points.length > 0
    && !props.loading
    && !props.liveMonitorActive,
  );
});
const isRtspProject = computed(() => {
  return Boolean(props.project?.bag_dir.trim().toLowerCase().startsWith("rtsp://"));
});
const panelTitle = computed(() => {
  if (props.project?.calibration_required) {
    return TEXT.titleNeedConfirm;
  }
  return isRtspProject.value ? TEXT.titleReadyAnalyze : "可直接执行离线分析";
});
const selectedModel = computed(() => props.analysisModel || props.supportedModels[0] || "qwen3.5-plus");
const providerAvailability = computed<ProviderAvailability>(() => ({
  provider_available: props.providerAvailable,
  yolo_available: props.yoloAvailable,
}));
const isModelSelectDisabled = computed(() =>
  isProviderModelSelectionDisabled(props.analysisMode, providerAvailability.value, props.supportedModels.length),
);
const runButtonLabel = computed(() => {
  if (props.liveMonitorActive) {
    return "实时监控中，暂不可离线分析";
  }
  return analysisRunButtonLabel(props.analysisMode, props.loading);
});
const rebuildButtonLabel = computed(() => {
  return props.loading ? TEXT.rebuilding : TEXT.rebuildScene;
});


function isModeDisabled(mode: AnalysisMode): boolean {
  return isAnalysisModeOptionDisabled(mode, providerAvailability.value);
}

function onModeChange(event: Event) {
  const target = event.target as HTMLSelectElement | null;
  const mode = parseAnalysisMode(target?.value);
  if (mode) {
    emit("update:analysisMode", mode);
  }
}

function onModelChange(event: Event) {
  const target = event.target as HTMLSelectElement | null;
  if (target?.value) {
    emit("update:analysisModel", target.value);
  }
}
</script>

<template>
  <section class="strip-panel analysis-panel" :class="{ 'warn-panel': project?.calibration_required }">
    <div class="section-head compact-head">
      <div>
        <p class="section-kicker">{{ TEXT.sectionKicker }}</p>
        <h2>{{ panelTitle }}</h2>
      </div>
      <span class="panel-tag">{{ project ? formatStatusLabel(project.status) : TEXT.noProjectSelected }}</span>
    </div>

    <p v-if="isRtspProject" class="analysis-footnote">{{ TEXT.liveMonitorHint }}</p>

    <div v-if="project" class="analysis-stack">
      <div class="analysis-row">
        <span>{{ TEXT.analyzableRules }}</span>
        <strong>{{ visualRuleCount }} {{ TEXT.ruleSuffix }}</strong>
      </div>
      <div class="analysis-row">
        <span>{{ TEXT.sceneArtifact }}</span>
        <strong>{{ scene?.source_type ?? TEXT.waitingScene }}</strong>
      </div>
    </div>

    <div class="analysis-mode-row">
      <label class="field compact-field">
        <span>{{ TEXT.analysisMode }}</span>
        <select :value="analysisMode" @change="onModeChange">
          <option value="provider_yolo" :disabled="isModeDisabled('provider_yolo')">{{ TEXT.providerYoloOption }}</option>
          <option value="provider" :disabled="isModeDisabled('provider')">{{ TEXT.providerOption }}</option>
          <option value="demo">{{ TEXT.demoOption }}</option>
        </select>
      </label>
      <label class="field compact-field">
        <span>{{ TEXT.analysisModel }}</span>
        <select
          :value="selectedModel"
          :disabled="isModelSelectDisabled"
          @change="onModelChange"
        >
          <option v-if="supportedModels.length === 0" value="">{{ TEXT.noModels }}</option>
          <option v-for="model in supportedModels" :key="model" :value="model">
            {{ model }}
          </option>
        </select>
      </label>
    </div>

    <div class="button-row">
      <button class="primary-button" :disabled="!canAnalyze" @click="$emit('analyze')">
        {{ runButtonLabel }}
      </button>
      <button class="secondary-button" :disabled="!project || loading" @click="$emit('rebuildScene')">
        {{ rebuildButtonLabel }}
      </button>
    </div>

    <div v-if="project?.status === 'provider_failed' && project.analysis_diagnostics.length" class="analysis-footnote">
      <p v-for="note in project.analysis_diagnostics" :key="note">{{ note }}</p>
    </div>
  </section>
</template>
