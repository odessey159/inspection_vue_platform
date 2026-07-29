# Architecture

## 1. 总体架构

系统由四个层次组成：

1. 前端展示层
2. 后端 API 与业务编排层
3. 运行时数据层
4. 外部工具 / 模型服务层

```text
Vue 3 + Vite + Three.js
        |
        v
FastAPI + SQLModel + SQLite
        |
        +--> rosbag 提取脚本
        +--> 规则解析 / Rule RAG
        +--> 视频生成 / 证据帧缓存
        +--> demo / provider / provider_yolo 分析
        +--> RTSP 看门狗 / 录制 / 时间轴
        +--> 场景重建 / 场景传输压缩
        |
        +--> YOLO 推理服务 (可选, :8001)
        +--> DashScope 兼容多模态接口 (可选)
        |
        v
.runtime/projects/<project_id>/
.runtime/robots/<vehicle_id>/
.runtime/rtsp_recordings/
```

## 2. 前端架构

前端位于 `web/`，基于 Vue 3 单页应用。

### 2.1 关键模块

- `src/App.vue`
  - 页面主壳
  - 维护当前项目、当前 finding、当前场景状态
  - 选车后加载车端地图，并按项目视频时钟对齐轨迹时间戳

- `src/lib/api.ts`
  - 所有后端请求统一入口
  - 主要 API：
    - `/api/bootstrap`
    - `/api/projects`
    - `/api/projects/import`
    - `/api/projects/{id}/analyze`
    - `/api/projects/{id}/scene`
    - `/api/projects/{id}/findings`
    - `/api/projects/{id}/rules`
    - `/api/rtsp-vehicles/{id}/scene`
    - `/api/rtsp-playback-state` 等 RTSP 接口

- `src/lib/sceneTime.ts`
  - 地图轨迹与视频时钟对齐（与后端 `align_scene_timestamps_to_video` 同思路）

- `src/lib/analysisMode.ts` / `analysisConfig.ts`
  - 分析模式选择与默认配置（默认 `provider_yolo`）

- `src/components/ImportPanel.vue`
  - 项目导入入口（rosbag / RTSP 车辆）

- `src/components/AnalysisPanel.vue`
  - 分析模式切换、模型切换、分析触发

- `src/components/FindingsPanel.vue`
  - findings 列表

- `src/components/FindingDetailPanel.vue`
  - finding 详情和复核信息

- `src/components/VideoEvidencePanel.vue`
  - 视频与证据帧联动；按绝对时间戳（`video_start_ts + currentTime`）同步轨迹

- `src/components/SceneViewport.vue`
  - Three.js 三维场景可视化；轨迹点点击驱动视频 seek

### 2.2 前端运行模式

- 开发模式（本地）：
  - Vite dev server 运行在 `5173`
  - `/api`、`/artifacts` 通过 `vite.config.ts` 代理到 `8010`

- Docker Compose 开发模式：
  - `web/Dockerfile.dev` 运行 Vite，宿主机 `8700` → 容器 `5173`
  - `VITE_API_PROXY` 指向 `backend:8010`，支持 HMR

- 生产模式：
  - 由 Nginx 提供静态资源（`web/Dockerfile` + `nginx.conf`）
  - Nginx 将 `/api`、`/artifacts` 反代到后端容器

## 3. 后端架构

后端位于 `backend/`，基于 FastAPI + SQLModel。

### 3.1 入口层

- `app/main.py`
  - FastAPI app 初始化
  - CORS / GZip 中间件
  - 路由注册
  - `/healthz`
  - `/artifacts` 静态文件挂载
  - 生命周期：初始化 DB、机器人运行时目录、RTSP 录制清理线程、RTSP 看门狗

### 3.2 路由层

- `app/routers/projects.py`
  - 项目导入、查询、分析、场景重建、证据帧访问
  - RTSP 直播 / 录制 / 看门狗设置
  - 车端地图预览：`GET /api/rtsp-vehicles/{vehicle_id}/scene`

- `app/routers/findings.py`
  - findings 复核状态更新

### 3.3 数据层

- `app/db.py`
  - SQLite 引擎与 session

- `app/models.py`
  - `Project`
  - `HazardRule`
  - `Finding`
  - `HazardZone`

- `app/schemas.py`
  - API 请求 / 响应 schema（含 RTSP playback 的 live/recorded `video_start_ts`）

### 3.4 服务层

- `services/import_pipeline.py`
  - 项目导入主编排（rosbag / RTSP）

- `services/rosbag.py`
  - rosbag 元数据解析和主题推断

- `services/extractors.py`
  - 通过内置 `backend/rosenv/tools` 提取相机 / 点云 / 位姿数据

- `services/rules.py`
  - 标准文档解析

- `services/video.py`
  - 由相机帧生成 `inspection.mp4`

- `services/scene.py`
  - lidar 主场景生成

- `services/scene_rebuild.py`
  - 重新提取并重建 lidar 场景

- `services/scene_transport.py`
  - 压缩大体积 `scene.json`（合并重复点云层），缓存 `scene.web.json`

- `services/sfm_reconstruction.py`
  - 基于 COLMAP 的图像重建场景

- `services/analysis.py`
  - findings 清理、分析调用、结果入库、hazard zone 生成

- `services/provider.py`
  - 大模型 provider 分析主链路

- `services/provider_YOLO.py`
  - YOLO 预检 + 大模型复核（视频 clip / RTSP 分段）

- `services/rule_retriever.py` / `rule_db.py` / `object_alias.py`
  - 可选 Rule RAG

- `services/evidence.py`
  - 证据帧缓存与按时间戳提取

- `services/storage.py`
  - JSON 读写、跨平台 `resolve_project_path` / `to_project_relative_path`

- `services/rtsp_vehicles.py`
  - 车辆配置与 `.runtime/robots/<id>/{recordings,maps}` 目录

- `services/rtsp_recorder.py`
  - RTSP 录制、回放状态、RTSP 项目导入、地图时间对齐

- `services/rtsp_timeline.py`
  - 从 RTSP 时间戳条码 / `/time` 旁路 / 地图原点解析录制 `video_start_ts`

- `services/rtsp_watchdog.py`
  - 后台轮询车辆流并自动录制 / 触发分析

- `services/rtsp_live.py`
  - MJPEG 浏览器预览

- `services/rtsp_auto_analysis.py`
  - 流上线后自动触发分析

- `services/rtsp_yolo_monitor.py` / `rtsp_yolo_llm_chain.py`
  - 直播分段 YOLO + 可选 LLM 复核链路

## 4. 运行时数据结构

后端运行时目录默认为 `.runtime/`。

### 4.1 数据库

- `.runtime/inspection.db`
  - SQLite 数据库

### 4.2 项目产物

每个项目会生成到：

```text
.runtime/projects/<project_id>/
├─ artifacts/
│  ├─ inspection.mp4
│  └─ analysis_clips/
├─ manifests/
│  └─ video_manifest.json
├─ scenes/
│  ├─ scene.json
│  └─ scene_sfm.json
├─ summaries/
│  ├─ rosbag_summary.json
│  ├─ dataset_summary.json
│  ├─ rules.json
│  ├─ analysis_summary.json
│  └─ sfm_summary.json
├─ extracted/
│  ├─ camera_lidar_pairs/
│  └─ pose_calibration/
└─ evidence_frames/
```

### 4.3 RTSP / 车端运行时

```text
.runtime/
├─ rtsp_recordings/
│  ├─ <key>_....mp4
│  └─ <key>_....meta.json      # video_start_ts + source
├─ robots/<vehicle_id>/
│  ├─ recordings/
│  └─ maps/
│     ├─ scene.json            # 车端点云地图（可选）
│     └─ scene.web.json        # 传输压缩缓存
└─ YOLO_log/
```

## 5. 分析链路架构

### 5.1 Demo 模式

`analysis.py` 中的 `demo` 模式不调用模型，只是：

1. 选择前几条可视规则
2. 人工生成时间窗
3. 构造演示 finding
4. 入库并生成 hazard zone

### 5.2 Provider 模式

主项目 provider 链路当前针对 DashScope 兼容接口设计：

1. 读取 `inspection.mp4`
2. 按 `VISION_CLIP_SECONDS` 切片
3. 压缩片段到可传输大小
4. 将片段以 Base64 `video_url` 发给兼容接口
5. 要求返回结构化 JSON
6. 解析结果，换算绝对时间戳
7. 写入 findings
8. 生成 hazard zone 和证据帧

### 5.3 Provider YOLO 模式

默认推荐模式：

1. 切分巡检视频（或对 RTSP 分段）
2. 调用 YOLO 服务 `/predict/video` 或 `/predict/rtsp`
3. （可选）Rule RAG 按检测类别检索规则片段
4. 将检测结果 + 规则 + 视频帧送入大模型复核
5. 写入 findings / hazard zone / 证据帧

RTSP 直播路径还可由 `rtsp_yolo_monitor` 持续分段检测，并经 `rtsp_yolo_llm_chain` 异步复核。

## 6. RTSP 时间轴与场景同步

目标：视频播放时钟与三维轨迹时间戳共用同一原点。

```text
测试发布端 generate_rtsp_stream
  └─ /live + /time（画面内嵌时间戳条码，PTS 映射到地图 epoch）
        |
        v
rtsp_timeline.resolve_recording_video_start_ts
  ├─ 优先采样条码
  ├─ 其次车端地图轨迹原点
  └─ 最后墙钟
        |
        v
录制 *.meta.json + dataset_summary.video_start_ts
        |
        +--> align_scene_timestamps_to_video（后端导入/升级场景）
        +--> sceneTime.ts（前端加载车端地图时对齐）
        |
        v
VideoEvidencePanel ↔ SceneViewport 绝对时间联动
```

场景 JSON 经 `scene_transport.compact_scene_payload` 压缩后再通过 API 返回，避免多份重复点云层拖慢传输。

## 7. 外部依赖

### 7.1 强依赖

- Python 3.11+
- Node.js 18+
- FFmpeg

### 7.2 可选依赖

- DashScope 或兼容模型服务（`provider` / `provider_yolo`）
- 自托管 YOLO 服务（`provider_yolo` / RTSP 监控）
- COLMAP（SFM）

### 7.3 内置资源

为降低交付复杂度，以下资源已经内置到仓库：

- `config/security_check.yaml`
- `backend/rosenv/tools/*.py`
- `backend/rosenv/msg_display/.../*.msg`
- `backend/config/rtsp_vehicles.yaml`
- `backend/config/object_aliases.yaml`
- `backend/models/YOLO/checklist_YOLO.txt`（权重需自行放置）

## 8. 配置设计

后端已改为容器友好的路径配置方案，主要通过环境变量控制：

- `APP_HOME`
- `RUNTIME_DIR`
- `PROJECTS_DIR`
- `DATABASE_PATH`
- `INPUTS_DIR`
- `CONFIG_DIR`
- `ROSENV_DIR`
- `DEFAULT_STANDARDS_DIR`
- `SECURITY_CHECK_CALIBRATION_PATH`
- `DISCOVERY_ROOTS`
- `YOLO_API_URL` / `YOLO_*`
- `RTSP_WATCH_*` / `RTSP_YOLO_MONITOR_*`
- `RULE_RAG_*`

这意味着：

- 本地运行可以继续直接使用仓库默认结构
- Docker 运行可以通过挂载目录或环境变量切换数据源
- 项目内文件路径以相对路径落库，跨 Windows / Linux 挂载时可再解析
