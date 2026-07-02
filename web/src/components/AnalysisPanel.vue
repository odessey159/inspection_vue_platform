<script setup lang="ts">
import { computed } from "vue";
import { formatStatusLabel } from "../lib/format";
import type { AnalysisMode, ProjectSummary, SceneResponse } from "../types";

const TEXT = {
  sectionKicker: "03 / \u5bf9\u9f50\u4e0e\u5206\u6790",
  titleNeedConfirm: "\u9700\u8981\u786e\u8ba4\u65f6\u95f4\u504f\u79fb",
  titleReadyAnalyze: "\u53ef\u76f4\u63a5\u6267\u884c\u79bb\u7ebf\u5206\u6790",
  noProjectSelected: "\u672a\u9009\u62e9\u9879\u76ee",
  analyzableRules: "\u53ef\u5206\u6790\u89c4\u5219",
  ruleSuffix: "\u6761",
  sceneArtifact: "\u573a\u666f\u4ea7\u7269",
  waitingScene: "\u7b49\u5f85 scene.json \u751f\u6210",
  analysisMode: "\u5206\u6790\u6a21\u5f0f",
  providerOption: "Provider / \u767e\u70bc",
  demoOption: "Demo",
  analysisModel: "\u767e\u70bc\u6a21\u578b",
  noModels: "\u6682\u65e0\u53ef\u9009\u6a21\u578b",
  running: "\u6b63\u5728\u5206\u6790...",
  runProvider: "\u8fd0\u884c Provider \u5206\u6790",
  runDemo: "\u8fd0\u884c Demo \u5206\u6790",
  rebuilding: "\u573a\u666f\u5904\u7406\u4e2d...",
  rebuildScene: "\u91cd\u5efa\u5168\u91cf LiDAR \u7ed3\u6784\u573a\u666f",
} as const;

const props = defineProps<{
  project: ProjectSummary | null;
  scene: SceneResponse | null;
  loading: boolean;
  visualRuleCount: number;
  analysisMode: AnalysisMode;
  analysisModel: string;
  supportedModels: string[];
  providerAvailable: boolean;
}>();

const emit = defineEmits<{
  analyze: [];
  rebuildScene: [];
  "update:analysisMode": [mode: AnalysisMode];
  "update:analysisModel": [model: string];
}>();

const canAnalyze = computed(() => {
  return Boolean(props.project && props.scene && props.scene.points.length > 0 && !props.loading);
});
const selectedModel = computed(() => props.analysisModel || props.supportedModels[0] || "qwen3.5-plus");
const runButtonLabel = computed(() => {
  if (props.loading) {
    return TEXT.running;
  }
  return props.analysisMode === "provider" ? TEXT.runProvider : TEXT.runDemo;
});
const rebuildButtonLabel = computed(() => {
  return props.loading ? TEXT.rebuilding : TEXT.rebuildScene;
});

function onModeChange(event: Event) {
  const target = event.target as HTMLSelectElement | null;
  emit("update:analysisMode", target?.value === "provider" ? "provider" : "demo");
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
        <h2>{{ project?.calibration_required ? TEXT.titleNeedConfirm : TEXT.titleReadyAnalyze }}</h2>
      </div>
      <span class="panel-tag">{{ project ? formatStatusLabel(project.status) : TEXT.noProjectSelected }}</span>
    </div>

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
          <option value="provider" :disabled="!providerAvailable">{{ TEXT.providerOption }}</option>
          <option value="demo">{{ TEXT.demoOption }}</option>
        </select>
      </label>
      <label class="field compact-field">
        <span>{{ TEXT.analysisModel }}</span>
        <select
          :value="selectedModel"
          :disabled="!providerAvailable || analysisMode !== 'provider' || supportedModels.length === 0"
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
  </section>
</template>
