<script setup lang="ts">
import { computed, ref } from "vue";
import { formatAnalysisMode, formatPercent, formatRelativeSeconds } from "../lib/format";
import type { FindingResponse, ProjectSummary, ReviewStatus } from "../types";

const props = defineProps<{
  findings: FindingResponse[];
  selectedFindingId: number | null;
  project: ProjectSummary | null;
}>();

const emit = defineEmits<{
  select: [findingId: number];
  updateReviewStatus: [findingId: number, status: ReviewStatus];
}>();

const severityFilter = ref("all");
const reviewFilter = ref("all");
const query = ref("");
const minSecond = ref("");
const maxSecond = ref("");

const filteredFindings = computed(() => {
  return props.findings.filter((finding) => {
    if (severityFilter.value !== "all" && finding.severity !== severityFilter.value) {
      return false;
    }
    if (reviewFilter.value !== "all" && finding.review_status !== reviewFilter.value) {
      return false;
    }

    const keyword = query.value.trim().toLowerCase();
    if (keyword) {
      const haystack = `${finding.title} ${finding.hazard_desc} ${finding.category} ${finding.checker_scope}`.toLowerCase();
      if (!haystack.includes(keyword)) {
        return false;
      }
    }

    if (props.project?.video_start_ts) {
      const relativeSeconds = (finding.time_start_ms - props.project.video_start_ts) / 1000;
      const minValue = minSecond.value ? Number(minSecond.value) : null;
      const maxValue = maxSecond.value ? Number(maxSecond.value) : null;
      if (minValue !== null && Number.isFinite(minValue) && relativeSeconds < minValue) {
        return false;
      }
      if (maxValue !== null && Number.isFinite(maxValue) && relativeSeconds > maxValue) {
        return false;
      }
    }

    return true;
  });
});
</script>

<template>
  <section class="findings-panel">
    <div class="panel-header compact-headline">
      <div>
        <p class="section-kicker">06 / 筛选与复核</p>
        <h2>隐患列表</h2>
      </div>
      <span class="panel-tag">{{ filteredFindings.length }} / {{ findings.length }}</span>
    </div>

    <div class="filters-grid">
      <label class="field">
        <span>严重级别</span>
        <select v-model="severityFilter">
          <option value="all">全部</option>
          <option value="critical">critical</option>
          <option value="high">high</option>
          <option value="medium">medium</option>
        </select>
      </label>

      <label class="field">
        <span>复核状态</span>
        <select v-model="reviewFilter">
          <option value="all">全部</option>
          <option value="pending">pending</option>
          <option value="confirmed">confirmed</option>
          <option value="rejected">rejected</option>
        </select>
      </label>

      <label class="field">
        <span>关键词</span>
        <input v-model="query" placeholder="设备、消防、通道..." />
      </label>

      <label class="field inline-field">
        <span>时间段 (s)</span>
        <div class="inline-pair">
          <input v-model="minSecond" placeholder="起始" />
          <input v-model="maxSecond" placeholder="结束" />
        </div>
      </label>
    </div>

    <div class="finding-list">
      <article
        v-for="finding in filteredFindings"
        :key="finding.id"
        class="finding-card"
        :class="{ active: finding.id === selectedFindingId }"
        @click="$emit('select', finding.id)"
      >
        <div class="finding-topline">
          <span class="severity-chip" :data-severity="finding.severity">{{ finding.severity }}</span>
          <span class="review-chip" :data-review="finding.review_status">{{ finding.review_status }}</span>
          <span class="confidence-chip">{{ formatPercent(finding.confidence) }}</span>
        </div>

        <h3>{{ finding.title }}</h3>
        <p>{{ finding.hazard_desc }}</p>

        <div class="finding-meta">
          <span>{{ formatRelativeSeconds(project, finding.time_start_ms) }} - {{ formatRelativeSeconds(project, finding.time_end_ms) }}</span>
          <span>{{ finding.category || "未分类" }}</span>
          <span>{{ formatAnalysisMode(finding.analysis_mode) }}</span>
        </div>

        <div class="review-actions">
          <button class="ghost-button" @click.stop="$emit('updateReviewStatus', finding.id, 'confirmed')">确认</button>
          <button class="ghost-button" @click.stop="$emit('updateReviewStatus', finding.id, 'rejected')">驳回</button>
          <button class="ghost-button" @click.stop="$emit('updateReviewStatus', finding.id, 'pending')">待定</button>
        </div>
      </article>

      <div v-if="filteredFindings.length === 0" class="empty-box slim">
        当前筛选条件下没有可显示的隐患记录。
      </div>
    </div>
  </section>
</template>
