<script setup lang="ts">
import { computed } from "vue";
import { formatCompactNumber, formatDateTime } from "../lib/format";
import type { ProjectSummary, SceneResponse } from "../types";

const props = defineProps<{
  project: ProjectSummary | null;
  scene: SceneResponse | null;
  rulesCount: number;
  visualRuleCount: number;
}>();

const metricTiles = computed(() => {
  if (!props.project) {
    return [];
  }

  const quality = props.scene?.scene_quality ?? {};
  const usedFrames = numberFromQuality(quality.used_frame_count) ?? props.scene?.source_frame_count ?? null;
  const inputFrames = numberFromQuality(quality.input_frame_count);
  const frameValue = inputFrames ? `${formatCompactNumber(usedFrames)} / ${formatCompactNumber(inputFrames)}` : formatCompactNumber(usedFrames);

  return [
    {
      label: "场景来源",
      value: "激光场景",
      hint: props.scene?.source_type ?? "等待 scene.json 生成",
    },
    {
      label: "累计帧数",
      value: frameValue,
      hint: "已参与建图 / 数据包可用 LiDAR 帧",
    },
    {
      label: "结构显示点",
      value: props.scene ? `${formatCompactNumber(props.scene.structure_point_count || props.scene.render_point_count)} pts` : "--",
      hint: props.scene ? `基础 roof-off 点 ${formatCompactNumber(props.scene.raw_point_count)}` : "等待场景生成",
    },
    {
      label: "体素精度",
      value: formatMeters(quality.base_voxel_size_m),
      hint: `显示体素 ${formatMeters(quality.structure_voxel_size_m)}`,
    },
    {
      label: "视频时间窗",
      value: formatDateTime(props.project.video_start_ts),
      hint: `至 ${formatDateTime(props.project.video_end_ts)}`,
    },
    {
      label: "点云时间窗",
      value: formatDateTime(props.project.point_start_ts),
      hint: `至 ${formatDateTime(props.project.point_end_ts)}`,
    },
    {
      label: "轨迹采样",
      value: props.scene ? formatCompactNumber(props.scene.trajectory.length) : "--",
      hint: props.project.pose_topic ?? "位姿来源待确认",
    },
    {
      label: "规则覆盖",
      value: `${props.rulesCount} / ${props.visualRuleCount}`,
      hint: "总规则数 / 可视觉检测规则",
    },
    {
      label: "切顶高度",
      value: props.scene ? `${props.scene.cut_height_default.toFixed(2)} m` : "--",
      hint: `去顶比例 ${formatRatio(quality.roof_removed_ratio)}`,
    },
    {
      label: "状态刷新",
      value: formatIsoDateTime(props.project.updated_at),
      hint: `分析结果 ${formatIsoDateTime(props.project.analysis_updated_at)}`,
    },
  ];
});

function numberFromQuality(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatMeters(value: unknown) {
  const numeric = numberFromQuality(value);
  return numeric === null ? "--" : `${numeric.toFixed(3)} m`;
}

function formatRatio(value: unknown) {
  const numeric = numberFromQuality(value);
  return numeric === null ? "--" : `${Math.round(numeric * 100)}%`;
}

function formatIsoDateTime(value: string | null | undefined) {
  if (!value) {
    return "--";
  }
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? value : formatDateTime(timestamp);
}
</script>

<template>
  <section class="strip-panel metrics-panel">
    <div class="section-head compact-head">
      <div>
        <p class="section-kicker">02 / 项目概览</p>
        <h2>全量点云与结构层指标</h2>
      </div>
    </div>

    <div v-if="project" class="metric-grid">
      <article v-for="tile in metricTiles" :key="tile.label" class="metric-tile">
        <span>{{ tile.label }}</span>
        <strong>{{ tile.value }}</strong>
        <small>{{ tile.hint }}</small>
      </article>
    </div>

    <div v-else class="empty-box slim">
      选择一个项目后，这里会显示 LiDAR 累计帧数、结构显示点、体素精度、轨迹采样和规则覆盖情况。
    </div>
  </section>
</template>
