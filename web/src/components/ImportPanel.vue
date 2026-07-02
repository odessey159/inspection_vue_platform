<script setup lang="ts">
import type { BootstrapResponse } from "../types";

defineProps<{
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
}>();

function emitInput(
  event: Event,
  forward: (value: string) => void,
) {
  const target = event.target as HTMLInputElement | null;
  forward(target?.value ?? "");
}
</script>

<template>
  <section class="strip-panel import-panel">
    <div class="section-head">
      <div>
        <p class="section-kicker">01 / 数据导入</p>
        <h2>导入 rosbag 与标准</h2>
      </div>
      <button class="ghost-button" @click="$emit('fillSample')">填入示例路径</button>
    </div>

    <div class="field-grid">
      <label class="field">
        <span>项目名称</span>
        <input :value="name" placeholder="例如：厂区日检 2026-03-24" @input="emitInput($event, (value) => $emit('updateName', value))" />
      </label>

      <label class="field span-2">
        <span>rosbag 目录</span>
        <input
          :value="bagDir"
          placeholder="D:\\Projets\\巡检机器人\\Analysis\\tidepilot_data_20260324_130504"
          @input="emitInput($event, (value) => $emit('updateBagDir', value))"
        />
      </label>

      <label class="field span-2">
        <span>standards 目录</span>
        <input
          :value="standardsDir"
          placeholder="D:\\Projets\\巡检机器人\\Analysis\\standards"
          @input="emitInput($event, (value) => $emit('updateStandardsDir', value))"
        />
      </label>
    </div>

    <div class="button-row">
      <button class="primary-button" :disabled="loading" @click="$emit('import')">
        {{ loading ? "正在导入..." : "开始预生成" }}
      </button>
      <span class="hint-text">
        导入阶段会直接产出 `inspection.mp4`、真实 `scene.json`、轨迹、规则摘要和证据帧索引。
      </span>
    </div>

    <div v-if="bootstrap" class="hint-grid">
      <span>检测到 rosbag 目录 {{ bootstrap.detected_bag_dirs.length }} 个</span>
      <span>检测到标准目录 {{ bootstrap.detected_standards_dirs.length }} 个</span>
    </div>
  </section>
</template>
