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
        +--> 规则解析
        +--> 视频生成 / 证据帧缓存
        +--> 大模型分析
        +--> 场景重建
        |
        v
.runtime/projects/<project_id>/
```

## 2. 前端架构

前端位于 `web/`，基于 Vue 3 单页应用。

### 2.1 关键模块

- `src/App.vue`
  - 页面主壳
  - 维护当前项目、当前 finding、当前场景状态

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

- `src/components/ImportPanel.vue`
  - 项目导入入口

- `src/components/AnalysisPanel.vue`
  - 分析模式切换、模型切换、分析触发

- `src/components/FindingsPanel.vue`
  - findings 列表

- `src/components/FindingDetailPanel.vue`
  - finding 详情和复核信息

- `src/components/VideoEvidencePanel.vue`
  - 视频与证据帧联动

- `src/components/SceneViewport.vue`
  - Three.js 三维场景可视化

### 2.2 前端运行模式

- 开发模式：
  - Vite dev server 运行在 `5173`
  - `/api`、`/artifacts` 通过 `vite.config.ts` 代理到 `8010`

- 生产模式：
  - 由 Nginx 提供静态资源
  - Nginx 将 `/api`、`/artifacts` 反代到后端容器

## 3. 后端架构

后端位于 `backend/`，基于 FastAPI + SQLModel。

### 3.1 入口层

- `app/main.py`
  - FastAPI app 初始化
  - CORS 中间件
  - 路由注册
  - `/healthz`
  - `/artifacts` 静态文件挂载

### 3.2 路由层

- `app/routers/projects.py`
  - 项目导入、查询、分析、场景重建、证据帧访问

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
  - API 请求 / 响应 schema

### 3.4 服务层

- `services/import_pipeline.py`
  - 项目导入主编排

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

- `services/sfm_reconstruction.py`
  - 基于 COLMAP 的图像重建场景

- `services/analysis.py`
  - findings 清理、分析调用、结果入库、hazard zone 生成

- `services/provider.py`
  - 大模型 provider 分析主链路

- `services/evidence.py`
  - 证据帧缓存与按时间戳提取

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

## 6. 外部依赖

### 6.1 强依赖

- Python 3.11+
- Node.js 20+
- FFmpeg

### 6.2 可选依赖

- DashScope 或兼容模型服务
- COLMAP
- Ollama，仅用于独立样例

### 6.3 内置资源

为降低交付复杂度，以下资源已经内置到仓库：

- `config/security_check.yaml`
- `backend/rosenv/tools/*.py`
- `backend/rosenv/msg_display/.../*.msg`

## 7. 配置设计

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

这意味着：

- 本地运行可以继续直接使用仓库默认结构
- Docker 运行可以通过挂载目录或环境变量切换数据源
