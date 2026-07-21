<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { formatCompactNumber } from "../lib/format";
import type { ScenePoint, SceneResponse, ZoneResponse } from "../types";

type ViewPreset = "perspective" | "top";
type ColorMode = "structure" | "height" | "intensity" | "inverseIntensityRainbow" | "rgb";
type SelectableKind = "trajectoryPoint" | "hazardPathPoint";

type Bounds = {
  min: [number, number, number];
  max: [number, number, number];
};

type DisplayPoint = {
  x: number;
  y: number;
  z: number;
  intensity: number;
  density: number;
  heightT: number;
  intensityT: number;
  densityT: number;
  edgeT: number;
  scalar: number;
  shade: number;
  rgb?: [number, number, number];
};

type PointBucket = {
  ix: number;
  iy: number;
  iz: number;
  sumX: number;
  sumY: number;
  sumZ: number;
  sumI: number;
  sumR: number;
  sumG: number;
  sumB: number;
  count: number;
  colorHits: number;
};

type PointSelection = {
  sourcePoints: ScenePoint[];
  renderPoints: DisplayPoint[];
  bounds: Bounds;
  rawCount: number;
  renderCount: number;
  usingBackendRender: boolean;
};

type PointSelectionStats = {
  rawCount: number;
  renderCount: number;
  usingBackendRender: boolean;
};

interface ZoneVisual {
  findingId: number;
  markerMaterial: THREE.MeshBasicMaterial;
  markerMesh: THREE.Mesh;
}

interface CameraTransition {
  startPosition: THREE.Vector3;
  endPosition: THREE.Vector3;
  startTarget: THREE.Vector3;
  endTarget: THREE.Vector3;
  startedAt: number;
  durationMs: number;
}

const props = defineProps<{
  sceneData: SceneResponse | null;
  selectedFindingId: number | null;
  activeTimestampMs?: number | null;
}>();

const emit = defineEmits<{
  select: [findingId: number, timestampMs?: number];
  seek: [timestampMs: number];
}>();

const host = ref<HTMLDivElement | null>(null);
const viewPreset = ref<ViewPreset>("perspective");
const showRenderControls = ref(false);
const showPathPoints = ref(true);
const fullHeightMode = ref(true);
const viewportHint = ref("");
const renderError = ref("");
const cutHeightM = ref(0);
const floorCutHeightM = ref(0);
const pointSizeScale = ref(0.82);
const pointDensityScale = ref(2.75);
const autoPointAggregation = ref(true);
const autoPointDensityScale = ref(2.75);
const structureClarity = ref(1.15);
const colorMode = ref<ColorMode>("inverseIntensityRainbow");
const edlEnabled = ref(true);
const surfaceFillEnabled = ref(true);
const lastSelectionStats = ref<PointSelectionStats | null>(null);

let renderer: THREE.WebGLRenderer | null = null;
let stage: THREE.Scene | null = null;
let camera: THREE.PerspectiveCamera | null = null;
let controls: OrbitControls | null = null;
let dynamicGroup: THREE.Group | null = null;
let sceneRenderTarget: THREE.WebGLRenderTarget | null = null;
let edlScene: THREE.Scene | null = null;
let edlCamera: THREE.OrthographicCamera | null = null;
let edlMaterial: THREE.ShaderMaterial | null = null;
let edlQuad: THREE.Mesh | null = null;
const edlAvailable = ref(true);
let selectableMeshes: THREE.Mesh[] = [];
let zoneVisuals: ZoneVisual[] = [];
let playbackMarker: THREE.Mesh | null = null;
let cachedSceneMetrics: { min: THREE.Vector3; max: THREE.Vector3; center: THREE.Vector3; span: number } | null = null;
let animationId = 0;
let transition: CameraTransition | null = null;
let resizeObserver: ResizeObserver | null = null;
let autoAggregationCheckAt = 0;
let autoAggregationTimer = 0;
let viewportHintTimer = 0;
let rebuildTimer = 0;
let deferredDenseRenderTimer = 0;
let pendingReframeCamera = false;
let suppressRebuildWatchers = false;
let denseRenderReady = false;
let selectionCache:
  | {
      sceneData: SceneResponse;
      cutHeight: number;
      floorCutHeight: number;
      pointDensity: number;
      fullHeightMode: boolean;
      denseRenderReady: boolean;
      selection: PointSelection;
    }
  | null = null;

const clock = new THREE.Clock();
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const percentileSampleLimit = 22000;
const fastInitialPointThreshold = 180000;
const fastInitialPointTarget = 140000;
const faceNeighborOffsets: Array<[number, number, number]> = [
  [1, 0, 0],
  [-1, 0, 0],
  [0, 1, 0],
  [0, -1, 0],
  [0, 0, 1],
  [0, 0, -1],
];
const fullNeighborOffsets: Array<[number, number, number]> = [];
for (let dx = -1; dx <= 1; dx += 1) {
  for (let dy = -1; dy <= 1; dy += 1) {
    for (let dz = -1; dz <= 1; dz += 1) {
      if (dx !== 0 || dy !== 0 || dz !== 0) {
        fullNeighborOffsets.push([dx, dy, dz]);
      }
    }
  }
}
const colorModeOptions: Array<{ value: ColorMode; label: string }> = [
  { value: "structure", label: "结构" },
  { value: "height", label: "高度" },
  { value: "intensity", label: "强度" },
  { value: "inverseIntensityRainbow", label: "反向强度彩虹" },
  { value: "rgb", label: "RGB" },
];
const cloudRampStops: Array<[number, THREE.Color]> = [
  [0, new THREE.Color("#14206c")],
  [0.18, new THREE.Color("#006bd6")],
  [0.36, new THREE.Color("#00c7df")],
  [0.55, new THREE.Color("#35d26f")],
  [0.72, new THREE.Color("#f4e65e")],
  [0.88, new THREE.Color("#ff9b35")],
  [1, new THREE.Color("#fff7dc")],
];

const hasRenderableScene = computed(() => {
  if (!props.sceneData) {
    return false;
  }
  return (
    props.sceneData.render_points.length > 0 ||
    props.sceneData.structure_points.length > 0 ||
    props.sceneData.points.length > 0 ||
    props.sceneData.roof_removed_points.length > 0 ||
    props.sceneData.floor_removed_points.length > 0 ||
    props.sceneData.full_points.length > 0 ||
    props.sceneData.trajectory.length > 0
  );
});

const sliderConfig = computed(() => {
  if (!props.sceneData) {
    return { min: 0, max: 1, step: 0.05 };
  }

  const bounds = boundsForCurrentPointMode(props.sceneData);
  let minZ = bounds?.min[2] ?? Number.NaN;
  let maxZ = bounds?.max[2] ?? Number.NaN;
  if (!Number.isFinite(minZ) || !Number.isFinite(maxZ) || Math.abs(maxZ - minZ) < 0.0001) {
    const scanned = scanHeightRange(getSourcePoints(props.sceneData));
    minZ = scanned.min;
    maxZ = scanned.max;
  }

  return {
    min: Number((Number.isFinite(minZ) ? minZ : 0).toFixed(2)),
    max: Number((Number.isFinite(maxZ) ? maxZ : 1).toFixed(2)),
    step: 0.05,
  };
});

const floorSliderConfig = computed(() => {
  const config = sliderConfig.value;
  return {
    min: config.min,
    max: Number(Math.max(config.min, cutHeightM.value - config.step).toFixed(2)),
    step: config.step,
  };
});

const hasRgbColor = computed(() => {
  if (!props.sceneData) {
    return false;
  }
  const source = fullHeightMode.value ? getFullHeightSourcePoints(props.sceneData) : getSourcePoints(props.sceneData);
  return sampleHasRgb(source) || sampleHasRgb(props.sceneData.structure_points) || sampleHasRgb(props.sceneData.render_points);
});

const activeSceneStats = computed(() => {
  if (!props.sceneData) {
    return [];
  }

  const density = currentPointDensityScale();
  const sourceCount = getSourcePoints(props.sceneData).length;
  const fallbackRenderCount =
    props.sceneData.structure_point_count ||
    props.sceneData.structure_points.length ||
    props.sceneData.render_point_count ||
    props.sceneData.render_points.length ||
    sourceCount;
  const stats = lastSelectionStats.value;
  const selection = {
    rawCount: stats?.rawCount ?? sourceCount,
    renderCount: stats?.renderCount ?? fallbackRenderCount,
  };
  return [
    `可用 ${formatCompactNumber(selection.rawCount)} pts`,
    `显示 ${formatCompactNumber(selection.renderCount)} pts`,
    `密度 ${density.toFixed(2)}x`,
  ];
});

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function currentPointDensityScale() {
  return autoPointAggregation.value ? autoPointDensityScale.value : pointDensityScale.value;
}

function isUsableBounds(bounds?: Bounds | SceneResponse["bounds"]) {
  return Boolean(
    bounds &&
      bounds.min?.length >= 3 &&
      bounds.max?.length >= 3 &&
      Number.isFinite(bounds.min[2]) &&
      Number.isFinite(bounds.max[2]) &&
      Math.abs(bounds.max[2] - bounds.min[2]) > 0.0001,
  );
}

function boundsForCurrentPointMode(sceneData: SceneResponse): Bounds {
  const preferred = fullHeightMode.value ? sceneData.full_bounds : sceneData.roof_removed_bounds;
  if (isUsableBounds(preferred)) {
    return preferred as Bounds;
  }
  if (isUsableBounds(sceneData.bounds)) {
    return sceneData.bounds as Bounds;
  }
  return computeBounds(getSourcePoints(sceneData), sceneData.trajectory);
}

function scanHeightRange(points: ScenePoint[]) {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  points.forEach((point) => {
    if (point[2] < min) min = point[2];
    if (point[2] > max) max = point[2];
  });
  return { min, max };
}

function sampleHasRgb(points: ScenePoint[]) {
  if (points.length === 0) {
    return false;
  }
  const stride = Math.max(1, Math.floor(points.length / 5000));
  for (let index = 0; index < points.length; index += stride) {
    if (points[index]?.length >= 7) {
      return true;
    }
  }
  return false;
}

function limitInitialRenderPoints(points: ScenePoint[]) {
  if (points.length <= fastInitialPointTarget) {
    return points;
  }
  const stride = Math.max(1, Math.ceil(points.length / fastInitialPointTarget));
  const limited: ScenePoint[] = [];
  for (let index = 0; index < points.length; index += stride) {
    limited.push(points[index]);
  }
  return limited;
}

function quantizeDensityScale(value: number) {
  return clamp(Math.round(value * 4) / 4, 0.75, 4);
}

function pointKey(ix: number, iy: number, iz: number) {
  return `${ix}|${iy}|${iz}`;
}

function resolveColorMode(points: DisplayPoint[]) {
  if (colorMode.value === "rgb" && !points.some((point) => point.rgb)) {
    return "structure";
  }
  return colorMode.value;
}

function percentile(values: number[], ratio: number) {
  if (values.length === 0) {
    return 0;
  }
  const sorted = [...values].sort((a, b) => a - b);
  const index = clamp(ratio, 0, 1) * (sorted.length - 1);
  const lower = Math.floor(index);
  const upper = Math.ceil(index);
  if (lower === upper) {
    return sorted[lower];
  }
  const weight = index - lower;
  return sorted[lower] * (1 - weight) + sorted[upper] * weight;
}

function normalizeScalar(value: number, low: number, high: number) {
  return clamp((value - low) / Math.max(0.0001, high - low), 0, 1);
}

function applyScalarRamp(value: number, target: THREE.Color) {
  const t = clamp(value, 0, 1);
  for (let index = 1; index < cloudRampStops.length; index += 1) {
    const [stop, color] = cloudRampStops[index];
    const [previousStop, previousColor] = cloudRampStops[index - 1];
    if (t <= stop) {
      const localT = normalizeScalar(t, previousStop, stop);
      target.copy(previousColor).lerp(color, localT);
      return target;
    }
  }
  return target.copy(cloudRampStops[cloudRampStops.length - 1][1]);
}

function applyInverseIntensityRainbowColor(intensity: number, target: THREE.Color) {
  const scaledIntensity = intensity <= 1 ? intensity * 255 : intensity;
  const normalizedIntensity = clamp(scaledIntensity, 0, 255);
  const value = 1 - normalizedIntensity / 255;
  const h = value * 5 + 1;
  const i = Math.floor(h);
  let f = h - i;
  if ((i & 1) === 0) {
    f = 1 - f;
  }
  const n = 1 - f;

  if (i <= 1) {
    target.setRGB(n, 0, 1);
  } else if (i === 2) {
    target.setRGB(0, n, 1);
  } else if (i === 3) {
    target.setRGB(0, 1, n);
  } else if (i === 4) {
    target.setRGB(n, 1, 0);
  } else {
    target.setRGB(1, n, 0);
  }
  return target;
}

function setRgbColor(rgb: [number, number, number], target: THREE.Color) {
  const divisor = rgb.some((value) => value > 1) ? 255 : 1;
  target.setRGB(
    clamp(rgb[0] / divisor, 0, 1),
    clamp(rgb[1] / divisor, 0, 1),
    clamp(rgb[2] / divisor, 0, 1),
  );
  return target;
}

function getFullHeightSourcePoints(sceneData: SceneResponse) {
  if (sceneData.full_points.length > 0) {
    return sceneData.full_points;
  }
  if (sceneData.points.length > 0) {
    return sceneData.points;
  }
  if (sceneData.roof_removed_points.length > 0) {
    return sceneData.roof_removed_points;
  }
  return sceneData.structure_points.length > 0 ? sceneData.structure_points : sceneData.render_points;
}

function getSourcePoints(sceneData: SceneResponse) {
  if (fullHeightMode.value) {
    return getFullHeightSourcePoints(sceneData);
  }
  if (sceneData.roof_removed_points.length > 0) {
    return sceneData.roof_removed_points;
  }
  if (sceneData.floor_removed_points.length > 0) {
    return sceneData.floor_removed_points;
  }
  if (sceneData.points.length > 0) {
    return sceneData.points;
  }
  if (sceneData.structure_points.length > 0) {
    return sceneData.structure_points;
  }
  if (sceneData.render_points.length > 0) {
    return sceneData.render_points;
  }
  if (sceneData.full_points.length > 0) {
    return sceneData.full_points;
  }
  return [];
}

function getDenseSourcePoints(sceneData: SceneResponse) {
  if (fullHeightMode.value) {
    return getFullHeightSourcePoints(sceneData);
  }
  if (sceneData.roof_removed_points.length > 0) {
    return sceneData.roof_removed_points;
  }
  if (sceneData.floor_removed_points.length > 0) {
    return sceneData.floor_removed_points;
  }
  if (sceneData.points.length > 0) {
    return sceneData.points;
  }
  if (sceneData.full_points.length > 0) {
    return sceneData.full_points;
  }
  return sceneData.structure_points.length > 0 ? sceneData.structure_points : sceneData.render_points;
}

function isDefaultCut(sceneData: SceneResponse) {
  const defaultFloorCut = sliderConfig.value.min;
  if (fullHeightMode.value) {
    return (
      Math.abs(cutHeightM.value - sliderConfig.value.max) <= 0.03 &&
      Math.abs(floorCutHeightM.value - defaultFloorCut) <= 0.03
    );
  }
  return (
    Math.abs(cutHeightM.value - sceneData.cut_height_default) <= 0.03 &&
    Math.abs(floorCutHeightM.value - defaultFloorCut) <= 0.03
  );
}

function getPointSelection(sceneData: SceneResponse): PointSelection {
  const densityScale = currentPointDensityScale();
  if (
    selectionCache?.sceneData === sceneData &&
    Math.abs(selectionCache.cutHeight - cutHeightM.value) < 0.0001 &&
    Math.abs(selectionCache.floorCutHeight - floorCutHeightM.value) < 0.0001 &&
    Math.abs(selectionCache.pointDensity - densityScale) < 0.0001 &&
    selectionCache.fullHeightMode === fullHeightMode.value &&
    selectionCache.denseRenderReady === denseRenderReady
  ) {
    return selectionCache.selection;
  }

  const sourcePoints = getSourcePoints(sceneData);
  const backendPoints = sceneData.structure_points.length > 0 ? sceneData.structure_points : sceneData.render_points;
  if (isDefaultCut(sceneData) && backendPoints.length > 0) {
    const denseSourcePoints = getDenseSourcePoints(sceneData);
    const shouldUseFastBackend =
      !denseRenderReady && fullHeightMode.value && denseSourcePoints.length > fastInitialPointThreshold;
    const aggregationSource = shouldUseFastBackend
      ? limitInitialRenderPoints(backendPoints.length > 0 ? backendPoints : denseSourcePoints)
      : denseSourcePoints.length > 0
        ? denseSourcePoints
        : backendPoints;
    const renderPoints = buildDisplayPoints(aggregationSource);
    const selection = {
      sourcePoints: aggregationSource.length > 0 ? aggregationSource : sourcePoints,
      renderPoints,
      bounds: boundsForCurrentPointMode(sceneData),
      rawCount: denseSourcePoints.length || aggregationSource.length || sourcePoints.length,
      renderCount: renderPoints.length,
      usingBackendRender: shouldUseFastBackend,
    };
    lastSelectionStats.value = {
      rawCount: selection.rawCount,
      renderCount: selection.renderCount,
      usingBackendRender: selection.usingBackendRender,
    };
    selectionCache = {
      sceneData,
      cutHeight: cutHeightM.value,
      floorCutHeight: floorCutHeightM.value,
      pointDensity: densityScale,
      fullHeightMode: fullHeightMode.value,
      denseRenderReady,
      selection,
    };
    if (shouldUseFastBackend) {
      scheduleDeferredDenseRender();
    }
    return selection;
  }

  const filtered = sourcePoints.filter((point) => point[2] <= cutHeightM.value && point[2] >= floorCutHeightM.value);
  const activePoints = filtered.length > 0 ? filtered : sourcePoints.slice(0, Math.min(sourcePoints.length, 4000));
  const bounds = computeBounds(activePoints, sceneData.trajectory);
  const renderPoints = buildDisplayPoints(activePoints);
  const selection = {
    sourcePoints: activePoints,
    renderPoints,
    bounds,
    rawCount: activePoints.length,
    renderCount: renderPoints.length,
    usingBackendRender: false,
  };
  lastSelectionStats.value = {
    rawCount: selection.rawCount,
    renderCount: selection.renderCount,
    usingBackendRender: selection.usingBackendRender,
  };
  selectionCache = {
    sceneData,
    cutHeight: cutHeightM.value,
    floorCutHeight: floorCutHeightM.value,
    pointDensity: densityScale,
    fullHeightMode: fullHeightMode.value,
    denseRenderReady,
    selection,
  };
  return selection;
}

function computeBounds(points: ScenePoint[], trajectory: [number, number, number][]): Bounds {
  const padding = [2.4, 2.4, 1.2] as const;
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let minZ = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  let maxZ = Number.NEGATIVE_INFINITY;

  points.forEach((point) => {
    if (point[0] < minX) minX = point[0];
    if (point[1] < minY) minY = point[1];
    if (point[2] < minZ) minZ = point[2];
    if (point[0] > maxX) maxX = point[0];
    if (point[1] > maxY) maxY = point[1];
    if (point[2] > maxZ) maxZ = point[2];
  });

  trajectory.forEach((pose) => {
    if (pose[0] < minX) minX = pose[0];
    if (pose[1] < minY) minY = pose[1];
    if (pose[2] < minZ) minZ = pose[2];
    if (pose[0] > maxX) maxX = pose[0];
    if (pose[1] > maxY) maxY = pose[1];
    if (pose[2] > maxZ) maxZ = pose[2];
  });

  if (!Number.isFinite(minX)) {
    return { min: [0, 0, 0], max: [0, 0, 0] };
  }

  return {
    min: [
      Number((minX - padding[0]).toFixed(4)),
      Number((minY - padding[1]).toFixed(4)),
      Number((minZ - padding[2]).toFixed(4)),
    ],
    max: [
      Number((maxX + padding[0]).toFixed(4)),
      Number((maxY + padding[1]).toFixed(4)),
      Number((maxZ + padding[2]).toFixed(4)),
    ],
  };
}

function buildDisplayPoints(points: ScenePoint[]) {
  const voxelSize = resolveRenderVoxelSize(points.length, currentPointDensityScale());
  const buckets = new Map<string, PointBucket>();

  points.forEach((point) => {
    const ix = Math.floor(point[0] / voxelSize);
    const iy = Math.floor(point[1] / voxelSize);
    const iz = Math.floor(point[2] / voxelSize);
    const key = pointKey(ix, iy, iz);
    const bucket = buckets.get(key) ?? {
      ix,
      iy,
      iz,
      sumX: 0,
      sumY: 0,
      sumZ: 0,
      sumI: 0,
      sumR: 0,
      sumG: 0,
      sumB: 0,
      count: 0,
      colorHits: 0,
    };
    bucket.sumX += point[0];
    bucket.sumY += point[1];
    bucket.sumZ += point[2];
    bucket.sumI += point[3] ?? 0;
    bucket.count += 1;

    if (point.length >= 7) {
      bucket.sumR += point[4] ?? 0;
      bucket.sumG += point[5] ?? 0;
      bucket.sumB += point[6] ?? 0;
      bucket.colorHits += 1;
    }
    buckets.set(key, bucket);
  });

  const result: DisplayPoint[] = [];

  buckets.forEach((bucket) => {
    const item: DisplayPoint = {
      x: bucket.sumX / bucket.count,
      y: bucket.sumY / bucket.count,
      z: bucket.sumZ / bucket.count,
      intensity: bucket.sumI / bucket.count,
      density: bucket.count,
      heightT: 0,
      intensityT: 0,
      densityT: 0,
      edgeT: 0,
      scalar: 0,
      shade: 1,
    };
    if (bucket.colorHits > 0) {
      item.rgb = [
        bucket.sumR / bucket.colorHits,
        bucket.sumG / bucket.colorHits,
        bucket.sumB / bucket.colorHits,
      ];
    }
    result.push(item);
  });

  const sampleStride = Math.max(1, Math.floor(result.length / percentileSampleLimit));
  const heights: number[] = [];
  const intensities: number[] = [];
  const densities: number[] = [];
  for (let sampleIndex = 0; sampleIndex < result.length; sampleIndex += sampleStride) {
    const item = result[sampleIndex];
    heights.push(item.z);
    intensities.push(item.intensity);
    densities.push(Math.log1p(item.density));
  }

  const lowZ = percentile(heights, 0.01);
  const highZ = percentile(heights, 0.99);
  const lowIntensity = percentile(intensities, 0.02);
  const highIntensity = percentile(intensities, 0.98);
  const lowDensity = percentile(densities, 0.04);
  const highDensity = percentile(densities, 0.98);

  const neighborOffsets = result.length > 260000 ? faceNeighborOffsets : fullNeighborOffsets;
  let index = 0;
  buckets.forEach((bucket) => {
    const item = result[index];
    let neighborCount = 0;
    neighborOffsets.forEach(([dx, dy, dz]) => {
      if (buckets.has(pointKey(bucket.ix + dx, bucket.iy + dy, bucket.iz + dz))) {
        neighborCount += 1;
      }
    });

    item.heightT = normalizeScalar(item.z, lowZ, highZ);
    item.intensityT = normalizeScalar(item.intensity, lowIntensity, highIntensity);
    item.densityT = normalizeScalar(Math.log1p(item.density), lowDensity, highDensity);
    const occupancyT = neighborCount / neighborOffsets.length;
    const densitySupport = clamp(0.38 + item.densityT * 0.42, 0.38, 0.8);
    item.edgeT = clamp(Math.pow(1 - occupancyT, 1.08) * densitySupport, 0, 0.82);
    item.scalar = item.heightT;
    item.shade = clamp(0.78 + item.heightT * 0.18 + item.intensityT * 0.08 + item.densityT * 0.06, 0.74, 1.12);
    index += 1;
  });

  return result;
}

function resolveRenderVoxelSize(rawCount: number, densityScale: number) {
  const factor = Math.cbrt(Math.max(1, rawCount) / 105000);
  return clamp((0.108 * factor) / clamp(densityScale, 0.75, 4), 0.032, 0.34);
}

function getSelectedZone(sceneData: SceneResponse | null): ZoneResponse | null {
  if (!sceneData || props.selectedFindingId === null) {
    return null;
  }
  return sceneData.hazard_zones.find((zone) => zone.finding_id === props.selectedFindingId) ?? null;
}

function getSceneMetrics(sceneData: SceneResponse) {
  const bounds = getPointSelection(sceneData).bounds;
  const min = new THREE.Vector3(...bounds.min);
  const max = new THREE.Vector3(...bounds.max);
  const center = min.clone().add(max).multiplyScalar(0.5);
  const span = Math.max(18, max.x - min.x, max.y - min.y, max.z - min.z);
  return { min, max, center, span };
}

function computeCameraShot(sceneData: SceneResponse, preset: ViewPreset, zone: ZoneResponse | null) {
  const { center, max, span } = getSceneMetrics(sceneData);
  const focus = zone ? new THREE.Vector3(zone.center[0], zone.center[1], zone.center[2]) : center;
  const distance = zone ? clamp(span * 0.42, 12, 30) : clamp(span * 0.9, 19, 58);

  if (preset === "top") {
    return {
      target: focus,
      position: new THREE.Vector3(focus.x, focus.y - distance * 0.04, max.z + distance * 1.6),
    };
  }

  return {
    target: focus,
    position: new THREE.Vector3(focus.x + distance * 0.96, focus.y - distance * 1.08, focus.z + distance * 0.7),
  };
}

function resolveAutoPointDensityScale(sceneData: SceneResponse) {
  if (!camera || !controls) {
    return quantizeDensityScale(pointDensityScale.value);
  }

  const min = sceneData.bounds.min;
  const max = sceneData.bounds.max;
  const span = Math.max(18, max[0] - min[0], max[1] - min[1], max[2] - min[2]);
  const distance = camera.position.distanceTo(controls.target);
  const distanceRatio = distance / span;
  const zoomT = clamp((1.32 - distanceRatio) / 1.05, 0, 1);
  const baseDensity = 1.25 + zoomT * 2.55;
  const userBias = pointDensityScale.value / 2.75;
  return quantizeDensityScale(baseDensity * userBias);
}

function applyAutoPointAggregation(force = false) {
  if (!autoPointAggregation.value || !props.sceneData || !camera || !controls) {
    return;
  }

  const nextDensity = resolveAutoPointDensityScale(props.sceneData);
  if (!force && Math.abs(nextDensity - autoPointDensityScale.value) < 0.001) {
    return;
  }

  autoPointDensityScale.value = nextDensity;
  scheduleAggregationRebuild();
}

function scheduleAggregationRebuild() {
  if (autoAggregationTimer !== 0) {
    return;
  }

  autoAggregationTimer = window.setTimeout(() => {
    autoAggregationTimer = 0;
    selectionCache = null;
    scheduleSceneRebuild(false);
  }, 180);
}

function scheduleDeferredDenseRender() {
  if (deferredDenseRenderTimer !== 0 || denseRenderReady) {
    return;
  }

  const schedule = window.requestIdleCallback
    ? (callback: () => void) => window.requestIdleCallback(callback, { timeout: 1600 })
    : (callback: () => void) => window.setTimeout(callback, 420);

  deferredDenseRenderTimer = schedule(() => {
    deferredDenseRenderTimer = 0;
    if (!props.sceneData || denseRenderReady) {
      return;
    }
    denseRenderReady = true;
    selectionCache = null;
    scheduleSceneRebuild(false);
  });
}

function cancelDeferredDenseRender() {
  if (deferredDenseRenderTimer === 0) {
    return;
  }
  if (window.cancelIdleCallback) {
    window.cancelIdleCallback(deferredDenseRenderTimer);
  } else {
    window.clearTimeout(deferredDenseRenderTimer);
  }
  deferredDenseRenderTimer = 0;
}

function scheduleSceneRebuild(reframeCamera: boolean) {
  pendingReframeCamera ||= reframeCamera;
  if (rebuildTimer !== 0) {
    return;
  }

  rebuildTimer = window.setTimeout(() => {
    rebuildTimer = 0;
    const shouldReframe = pendingReframeCamera;
    pendingReframeCamera = false;
    rebuildSceneGraph(shouldReframe);
  }, 0);
}

function clearSelectionCache(resetDenseRender = false) {
  selectionCache = null;
  if (resetDenseRender) {
    denseRenderReady = false;
    cancelDeferredDenseRender();
  }
}

function showViewportHint(message: string) {
  viewportHint.value = message;
  if (viewportHintTimer !== 0) {
    window.clearTimeout(viewportHintTimer);
  }
  viewportHintTimer = window.setTimeout(() => {
    viewportHint.value = "";
    viewportHintTimer = 0;
  }, 3200);
}

function setFullHeightMode(enabled: boolean) {
  if (!props.sceneData) {
    return;
  }
  suppressRebuildWatchers = true;
  fullHeightMode.value = enabled;
  clearSelectionCache(true);
  const config = sliderConfig.value;
  cutHeightM.value = Number((enabled ? config.max : props.sceneData.cut_height_default || config.max).toFixed(2));
  floorCutHeightM.value = Number((config.min || 0).toFixed(2));
  suppressRebuildWatchers = false;
  scheduleSceneRebuild(false);
}

function toggleFullHeightMode() {
  setFullHeightMode(!fullHeightMode.value);
}

function initStage() {
  if (!host.value || renderer) {
    return;
  }

  stage = new THREE.Scene();
  stage.background = new THREE.Color("#020711");
  stage.fog = new THREE.Fog("#020711", 48, 190);

  camera = new THREE.PerspectiveCamera(42, 1, 0.1, 650);
  camera.up.set(0, 0, 1);
  camera.position.set(24, -32, 18);

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;
  host.value.appendChild(renderer.domElement);
  initEdlPostProcess();

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 8;
  controls.maxDistance = 220;
  controls.target.set(0, 0, 0);
  controls.addEventListener("start", cancelAutoFocus);

  stage.add(new THREE.AmbientLight("#9ad8ff", 0.42));
  stage.add(new THREE.HemisphereLight("#e9fbff", "#040c16", 0.78));
  const key = new THREE.DirectionalLight("#ffffff", 1.42);
  key.position.set(32, -22, 36);
  const fill = new THREE.DirectionalLight("#5bbcff", 0.58);
  fill.position.set(-18, 14, 18);
  const rim = new THREE.DirectionalLight("#ffb26f", 0.3);
  rim.position.set(-10, -28, 12);
  stage.add(key, fill, rim);

  renderer.domElement.addEventListener("pointerdown", onPointerDown);
  window.addEventListener("resize", resizeRenderer);
  resizeObserver = new ResizeObserver(() => resizeRenderer());
  resizeObserver.observe(host.value);

  resizeRenderer();
  scheduleSceneRebuild(true);
  animate();
}

function initEdlPostProcess() {
  if (!renderer) {
    return;
  }
  edlAvailable.value = renderer.capabilities.isWebGL2 || Boolean(renderer.extensions.get("WEBGL_depth_texture"));
  edlScene = new THREE.Scene();
  edlCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  edlMaterial = new THREE.ShaderMaterial({
    uniforms: {
      tColor: { value: null },
      tDepth: { value: null },
      uTexelSize: { value: new THREE.Vector2(1, 1) },
      uNear: { value: 0.1 },
      uFar: { value: 650 },
      uRadius: { value: 1.0 },
      uStrength: { value: 0.72 },
      uFillRadius: { value: 1.6 },
      uFillStrength: { value: 0.82 },
      uSurfaceFillEnabled: { value: 1 },
    },
    depthTest: false,
    depthWrite: false,
    transparent: false,
    vertexShader: `
      varying vec2 vUv;

      void main() {
        vUv = uv;
        gl_Position = vec4(position.xy, 0.0, 1.0);
      }
    `,
    fragmentShader: `
      uniform sampler2D tColor;
      uniform sampler2D tDepth;
      uniform vec2 uTexelSize;
      uniform float uNear;
      uniform float uFar;
      uniform float uRadius;
      uniform float uStrength;
      uniform float uFillRadius;
      uniform float uFillStrength;
      uniform int uSurfaceFillEnabled;

      varying vec2 vUv;

      float perspectiveDepthToViewZ(const in float depth, const in float near, const in float far) {
        return (near * far) / ((far - near) * depth - far);
      }

      float readLinearDepth(vec2 uv) {
        float depth = texture2D(tDepth, uv).x;
        if (depth >= 1.0) {
          return 1.0e6;
        }
        return -perspectiveDepthToViewZ(depth, uNear, uFar);
      }

      vec4 readColor(vec2 uv) {
        return texture2D(tColor, clamp(uv, vec2(0.0), vec2(1.0)));
      }

      float readClampedDepth(vec2 uv) {
        return readLinearDepth(clamp(uv, vec2(0.0), vec2(1.0)));
      }

      float nearestNeighborDepth(vec2 uv, vec2 stepUv) {
        float nearestDepth = 1.0e6;
        for (int x = -2; x <= 2; x++) {
          for (int y = -2; y <= 2; y++) {
            vec2 offset = vec2(float(x), float(y));
            if (dot(offset, offset) < 0.25) {
              continue;
            }
            float distanceT = length(offset);
            if (distanceT > uFillRadius + 0.35) {
              continue;
            }
            float sampleDepth = readClampedDepth(uv + offset * stepUv);
            nearestDepth = min(nearestDepth, sampleDepth);
          }
        }
        return nearestDepth;
      }

      vec4 surfaceFillColor(vec2 uv, vec2 stepUv, float nearestDepth) {
        vec3 colorSum = vec3(0.0);
        float weightSum = 0.0;
        float tolerance = max(0.22, nearestDepth * 0.0045);
        for (int x = -2; x <= 2; x++) {
          for (int y = -2; y <= 2; y++) {
            vec2 offset = vec2(float(x), float(y));
            float distanceT = length(offset);
            if (distanceT > uFillRadius + 0.35) {
              continue;
            }
            vec2 sampleUv = uv + offset * stepUv;
            float sampleDepth = readClampedDepth(sampleUv);
            if (sampleDepth > 9.0e5 || abs(sampleDepth - nearestDepth) > tolerance) {
              continue;
            }
            float weight = 1.0 / (1.0 + distanceT * distanceT);
            colorSum += readColor(sampleUv).rgb * weight;
            weightSum += weight;
          }
        }
        if (weightSum <= 0.0) {
          return readColor(uv);
        }
        return vec4(colorSum / weightSum, 1.0);
      }

      float sampleOcclusion(vec2 uv, vec2 offset, vec2 stepUv, float centerDepth) {
        float neighborDepth = readLinearDepth(uv + offset * stepUv);
        if (neighborDepth > 9.0e5) {
          return 0.0;
        }
        return abs(log2((neighborDepth + 1.0) / (centerDepth + 1.0)));
      }

      void main() {
        vec4 base = readColor(vUv);
        float centerDepth = readLinearDepth(vUv);
        vec2 fillStepUv = uTexelSize;

        if (uSurfaceFillEnabled == 1) {
          float nearestDepth = nearestNeighborDepth(vUv, fillStepUv);
          if (nearestDepth < 9.0e5) {
            float centerBehindSurface = centerDepth > nearestDepth + max(0.26, nearestDepth * 0.0045) ? 1.0 : 0.0;
            float centerEmpty = centerDepth > 9.0e5 ? 1.0 : 0.0;
            float fillMask = max(centerEmpty, centerBehindSurface);
            if (fillMask > 0.0) {
              vec4 filled = surfaceFillColor(vUv, fillStepUv, nearestDepth);
              float fillAmount = clamp(uFillStrength, 0.0, 1.0);
              base = mix(base, filled, fillAmount);
              centerDepth = nearestDepth;
            }
          }
        }

        if (centerDepth > 9.0e5) {
          gl_FragColor = base;
          #include <colorspace_fragment>
          return;
        }

        vec2 stepUv = uTexelSize * uRadius;
        float occlusion = 0.0;
        occlusion += sampleOcclusion(vUv, vec2(1.0, 0.0), stepUv, centerDepth);
        occlusion += sampleOcclusion(vUv, vec2(-1.0, 0.0), stepUv, centerDepth);
        occlusion += sampleOcclusion(vUv, vec2(0.0, 1.0), stepUv, centerDepth);
        occlusion += sampleOcclusion(vUv, vec2(0.0, -1.0), stepUv, centerDepth);
        occlusion += sampleOcclusion(vUv, vec2(0.707, 0.707), stepUv, centerDepth);
        occlusion += sampleOcclusion(vUv, vec2(-0.707, 0.707), stepUv, centerDepth);
        occlusion += sampleOcclusion(vUv, vec2(0.707, -0.707), stepUv, centerDepth);
        occlusion += sampleOcclusion(vUv, vec2(-0.707, -0.707), stepUv, centerDepth);
        occlusion *= 0.125;
        float shade = exp(-occlusion * uStrength * 0.42);
        shade = clamp(shade, 0.46, 1.0);
        vec3 color = base.rgb * shade;
        color = clamp((color - vec3(0.5)) * (1.0 + uStrength * 0.07) + vec3(0.5), 0.0, 1.0);
        gl_FragColor = vec4(color, base.a);
        #include <colorspace_fragment>
      }
    `,
  });
  edlQuad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), edlMaterial);
  edlScene.add(edlQuad);
}

function ensureEdlTarget(width: number, height: number) {
  if (!renderer || !edlMaterial || !edlAvailable.value) {
    return;
  }

  const pixelRatio = renderer.getPixelRatio();
  const targetWidth = Math.max(1, Math.floor(width * pixelRatio));
  const targetHeight = Math.max(1, Math.floor(height * pixelRatio));

  try {
    if (!sceneRenderTarget) {
      sceneRenderTarget = new THREE.WebGLRenderTarget(targetWidth, targetHeight, {
        depthBuffer: true,
        stencilBuffer: false,
      });
      sceneRenderTarget.texture.name = "SceneViewportColor";
      sceneRenderTarget.texture.colorSpace = THREE.SRGBColorSpace;
      sceneRenderTarget.depthTexture = new THREE.DepthTexture(targetWidth, targetHeight);
      sceneRenderTarget.depthTexture.format = THREE.DepthFormat;
      sceneRenderTarget.depthTexture.type = THREE.UnsignedShortType;
      edlMaterial.uniforms.tColor.value = sceneRenderTarget.texture;
      edlMaterial.uniforms.tDepth.value = sceneRenderTarget.depthTexture;
    } else {
      sceneRenderTarget.setSize(targetWidth, targetHeight);
    }
    edlMaterial.uniforms.uTexelSize.value.set(1 / targetWidth, 1 / targetHeight);
  } catch (error) {
    sceneRenderTarget?.dispose();
    sceneRenderTarget = null;
    edlAvailable.value = false;
  }
}

function renderSceneFrame() {
  if (!renderer || !stage || !camera) {
    return;
  }

  if ((edlEnabled.value || surfaceFillEnabled.value) && edlAvailable.value && sceneRenderTarget && edlScene && edlCamera && edlMaterial) {
    const fillDistanceRatio =
      props.sceneData && controls
        ? camera.position.distanceTo(controls.target) /
          Math.max(
            18,
            props.sceneData.bounds.max[0] - props.sceneData.bounds.min[0],
            props.sceneData.bounds.max[1] - props.sceneData.bounds.min[1],
            props.sceneData.bounds.max[2] - props.sceneData.bounds.min[2],
          )
        : 1;
    const farFillT = clamp((fillDistanceRatio - 0.34) / 0.82, 0, 1);
    edlMaterial.uniforms.uNear.value = camera.near;
    edlMaterial.uniforms.uFar.value = camera.far;
    edlMaterial.uniforms.uRadius.value = clamp(1.0 + structureClarity.value * 0.22 + farFillT * 0.28, 1.05, 1.75);
    edlMaterial.uniforms.uStrength.value = edlEnabled.value
      ? clamp(0.72 + structureClarity.value * 0.42, 0.86, 1.48)
      : 0;
    edlMaterial.uniforms.uFillRadius.value = surfaceFillEnabled.value ? clamp(1.25 + farFillT * 1.35, 1.25, 2.6) : 0;
    edlMaterial.uniforms.uFillStrength.value = surfaceFillEnabled.value ? clamp(0.48 + farFillT * 0.34, 0.48, 0.86) : 0;
    edlMaterial.uniforms.uSurfaceFillEnabled.value = surfaceFillEnabled.value ? 1 : 0;
    renderer.setRenderTarget(sceneRenderTarget);
    renderer.clear();
    renderer.render(stage, camera);
    renderer.setRenderTarget(null);
    renderer.clear();
    renderer.render(edlScene, edlCamera);
    return;
  }

  renderer.setRenderTarget(null);
  renderer.render(stage, camera);
}

function animate() {
  if (!renderer || !stage || !camera) {
    return;
  }

  animationId = window.requestAnimationFrame(animate);
  clock.getDelta();

  if (transition && controls) {
    const progress = clamp((performance.now() - transition.startedAt) / transition.durationMs, 0, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    camera.position.lerpVectors(transition.startPosition, transition.endPosition, eased);
    controls.target.lerpVectors(transition.startTarget, transition.endTarget, eased);
    if (progress >= 1) {
      transition = null;
    }
  }

  controls?.update();
  if (autoPointAggregation.value) {
    const now = performance.now();
    if (now - autoAggregationCheckAt > 140) {
      autoAggregationCheckAt = now;
      applyAutoPointAggregation(false);
    }
  }
  renderSceneFrame();
}

function resizeRenderer() {
  if (!renderer || !camera || !host.value) {
    return;
  }

  const width = Math.max(host.value.clientWidth, 1);
  const height = Math.max(host.value.clientHeight, 1);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  ensureEdlTarget(width, height);
}

function disposeObject(object: THREE.Object3D) {
  object.traverse((entry) => {
    const mesh = entry as THREE.Mesh;
    if ("geometry" in mesh && mesh.geometry) {
      mesh.geometry.dispose();
    }
    const material = (mesh.material ?? null) as THREE.Material | THREE.Material[] | null;
    if (Array.isArray(material)) {
      material.forEach((item) => item.dispose());
    } else {
      material?.dispose();
    }
  });
}

function disposeEdlResources() {
  sceneRenderTarget?.dispose();
  sceneRenderTarget = null;
  edlQuad?.geometry.dispose();
  edlMaterial?.dispose();
  edlScene = null;
  edlCamera = null;
  edlMaterial = null;
  edlQuad = null;
}

function clearDynamic() {
  selectableMeshes = [];
  zoneVisuals = [];
  playbackMarker = null;
  cachedSceneMetrics = null;
  if (!stage || !dynamicGroup) {
    return;
  }
  stage.remove(dynamicGroup);
  disposeObject(dynamicGroup);
  dynamicGroup = null;
}

function rebuildSceneGraph(reframeCamera: boolean) {
  if (rebuildTimer !== 0) {
    window.clearTimeout(rebuildTimer);
    rebuildTimer = 0;
    pendingReframeCamera ||= reframeCamera;
    reframeCamera = pendingReframeCamera;
    pendingReframeCamera = false;
  }
  if (!stage) {
    return;
  }

  renderError.value = "";
  clearDynamic();
  dynamicGroup = new THREE.Group();
  stage.add(dynamicGroup);

  if (!props.sceneData) {
    return;
  }

  try {
    const selection = getPointSelection(props.sceneData);
    const { min, center, span } = getSceneMetrics(props.sceneData);
    cachedSceneMetrics = { min: min.clone(), max: selection.bounds.max ? new THREE.Vector3(...selection.bounds.max) : min.clone(), center: center.clone(), span };

    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(span * 1.95, span * 1.95),
      new THREE.MeshBasicMaterial({ color: "#040b14", transparent: true, opacity: 0.18, depthWrite: false }),
    );
    ground.rotation.x = Math.PI / 2;
    ground.position.set(center.x, center.y, min.z - 0.12);
    dynamicGroup.add(ground);

    const grid = new THREE.GridHelper(span * 1.78, Math.max(18, Math.round(span * 1.15)), 0x16304c, 0x0a1728);
    grid.rotation.x = Math.PI / 2;
    grid.position.set(center.x, center.y, min.z - 0.04);
    applyGridStyle(grid, 0.18);
    dynamicGroup.add(grid);

    if (selection.renderPoints.length > 0) {
      dynamicGroup.add(buildPointCloud(selection.renderPoints, selection.bounds, span));
    }
    if (showPathPoints.value) {
      buildTrajectoryPoints(props.sceneData, min.z, span);
    }
    updatePlaybackMarker();

    if (reframeCamera) {
      startAutoFocus(true);
    }
  } catch (error) {
    clearDynamic();
    renderError.value = error instanceof Error ? error.message : "Unknown scene render error";
  }
}

function applyGridStyle(grid: THREE.GridHelper, opacity: number) {
  const material = grid.material as THREE.Material | THREE.Material[];
  if (Array.isArray(material)) {
    material.forEach((entry) => {
      entry.transparent = true;
      entry.opacity = opacity;
    });
    return;
  }
  material.transparent = true;
  material.opacity = opacity;
}

function buildPointCloud(points: DisplayPoint[], bounds: Bounds, span: number) {
  const geometry = new THREE.BufferGeometry();
  const positions = new Float32Array(points.length * 3);
  const colors = new Float32Array(points.length * 3);
  const features = new Float32Array(points.length);
  const shades = new Float32Array(points.length);
  const tint = new THREE.Color();
  const structureLow = new THREE.Color("#6f7d83");
  const structureMid = new THREE.Color("#a4b2b8");
  const structureHigh = new THREE.Color("#d9e3e5");
  const mode = resolveColorMode(points);

  points.forEach((point, index) => {
    positions[index * 3] = point.x;
    positions[index * 3 + 1] = point.y;
    positions[index * 3 + 2] = point.z;

    if (mode === "structure") {
      const heightLight = clamp(0.42 + point.heightT * 0.34, 0.34, 0.8);
      const detailLight = clamp(point.intensityT * 0.08 + point.densityT * 0.08, 0, 0.14);
      tint.copy(structureLow).lerp(structureMid, heightLight);
      tint.lerp(structureHigh, detailLight);
    } else if (mode === "rgb" && point.rgb) {
      setRgbColor(point.rgb, tint);
    } else if (mode === "intensity") {
      applyScalarRamp(Math.pow(point.intensityT, 0.76), tint);
    } else if (mode === "inverseIntensityRainbow") {
      applyInverseIntensityRainbowColor(point.intensity, tint);
    } else {
      applyScalarRamp(point.heightT, tint);
    }

    const featureT = clamp(point.edgeT * 0.58 + point.densityT * 0.18, 0, 0.86);
    colors[index * 3] = tint.r;
    colors[index * 3 + 1] = tint.g;
    colors[index * 3 + 2] = tint.b;
    features[index] = featureT;
    shades[index] = clamp(point.shade - point.edgeT * 0.1 + point.densityT * 0.03, 0.68, 1.14);
  });

  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  geometry.setAttribute("feature", new THREE.BufferAttribute(features, 1));
  geometry.setAttribute("shade", new THREE.BufferAttribute(shades, 1));
  geometry.computeBoundingSphere();

  const densityScale = Math.log10(Math.max(1, points.length) / 12000);
  const basePixelSize = clamp(1.72 - densityScale * 0.14, 1.18, 1.85);
  const pixelSize = clamp(basePixelSize * pointSizeScale.value, 0.88, 2.65);
  const maxPointSize = clamp(basePixelSize * 1.65, 2.4, 3.35);

  const cloud = new THREE.Points(
    geometry,
    new THREE.ShaderMaterial({
      uniforms: {
        uPointSize: { value: pixelSize },
        uMaxPointSize: { value: maxPointSize },
      },
      vertexColors: true,
      transparent: false,
      depthTest: true,
      depthWrite: true,
      vertexShader: `
        attribute float feature;
        attribute float shade;

        uniform float uPointSize;
        uniform float uMaxPointSize;

        varying vec3 vColor;
        varying float vFeature;
        varying float vShade;

        void main() {
          vColor = color;
          vFeature = feature;
          vShade = shade;

          vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
          gl_Position = projectionMatrix * mvPosition;

          float depthT = clamp((-mvPosition.z - 8.0) / 90.0, 0.0, 1.0);
          float depthTrim = mix(1.06, 0.82, depthT);
          float featureBoost = mix(0.94, 1.08, feature);
          gl_PointSize = clamp(uPointSize * depthTrim * featureBoost, 1.0, uMaxPointSize);
        }
      `,
      fragmentShader: `
        varying vec3 vColor;
        varying float vFeature;
        varying float vShade;

        void main() {
          vec2 centered = gl_PointCoord - vec2(0.5);
          float radiusSq = dot(centered, centered) * 4.0;
          if (radiusSq > 0.96) {
            discard;
          }

          float rim = smoothstep(0.72, 0.96, radiusSq);
          float contrast = 1.08;
          vec3 color = clamp((vColor - vec3(0.5)) * contrast + vec3(0.5), 0.0, 1.0);
          color *= clamp(vShade, 0.58, 1.28);
          color *= 1.0 - rim * 0.1;

          gl_FragColor = vec4(color, 1.0);
          #include <tonemapping_fragment>
          #include <colorspace_fragment>
        }
      `,
    }),
  );
  cloud.renderOrder = 1;
  cloud.frustumCulled = false;
  return cloud;
}

function nearestTrajectoryIndexByTimestamp(sceneData: SceneResponse, targetMs: number) {
  const timestamps = sceneData.trajectory_timestamps;
  if (!timestamps.length || timestamps.length !== sceneData.trajectory.length) {
    return -1;
  }
  let bestIndex = 0;
  let bestDelta = Number.POSITIVE_INFINITY;
  timestamps.forEach((timestamp, index) => {
    const delta = Math.abs(timestamp - targetMs);
    if (delta < bestDelta) {
      bestDelta = delta;
      bestIndex = index;
    }
  });
  return bestIndex;
}

function nearestTrajectoryIndexByPosition(sceneData: SceneResponse, position: [number, number, number]) {
  if (!sceneData.trajectory.length) {
    return -1;
  }
  let bestIndex = 0;
  let bestDistanceSq = Number.POSITIVE_INFINITY;
  sceneData.trajectory.forEach((point, index) => {
    const distanceSq = (point[0] - position[0]) ** 2 + (point[1] - position[1]) ** 2 + (point[2] - position[2]) ** 2;
    if (distanceSq < bestDistanceSq) {
      bestDistanceSq = distanceSq;
      bestIndex = index;
    }
  });
  return bestIndex;
}

function resolveHazardTrajectoryIndex(sceneData: SceneResponse, zone: ZoneResponse) {
  if (zone.related_pose_ts > 0) {
    const timestampIndex = nearestTrajectoryIndexByTimestamp(sceneData, zone.related_pose_ts);
    if (timestampIndex >= 0) {
      return timestampIndex;
    }
  }
  return nearestTrajectoryIndexByPosition(sceneData, zone.center);
}

function buildHazardTrajectoryMap(sceneData: SceneResponse) {
  const result = new Map<number, ZoneResponse[]>();
  sceneData.hazard_zones.forEach((zone) => {
    const index = resolveHazardTrajectoryIndex(sceneData, zone);
    if (index < 0) {
      return;
    }
    const zones = result.get(index) ?? [];
    zones.push(zone);
    result.set(index, zones);
  });
  return result;
}

function sampleTrajectoryPointIndices(sceneData: SceneResponse, spacingM = 1) {
  const trajectory = sceneData.trajectory;
  if (!trajectory.length) {
    return [];
  }

  const hazardMap = buildHazardTrajectoryMap(sceneData);
  const indices = new Set<number>();
  indices.add(0);
  let lastPoint = trajectory[0];
  for (let index = 1; index < trajectory.length; index += 1) {
    const point = trajectory[index];
    const distance = Math.hypot(point[0] - lastPoint[0], point[1] - lastPoint[1], point[2] - lastPoint[2]);
    if (distance >= spacingM) {
      indices.add(index);
      lastPoint = point;
    }
  }
  indices.add(trajectory.length - 1);
  hazardMap.forEach((_, index) => indices.add(index));
  return [...indices].sort((a, b) => a - b);
}

function resolveTrajectoryDisplayDirection(sceneData: SceneResponse, index: number) {
  const orientation = sceneData.trajectory_orientations?.[index];
  if (orientation && orientation.length >= 4) {
    const quaternion = new THREE.Quaternion(orientation[0], orientation[1], orientation[2], orientation[3]).normalize();
    const forward = new THREE.Vector3(1, 0, 0).applyQuaternion(quaternion);
    forward.z = 0;
    if (forward.lengthSq() > 0.0001) {
      return forward.normalize();
    }
  }

  const current = sceneData.trajectory[index];
  const neighbor = sceneData.trajectory[index + 1] ?? sceneData.trajectory[index - 1];
  if (!current || !neighbor) {
    return null;
  }
  const direction = new THREE.Vector3(neighbor[0] - current[0], neighbor[1] - current[1], 0);
  if (direction.lengthSq() <= 0.0001) {
    return null;
  }
  if (!sceneData.trajectory[index + 1]) {
    direction.multiplyScalar(-1);
  }
  return direction.normalize();
}

function buildDirectionArrow(
  position: THREE.Vector3,
  direction: THREE.Vector3,
  shaftGeometry: THREE.CylinderGeometry,
  headGeometry: THREE.ConeGeometry,
  material: THREE.MeshBasicMaterial,
  shaftLength: number,
  headLength: number,
) {
  const group = new THREE.Group();
  const orientation = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction);
  const shaft = new THREE.Mesh(shaftGeometry, material);
  const head = new THREE.Mesh(headGeometry, material);

  group.quaternion.copy(orientation);
  group.position.copy(position);
  group.position.z += shaftLength * 0.16;
  shaft.position.y = shaftLength * 0.5;
  head.position.y = shaftLength + headLength * 0.5;
  shaft.renderOrder = 4;
  head.renderOrder = 4;
  group.add(shaft, head);
  return group;
}

function buildTrajectoryPoints(sceneData: SceneResponse, groundZ: number, span: number) {
  if (!dynamicGroup || sceneData.trajectory.length === 0) {
    return;
  }

  const hazardMap = buildHazardTrajectoryMap(sceneData);
  const indices = sampleTrajectoryPointIndices(sceneData, 1);
  const pointRadius = clamp(span * 0.002, 0.055, 0.14);
  const markerRadius = clamp(pointRadius * 1.32, 0.075, 0.18);
  const arrowLength = clamp(span * 0.012, 0.36, 0.82);
  const arrowRadius = clamp(pointRadius * 0.42, 0.024, 0.055);
  const shaftLength = arrowLength * 0.58;
  const headLength = arrowLength * 0.42;
  const hazardArrowLength = arrowLength * 1.12;
  const hazardShaftLength = hazardArrowLength * 0.58;
  const hazardHeadLength = hazardArrowLength * 0.42;
  const hazardArrowRadius = arrowRadius * 1.22;
  const zLift = clamp(span * 0.006, 0.14, 0.32);
  const pointGeometry = new THREE.SphereGeometry(pointRadius, 10, 10);
  const markerGeometry = new THREE.SphereGeometry(markerRadius, 12, 12);
  const shaftGeometry = new THREE.CylinderGeometry(arrowRadius * 0.46, arrowRadius * 0.46, shaftLength, 6);
  const headGeometry = new THREE.ConeGeometry(arrowRadius * 1.45, headLength, 8);
  const hazardShaftGeometry = new THREE.CylinderGeometry(
    hazardArrowRadius * 0.5,
    hazardArrowRadius * 0.5,
    hazardShaftLength,
    7,
  );
  const hazardHeadGeometry = new THREE.ConeGeometry(hazardArrowRadius * 1.58, hazardHeadLength, 9);
  const pointMaterial = new THREE.MeshBasicMaterial({
    color: "#a8c9d3",
    transparent: true,
    opacity: 0.42,
    depthTest: true,
    depthWrite: false,
  });
  const arrowMaterial = new THREE.MeshBasicMaterial({
    color: "#d7eef1",
    transparent: true,
    opacity: 0.58,
    depthTest: true,
    depthWrite: false,
  });
  const hazardArrowMaterial = new THREE.MeshBasicMaterial({
    color: "#ff6b4a",
    transparent: true,
    opacity: 0.88,
    depthTest: true,
    depthWrite: false,
  });

  indices.forEach((index) => {
    const point = sceneData.trajectory[index];
    const basePosition = new THREE.Vector3(point[0], point[1], Math.max(point[2], groundZ) + zLift);
    const direction = resolveTrajectoryDisplayDirection(sceneData, index);
    const pointMesh = new THREE.Mesh(pointGeometry, pointMaterial);
    pointMesh.position.copy(basePosition);
    pointMesh.renderOrder = 3;
    pointMesh.userData = { kind: "trajectoryPoint" satisfies SelectableKind, trajectoryIndex: index };
    dynamicGroup?.add(pointMesh);
    selectableMeshes.push(pointMesh);

    if (direction) {
      const arrow = buildDirectionArrow(
        basePosition,
        direction,
        shaftGeometry,
        headGeometry,
        arrowMaterial,
        shaftLength,
        headLength,
      );
      dynamicGroup?.add(arrow);
    }

    const zones = hazardMap.get(index) ?? [];
    zones.forEach((zone, offset) => {
      const markerMaterial = new THREE.MeshBasicMaterial({
        color: "#ff3f2f",
        transparent: true,
        opacity: 0.9,
        depthTest: true,
        depthWrite: false,
      });
      const markerMesh = new THREE.Mesh(markerGeometry, markerMaterial);
      markerMesh.position.copy(basePosition);
      markerMesh.position.z += markerRadius * (2.05 + offset * 1.35);
      markerMesh.renderOrder = 6;
      markerMesh.userData = {
        kind: "hazardPathPoint" satisfies SelectableKind,
        trajectoryIndex: index,
        findingId: zone.finding_id,
      };
      dynamicGroup?.add(markerMesh);
      selectableMeshes.push(markerMesh);
      zoneVisuals.push({ findingId: zone.finding_id, markerMaterial, markerMesh });

      if (direction) {
        const hazardArrowPosition = markerMesh.position.clone();
        hazardArrowPosition.z += markerRadius * 0.52;
        const hazardArrow = buildDirectionArrow(
          hazardArrowPosition,
          direction,
          hazardShaftGeometry,
          hazardHeadGeometry,
          hazardArrowMaterial,
          hazardShaftLength,
          hazardHeadLength,
        );
        hazardArrow.renderOrder = 6;
        dynamicGroup?.add(hazardArrow);
      }
    });
  });
}

function applyZoneSelection() {
  zoneVisuals.forEach((visual) => {
    const selected = visual.findingId === props.selectedFindingId;
    visual.markerMaterial.color.set(selected ? "#ffd0bd" : "#ff3f2f");
    visual.markerMaterial.opacity = selected ? 0.98 : 0.9;
    visual.markerMesh.scale.setScalar(selected ? 1.42 : 1);
  });
}

function updatePlaybackMarker() {
  if (!dynamicGroup || !props.sceneData || !showPathPoints.value) {
    if (playbackMarker) {
      playbackMarker.visible = false;
    }
    return;
  }

  const timestamp = props.activeTimestampMs;
  if (timestamp === null || timestamp === undefined || !Number.isFinite(timestamp)) {
    if (playbackMarker) {
      playbackMarker.visible = false;
    }
    return;
  }

  const index = nearestTrajectoryIndexByTimestamp(props.sceneData, timestamp);
  const point = index >= 0 ? props.sceneData.trajectory[index] : null;
  if (!point) {
    if (playbackMarker) {
      playbackMarker.visible = false;
    }
    return;
  }

  // Reuse cached metrics from rebuild; recompute only when unavailable.
  const metrics = cachedSceneMetrics ?? getSceneMetrics(props.sceneData);
  if (!cachedSceneMetrics) {
    cachedSceneMetrics = {
      min: metrics.min.clone(),
      max: metrics.max.clone(),
      center: metrics.center.clone(),
      span: metrics.span,
    };
  }

  const zLift = clamp(metrics.span * 0.006, 0.14, 0.32);
  const radius = clamp(metrics.span * 0.0035, 0.09, 0.22);

  if (!playbackMarker) {
    playbackMarker = new THREE.Mesh(
      new THREE.SphereGeometry(1, 14, 14),
      new THREE.MeshBasicMaterial({
        color: "#ffd166",
        transparent: true,
        opacity: 0.95,
        depthTest: true,
        depthWrite: false,
      }),
    );
    playbackMarker.renderOrder = 8;
    dynamicGroup.add(playbackMarker);
  }

  playbackMarker.visible = true;
  playbackMarker.scale.setScalar(radius);
  playbackMarker.position.set(point[0], point[1], Math.max(point[2], metrics.min.z) + zLift + radius * 1.2);
}

function startAutoFocus(immediate = false) {
  if (!props.sceneData || !camera || !controls) {
    return;
  }
  const shot = computeCameraShot(props.sceneData, viewPreset.value, getSelectedZone(props.sceneData));
  if (immediate) {
    camera.position.copy(shot.position);
    controls.target.copy(shot.target);
    controls.update();
    transition = null;
    return;
  }
  transition = {
    startPosition: camera.position.clone(),
    endPosition: shot.position.clone(),
    startTarget: controls.target.clone(),
    endTarget: shot.target.clone(),
    startedAt: performance.now(),
    durationMs: 720,
  };
}

function startCameraTransition(position: THREE.Vector3, target: THREE.Vector3, durationMs = 560) {
  if (!camera || !controls) {
    return;
  }
  transition = {
    startPosition: camera.position.clone(),
    endPosition: position,
    startTarget: controls.target.clone(),
    endTarget: target,
    startedAt: performance.now(),
    durationMs,
  };
}

function trajectoryTimestamp(sceneData: SceneResponse, trajectoryIndex: number) {
  const timestamp = sceneData.trajectory_timestamps[trajectoryIndex];
  return Number.isFinite(timestamp) ? timestamp : null;
}

function focusTrajectoryIndex(trajectoryIndex: number, syncVideo = true) {
  if (!props.sceneData || !camera || !controls) {
    return false;
  }
  const timestamp = trajectoryTimestamp(props.sceneData, trajectoryIndex);
  if (syncVideo && timestamp !== null) {
    emit("seek", timestamp);
  }

  const point = props.sceneData.trajectory[trajectoryIndex];
  const orientation = props.sceneData.trajectory_orientations?.[trajectoryIndex];
  if (!point || !orientation || orientation.length < 4) {
    showViewportHint("重建场景后可进入真实车体视角");
    return false;
  }

  const quaternion = new THREE.Quaternion(orientation[0], orientation[1], orientation[2], orientation[3]).normalize();
  const forward = new THREE.Vector3(1, 0, 0).applyQuaternion(quaternion);
  if (forward.lengthSq() < 0.0001) {
    showViewportHint("该路径点缺少有效车体姿态");
    return false;
  }

  forward.normalize();
  const position = new THREE.Vector3(point[0], point[1], point[2] + 1.2);
  const target = position.clone().add(forward.multiplyScalar(8));
  startCameraTransition(position, target, 520);
  return true;
}

function cancelAutoFocus() {
  transition = null;
}

function resetView() {
  viewPreset.value = "perspective";
  startAutoFocus(false);
}

function onPointerDown(event: PointerEvent) {
  if (!renderer || !camera || selectableMeshes.length === 0) {
    return;
  }
  const bounds = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
  pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(selectableMeshes, false);
  const hit = hits[0]?.object;
  if (!hit) {
    return;
  }

  const kind = hit.userData.kind as SelectableKind | undefined;
  const trajectoryIndex = Number(hit.userData.trajectoryIndex ?? -1);
  const findingId = Number(hit.userData.findingId ?? 0);
  if (kind === "hazardPathPoint" && findingId > 0) {
    const timestamp = props.sceneData ? trajectoryTimestamp(props.sceneData, trajectoryIndex) : null;
    emit("select", findingId, timestamp ?? undefined);
    focusTrajectoryIndex(trajectoryIndex, false);
    return;
  }
  if (kind === "trajectoryPoint" && trajectoryIndex >= 0) {
    focusTrajectoryIndex(trajectoryIndex);
  }
}

onMounted(() => {
  initStage();
});

watch(
  () => props.sceneData,
  (sceneData, previous) => {
    clearSelectionCache(true);
    lastSelectionStats.value = null;
    suppressRebuildWatchers = true;
    if (sceneData) {
      fullHeightMode.value = true;
      cutHeightM.value = Number((sliderConfig.value.max || sceneData.cut_height_default || 0).toFixed(2));
      floorCutHeightM.value = Number((sliderConfig.value.min || 0).toFixed(2));
    }
    if (sceneData !== previous) {
      autoPointAggregation.value = true;
      pointDensityScale.value = 2.75;
      autoPointDensityScale.value = 2.75;
    }
    suppressRebuildWatchers = false;
    if (sceneData !== previous) {
      scheduleSceneRebuild(true);
    }
  },
  { immediate: true },
);

watch(
  () => cutHeightM.value,
  () => {
    if (suppressRebuildWatchers) {
      return;
    }
    clearSelectionCache(true);
    scheduleSceneRebuild(false);
  },
);

watch(
  () => floorCutHeightM.value,
  () => {
    if (suppressRebuildWatchers) {
      return;
    }
    clearSelectionCache(true);
    scheduleSceneRebuild(false);
  },
);

watch(() => pointSizeScale.value, () => {
  if (!suppressRebuildWatchers) {
    scheduleSceneRebuild(false);
  }
});
watch(() => pointDensityScale.value, () => {
  if (suppressRebuildWatchers) {
    return;
  }
  clearSelectionCache(true);
  if (autoPointAggregation.value && props.sceneData) {
    autoPointDensityScale.value = resolveAutoPointDensityScale(props.sceneData);
  }
  scheduleSceneRebuild(false);
});
watch(() => autoPointAggregation.value, () => {
  if (suppressRebuildWatchers) {
    return;
  }
  clearSelectionCache(true);
  if (autoPointAggregation.value) {
    applyAutoPointAggregation(true);
    return;
  }
  scheduleSceneRebuild(false);
});
watch(() => structureClarity.value, () => scheduleSceneRebuild(false));
watch(() => colorMode.value, () => scheduleSceneRebuild(false));
watch(() => showPathPoints.value, () => scheduleSceneRebuild(false));
watch(() => hasRgbColor.value, (available) => {
  if (!available && colorMode.value === "rgb") {
    colorMode.value = "structure";
  }
});
watch(() => viewPreset.value, () => startAutoFocus(false));
watch(() => props.selectedFindingId, () => {
  applyZoneSelection();
  if (!props.sceneData || props.selectedFindingId === null) {
    return;
  }
  const zone = getSelectedZone(props.sceneData);
  if (zone) {
    const trajectoryIndex = resolveHazardTrajectoryIndex(props.sceneData, zone);
    if (trajectoryIndex >= 0) {
      focusTrajectoryIndex(trajectoryIndex);
    }
  }
});
watch(() => props.activeTimestampMs, () => {
  updatePlaybackMarker();
});

onBeforeUnmount(() => {
  window.cancelAnimationFrame(animationId);
  if (autoAggregationTimer !== 0) {
    window.clearTimeout(autoAggregationTimer);
    autoAggregationTimer = 0;
  }
  if (rebuildTimer !== 0) {
    window.clearTimeout(rebuildTimer);
    rebuildTimer = 0;
  }
  cancelDeferredDenseRender();
  if (viewportHintTimer !== 0) {
    window.clearTimeout(viewportHintTimer);
    viewportHintTimer = 0;
  }
  window.removeEventListener("resize", resizeRenderer);
  resizeObserver?.disconnect();
  if (renderer) {
    renderer.domElement.removeEventListener("pointerdown", onPointerDown);
    renderer.dispose();
  }
  if (controls) {
    controls.removeEventListener("start", cancelAutoFocus);
    controls.dispose();
  }
  clearDynamic();
  disposeEdlResources();
  if (host.value && renderer?.domElement.parentElement === host.value) {
    host.value.removeChild(renderer.domElement);
  }
  renderer = null;
  stage = null;
  camera = null;
  controls = null;
});
</script>

<template>
  <div class="scene-stage">
    <div ref="host" class="scene-canvas-host"></div>

    <div v-if="hasRenderableScene && !renderError" class="scene-overlay">
      <div class="scene-topbar">
        <div class="hud-cluster">
          <span v-for="item in activeSceneStats" :key="item" class="scene-hud-badge">{{ item }}</span>
          <span v-if="viewportHint" class="scene-hud-badge scene-hud-warning">{{ viewportHint }}</span>
        </div>

        <div class="scene-topbar-actions">
          <button class="toolbar-button scene-control-toggle" :class="{ active: showPathPoints }" @click="showPathPoints = !showPathPoints">
            {{ showPathPoints ? "隐藏路径" : "显示路径" }}
          </button>
          <button class="toolbar-button scene-control-toggle" :class="{ active: !fullHeightMode }" @click="toggleFullHeightMode">
            {{ fullHeightMode ? "切顶切底" : "全量高度" }}
          </button>
          <button class="toolbar-button scene-control-toggle" :class="{ active: showRenderControls }" @click="showRenderControls = !showRenderControls">
            {{ showRenderControls ? "隐藏参数" : "显示参数" }}
          </button>
        </div>
      </div>

      <div v-if="showRenderControls" class="scene-toolbar">
          <div class="toolbar-group toolbar-slider">
            <span class="toolbar-label">切顶高度</span>
            <input
              v-model.number="cutHeightM"
              class="toolbar-range"
              type="range"
              :min="sliderConfig.min"
              :max="sliderConfig.max"
              :step="sliderConfig.step"
            />
            <span class="toolbar-value">{{ cutHeightM.toFixed(2) }} m</span>
          </div>

          <div class="toolbar-group toolbar-slider">
            <span class="toolbar-label">离地高度</span>
            <input
              v-model.number="floorCutHeightM"
              class="toolbar-range"
              type="range"
              :min="floorSliderConfig.min"
              :max="floorSliderConfig.max"
              :step="floorSliderConfig.step"
            />
            <span class="toolbar-value">{{ floorCutHeightM.toFixed(2) }} m</span>
          </div>

          <div class="toolbar-group toolbar-slider">
            <span class="toolbar-label">{{ autoPointAggregation ? "密度上限" : "点密度" }}</span>
            <input
              v-model.number="pointDensityScale"
              class="toolbar-range"
              type="range"
              min="0.75"
              max="4"
              step="0.05"
            />
            <span class="toolbar-value">{{ pointDensityScale.toFixed(2) }}x</span>
          </div>

          <div class="toolbar-group">
            <button class="toolbar-button" :class="{ active: autoPointAggregation }" @click="autoPointAggregation = !autoPointAggregation">
              {{ autoPointAggregation ? "自动聚合" : "手动密度" }}
            </button>
          </div>

          <div class="toolbar-group toolbar-slider">
            <span class="toolbar-label">点大小</span>
            <input
              v-model.number="pointSizeScale"
              class="toolbar-range"
              type="range"
              min="0.45"
              max="1.9"
              step="0.05"
            />
            <span class="toolbar-value">{{ pointSizeScale.toFixed(2) }}x</span>
          </div>

          <div class="toolbar-group toolbar-slider">
            <span class="toolbar-label">轮廓强度</span>
            <input
              v-model.number="structureClarity"
              class="toolbar-range"
              type="range"
              min="0.55"
              max="1.8"
              step="0.05"
            />
            <span class="toolbar-value">{{ structureClarity.toFixed(2) }}x</span>
          </div>

          <div class="toolbar-group toolbar-select-group">
            <span class="toolbar-label">着色</span>
            <select v-model="colorMode" class="toolbar-select">
              <option
                v-for="option in colorModeOptions"
                :key="option.value"
                :value="option.value"
                :disabled="option.value === 'rgb' && !hasRgbColor"
              >
                {{ option.label }}
              </option>
            </select>
          </div>

          <div class="toolbar-group">
            <button class="toolbar-button" :class="{ active: edlEnabled && edlAvailable }" @click="edlEnabled = !edlEnabled">
              {{ edlEnabled && edlAvailable ? "轮廓增强" : "标准深度" }}
            </button>
          </div>

          <div class="toolbar-group">
            <button class="toolbar-button" :class="{ active: viewPreset === 'perspective' }" @click="viewPreset = 'perspective'">透视</button>
            <button class="toolbar-button" :class="{ active: viewPreset === 'top' }" @click="viewPreset = 'top'">顶视</button>
            <button class="toolbar-button" @click="resetView">重置</button>
          </div>

        </div>
    </div>

    <div v-if="renderError" class="scene-empty">
      <strong>三维场景渲染失败</strong>
      <span>{{ renderError }}</span>
    </div>

    <div v-else-if="!hasRenderableScene" class="scene-empty">
      <strong>真实场景地图尚未生成</strong>
      <span>导入或重建场景后，这里会显示 roof-off 点云。</span>
    </div>
  </div>
</template>
