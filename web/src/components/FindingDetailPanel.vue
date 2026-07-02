<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { frameUrl } from "../lib/api";
import { formatAnalysisMode, formatCoordinate, formatPercent, formatRelativeSeconds } from "../lib/format";
import type { FindingResponse, ProjectSummary, ReviewStatus } from "../types";

const props = defineProps<{
  project: ProjectSummary | null;
  projectId: number | null;
  finding: FindingResponse | null;
  requestedSeekTs: number | null;
}>();

const emit = defineEmits<{
  requestSeek: [timestampMs: number];
  saveNotes: [findingId: number, notes: string];
  updateReviewStatus: [findingId: number, status: ReviewStatus];
}>();

const noteDraft = ref("");

const evidenceFrames = computed(() => {
  if (!props.finding) {
    return [];
  }
  return Array.from(new Set(props.finding.evidence_frame_ts)).sort((left, right) => left - right);
});

watch(
  () => props.finding?.id,
  () => {
    noteDraft.value = props.finding?.reviewer_notes ?? "";
  },
  { immediate: true },
);

function onSaveNotes() {
  if (!props.finding) {
    return;
  }
  emit("saveNotes", props.finding.id, noteDraft.value);
}
</script>

<template>
  <section class="detail-panel">
    <div class="panel-header compact-headline">
      <div>
        <p class="section-kicker">07 / 证据与依据</p>
        <h2>选中隐患详情</h2>
      </div>
    </div>

    <div v-if="finding" class="detail-body">
      <div class="detail-copy">
        <div class="finding-topline">
          <span class="severity-chip" :data-severity="finding.severity">{{ finding.severity }}</span>
          <span class="review-chip" :data-review="finding.review_status">{{ finding.review_status }}</span>
          <span class="confidence-chip">{{ formatPercent(finding.confidence) }}</span>
        </div>

        <h3>{{ finding.title }}</h3>
        <p>{{ finding.description }}</p>

        <dl class="detail-grid">
          <div>
            <dt>规则编号</dt>
            <dd>{{ finding.rule_id }}</dd>
          </div>
          <div>
            <dt>分析模式</dt>
            <dd>{{ formatAnalysisMode(finding.analysis_mode) }}</dd>
          </div>
          <div>
            <dt>时间范围</dt>
            <dd>{{ formatRelativeSeconds(project, finding.time_start_ms) }} - {{ formatRelativeSeconds(project, finding.time_end_ms) }}</dd>
          </div>
          <div>
            <dt>空间坐标</dt>
            <dd>{{ formatCoordinate(finding.zone?.center ?? null) }}</dd>
          </div>
          <div>
            <dt>巡检范围</dt>
            <dd>{{ finding.checker_scope || "未注明" }}</dd>
          </div>
          <div>
            <dt>证据帧数</dt>
            <dd>{{ evidenceFrames.length }}</dd>
          </div>
        </dl>

        <div class="detail-section">
          <span class="detail-label">隐患描述</span>
          <p>{{ finding.hazard_desc || "暂无隐患描述" }}</p>
        </div>

        <div class="detail-section">
          <span class="detail-label">法规依据</span>
          <p>{{ finding.legal_basis || "暂无法规依据" }}</p>
        </div>
      </div>

      <div v-if="projectId !== null && evidenceFrames.length" class="thumb-strip">
        <button
          v-for="timestamp in evidenceFrames"
          :key="timestamp"
          class="thumb-card"
          :class="{ active: timestamp === requestedSeekTs }"
          @click="$emit('requestSeek', timestamp)"
        >
          <img :src="frameUrl(projectId, timestamp)" :alt="`evidence-${timestamp}`" />
          <span>{{ formatRelativeSeconds(project, timestamp) }}</span>
        </button>
      </div>

      <label class="field notes-field">
        <span>复核备注</span>
        <textarea v-model="noteDraft" rows="4" placeholder="记录现场复核意见、需要复拍的位置或人工确认结论..." />
      </label>

      <div class="button-row stacked-actions">
        <button class="secondary-button" @click="onSaveNotes">保存备注</button>
        <button class="ghost-button" @click="$emit('updateReviewStatus', finding.id, 'confirmed')">确认</button>
        <button class="ghost-button" @click="$emit('updateReviewStatus', finding.id, 'rejected')">驳回</button>
        <button class="ghost-button" @click="$emit('updateReviewStatus', finding.id, 'pending')">标记待定</button>
      </div>
    </div>

    <div v-else class="empty-box slim">
      从三维场景或隐患列表中选择一项后，这里会展示规则依据、证据帧和复核备注。
    </div>
  </section>
</template>
