# Inspection Vue Platform

工业巡检隐患识别与三维工作台系统。

本项目提供一套从 ROS 2 rosbag 或 RTSP 实时流导入、标准规则解析、巡检视频生成、YOLO + 大模型隐患识别、证据帧缓存，到三维场景联动展示的完整工作台。前端为 Vue 3 + Vite + Three.js，后端为 FastAPI + SQLModel + SQLite，YOLO 检测以独立微服务形式运行。

## 1. 项目概览

### 1.1 目标

系统用于支撑以下完整业务链路：

1. 导入 ROS 2 rosbag 数据目录，或绑定巡检小车 RTSP 流
2. 解析相机、激光雷达、位姿等主题数据（rosbag 路径）
3. 解析行业标准文档并生成结构化隐患规则
4. 生成巡检视频与三维场景（含车端地图预览与传输压缩）
5. 通过 Demo、Provider 或 YOLO + Provider 模式生成隐患识别结果
6. 在视频、证据帧、规则详情和三维场景之间建立时间轴联动
7. 对 RTSP 流进行自动录制、实时预览、时间轴对齐与可选的自动分析

### 1.2 当前交付范围

当前仓库已经包含以下能力：

- 项目导入与运行时项目管理
- **RTSP 巡检小车导入**：从 `rtsp_vehicles.yaml` 选择车辆，绑定 RTSP 地址创建项目
- **RTSP 看门狗**：后台轮询车辆流，流上线时自动录制；支持测试模式（时长/数量上限）
- **RTSP 实时预览**：MJPEG 直播与项目级 RTSP 回放
- **RTSP 自动分析**：流连接后可按配置自动触发 `provider_yolo` 分析
- **RTSP 时间轴对齐**：录制时优先从流内时间戳条码 / `/time` 旁路采样 `video_start_ts`，并写入 `*.meta.json`，使视频时钟与车端地图轨迹共用同一原点
- **车端地图预览**：按车辆加载 `.runtime/robots/<id>/maps/scene.json`，导入前即可在三维视口预览
- **场景传输压缩**：API 下发时合并重复点云层，并缓存 `scene.web.json`，加快大地图加载
- **地图–视频时间同步**：轨迹点击与视频播放按绝对时间戳联动；时钟不一致时线性重映射
- rosbag 元数据识别和主题推断
- 相机图像 / 点云 / 位姿提取
- 巡检视频 `inspection.mp4` 生成
- 标准文档规则解析
- 基于规则的 `demo` 分析模式
- 基于 DashScope 兼容接口的 `provider` 分析模式
- **基于 YOLO 预检 + 大模型复核的 `provider_yolo` 分析模式**（默认推荐）
- **独立 YOLO 推理服务**（`/predict/video`、`/predict/rtsp`）
- **可选 Rule RAG**：按检测目标检索相关规则片段，减少 prompt 体积
- 三维场景重建与展示
- 基于图像的 SFM 场景重建的初步实现
- 证据帧缓存与 findings 复核
- **跨平台路径解析**：项目产物路径以相对路径持久化，兼容 Windows 宿主机 ↔ Linux/Docker

### 1.3 交付包含内容

- 后端服务代码：`backend/`
- 前端工作台代码：`web/`
- YOLO 推理服务：`backend/yolo_service/`
- 容器化交付文件：
  - `backend/Dockerfile`
  - `web/Dockerfile`
  - `web/nginx.conf`
  - `docker-compose.yml`
  - `docker-run.ps1` / `docker-run.sh`（一键启动辅助脚本）
- 配置模板：
  - `.env.example`
  - `backend/.env.example`
  - `web/.env.example`
- 业务配置：
  - `config/security_check.yaml`（标定）
  - `backend/config/object_aliases.yaml`（检测类别别名，供 Rule RAG 使用）
  - `backend/config/rtsp_vehicles.yaml`（巡检小车 RTSP 列表）
- 模型与工具资源：
  - `backend/models/YOLO/`（权重文件需自行放置，见下文）
  - `backend/rosenv/`（rosbag 提取脚本与消息定义）

## 2. 仓库结构

```text
inspection_vue_platform/
├─ backend/                     # FastAPI 后端
│  ├─ app/
│  │  ├─ main.py                # FastAPI 入口（含 RTSP 看门狗生命周期）
│  │  ├─ db.py                  # SQLite / SQLModel 初始化
│  │  ├─ models.py              # 数据模型
│  │  ├─ schemas.py             # API schema
│  │  ├─ routers/               # API 路由
│  │  └─ services/              # 核心业务服务
│  │     ├─ provider.py         # 大模型分析
│  │     ├─ provider_YOLO.py    # YOLO + Provider 联合分析
│  │     ├─ rtsp_*.py           # RTSP 录制、看门狗、直播、自动分析、时间轴
│  │     ├─ scene_transport.py  # 大场景 JSON 压缩与 web 缓存
│  │     ├─ rule_retriever.py   # Rule RAG 检索
│  │     └─ ...
│  ├─ yolo_service/             # 独立 YOLO 推理服务（默认端口 8001）
│  ├─ config/                   # object_aliases.yaml、rtsp_vehicles.yaml
│  ├─ models/YOLO/              # YOLO 权重与类别说明
│  ├─ scripts/run_yolo_service.ps1
│  ├─ rosenv/                   # 内置 rosbag 提取脚本与消息定义
│  ├─ tests/                    # 后端测试（含 RTSP / YOLO / 时间轴用例）
│  ├─ requirements.txt
│  ├─ requirements-yolo.txt     # YOLO 服务额外依赖
│  └─ Dockerfile
├─ web/                         # Vue 3 前端
│  ├─ src/
│  │  ├─ lib/
│  │  │  ├─ analysisMode.ts    # 分析模式选择与文案
│  │  │  └─ sceneTime.ts       # 地图轨迹与视频时钟对齐
│  │  └─ components/            # ImportPanel、SceneViewport、VideoEvidencePanel 等
│  ├─ package.json
│  ├─ nginx.conf                # 生产静态部署
│  ├─ Dockerfile                # 生产镜像
│  ├─ Dockerfile.dev            # Compose 开发镜像（Vite HMR）
│  └─ docker-entrypoint.dev.sh
├─ config/                      # 校准等静态配置
├─ inputs/                      # 推荐挂载的输入目录（rosbag / standards）
├─ .runtime/                    # 运行时数据库和项目产物
│  ├─ YOLO_log/                 # YOLO 检测日志
│  ├─ rtsp_recordings/          # RTSP 看门狗录制（含 *.meta.json）
│  └─ robots/<vehicle_id>/      # 车端录制与 maps/scene.json
├─ docker-compose.yml
├─ docker-run.ps1
├─ docker-run.sh
├─ ARCHITECTURE.md
└─ CONFIGURATION.md
```

## 3. 快速开始

### 3.1 本地开发运行

**后端：**

```powershell
Set-Location backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload
```

**YOLO 推理服务**（使用 `provider_yolo` 模式时需要，独立进程）：

```powershell
# 先将权重放到 backend/models/YOLO/security_check_540.pt
Set-Location backend
.\scripts\run_yolo_service.ps1
```

服务默认监听 `http://127.0.0.1:8001`，健康检查：`/healthz`。

**前端：**

```powershell
Set-Location web
npm install
npm run dev
```

访问地址：

- 前端开发地址：`http://127.0.0.1:5173`
- 后端 API：`http://127.0.0.1:8010`
- YOLO 服务：`http://127.0.0.1:8001`
- 健康检查：`http://127.0.0.1:8010/healthz`

### 3.2 Docker 一键启动

```powershell
Copy-Item .env.example .env
# 或使用辅助脚本（会自动创建 .env 与 inputs 目录）
.\docker-run.ps1
```

Linux / macOS：

```bash
cp .env.example .env
./docker-run.sh
```

启动后访问：

- Web：`http://127.0.0.1:8700`
- API：`http://127.0.0.1:8010`

**注意：**

- Docker Compose 当前包含 `backend` 与 `web` 容器；`web` 默认使用 `Dockerfile.dev`（Vite 开发服 + HMR，宿主机 `8700` 映射容器 `5173`）。生产静态构建仍可用 `web/Dockerfile` + Nginx。
- YOLO 服务默认在**宿主机**运行（端口 8001），后端通过 `YOLO_API_URL=http://host.docker.internal:8001` 访问。若需完整 `provider_yolo` 能力，请先在宿主机启动 YOLO 服务。

### 3.3 首次导入前准备

#### rosbag 导入

请将以下输入数据放到 `inputs/` 下，或通过环境变量修改扫描目录：

- rosbag 目录
- 标准文档目录（`.docx` / `.xlsx`）

推荐结构：

```text
inputs/
├─ bags/
│  └─ your_rosbag/
└─ standards/
   └─ your_standard_docs/
```

补充说明：

- 如果你当前已经把 rosbag 目录直接放在项目根目录，本地开发阶段可以直接使用，不需要强制移动。
- 当前默认 `DISCOVERY_ROOTS` 同时包含项目根目录和 `inputs/`，因此根目录下的 rosbag 目录也能被扫描到。
- 但在交付和 Docker 部署场景下，仍建议把 rosbag 放到 `inputs/bags/`，把标准文档放到 `inputs/standards/`，这样目录结构更稳定，也更便于挂载和运维。

#### RTSP 导入

1. 编辑 `backend/config/rtsp_vehicles.yaml`，配置巡检小车 ID、名称与 RTSP 地址。
2. 确保 RTSP 流可访问（本地测试可用 `backend/tests/start_rtsp_server.ps1` 配合 `generate_rtsp_stream.py`；测试流会同时发布 `/live` 与带时间戳条码的 `/time`）。
3. （可选）将车端点云地图放到 `.runtime/robots/<vehicle_id>/maps/scene.json`，前端选车后可立即预览。
4. 在前端「选择巡检小车」步骤选取车辆，或手动填写 RTSP URL 导入项目。

## 4. 核心能力说明

### 4.1 项目导入

导入接口：`POST /api/projects/import`

支持两种数据源：

| 来源 | 说明 |
|------|------|
| rosbag 目录 | 传统离线导入，完整提取点云、位姿、视频 |
| RTSP URL | 实时流项目，录制/关联 RTSP 视频作为分析输入 |

rosbag 导入时后端会执行：

1. 校验 rosbag 目录与标准目录
2. 读取 rosbag 元数据并推断视频 / 点云 / 位姿主题
3. 提取相机图像与点云配对数据
4. 提取位姿与 TF 数据
5. 生成 `dataset_summary.json`
6. 解析标准规则，生成 `rules.json`
7. 构建场景 `scene.json`
8. 生成巡检视频 `inspection.mp4`
9. 写入 SQLite 数据库和项目运行时目录

### 4.2 分析模式

系统支持三种分析模式：

| 模式 | 说明 |
|------|------|
| `demo` | 不调用真实大模型；依据前几条可视规则生成演示 findings，用于联调前端和工作台流程 |
| `provider` | 调用 DashScope 兼容多模态接口进行真实分析；识别结果写入 findings 并缓存证据帧 |
| `provider_yolo` | **默认推荐**。先用 YOLO 服务对视频/RTSP 流做目标检测，再将检测结果与相关规则片段一并送入大模型复核，提高准确性与效率 |

前端默认分析模式为 `provider_yolo`（见 `web/src/lib/analysisConfig.ts`）。若 YOLO 或大模型未配置，分析运行后会返回相应诊断信息。

`provider_yolo` 典型流程：

1. 将巡检视频切分为 clip
2. 调用 YOLO 服务 `/predict/video`（或 RTSP 场景下 `/predict/rtsp`）获取检测框与时间戳
3. （可选）通过 Rule RAG 按检测类别检索相关规则
4. 将 YOLO 结果 + 规则片段 + 视频帧送入大模型生成 findings

### 4.3 YOLO 推理服务

YOLO 服务位于 `backend/yolo_service/`，与主后端解耦部署。

| 端点 | 用途 |
|------|------|
| `GET /healthz` | 健康检查 |
| `POST /predict/video` | 上传视频文件进行逐帧检测 |
| `POST /predict/rtsp` | 对 RTSP 流分段检测（支持 duration、segment 参数） |

主要配置（见 `.env.example`）：

- `YOLO_WEIGHTS_PATH`：权重路径，默认 `backend/models/YOLO/security_check_540.pt`
- `YOLO_SERVICE_PORT`：默认 `8001`
- `YOLO_IMGSZ` / `YOLO_CONFIDENCE`：推理尺寸与置信度阈值
- `YOLO_LOG_DIR`：检测日志输出目录（默认 `.runtime/YOLO_log`）

当前模型支持 19 类工业安全相关目标，类别说明见 `backend/models/YOLO/checklist_YOLO.txt`。

启动方式：

```powershell
Set-Location backend
.\scripts\run_yolo_service.ps1
```

依赖安装使用 `requirements-yolo.txt`（基于 `ultralytics`、`opencv-python-headless`）。

### 4.4 RTSP 实时巡检

后端启动时会自动拉起 RTSP 看门狗（`rtsp_watchdog`），轮询 `rtsp_vehicles.yaml` 中的流地址：

- 流上线 → 自动开始录制到 `.runtime/rtsp_recordings/`（或车辆目录下的 recordings）
- 录制开始前通过 `rtsp_timeline` 解析视频时钟原点（优先流内时间戳条码 / `/time`，其次车端地图轨迹原点，最后回退墙钟），并写入同名 `*.meta.json`
- 流断开 → 停止录制，可选触发自动分析（`RTSP_WATCH_AUTO_ANALYSIS_MODE`）
- 测试模式（`RTSP_WATCH_TEST_MODE=true`）：单次录制最长 10 分钟，每车最多保留 5 段，超出时删除最旧录制

相关 API：

| 端点 | 说明 |
|------|------|
| `GET /api/rtsp-live` | MJPEG 实时预览 |
| `GET /api/projects/{id}/rtsp-live` | 项目级 RTSP 直播 |
| `GET /api/rtsp-playback-state` | 查询录制/播放状态（含 live/recorded `video_start_ts`） |
| `GET /api/rtsp-recordings/{key}/latest` | 获取最新录制文件 |
| `DELETE /api/rtsp-recordings` | 清空所有 RTSP 录制 |
| `GET/PATCH /api/rtsp-watch-settings` | 看门狗测试模式开关 |
| `GET /api/rtsp-vehicles/{vehicle_id}/scene` | 加载车端 `maps/scene.json`（经传输压缩）预览 |

### 4.5 Rule RAG（可选）

默认关闭（`RULE_RAG_ENABLED=false`），开启后会：

1. 根据 YOLO 检测到的目标类别，通过 `object_aliases.yaml` 映射到规则关键词
2. 从 `rules.db` 检索 Top-K 相关规则片段
3. 将检索结果写入分析 artifact，并注入大模型 prompt

适用于规则库较大、希望减少 token 消耗的场景。关闭时仍使用完整规则 prompt（与早期行为一致）。

### 4.6 三维场景与时间同步

系统支持两种场景来源：

- `lidar`
  - 基于点云和位姿生成主场景；RTSP 项目也可关联车端 onboard 地图
- `sfm`
  - 基于图像和 COLMAP 重建场景，该功能处于初步开发阶段

为加快前端加载，`GET /api/projects/{id}/scene` 与车端地图接口会对大体积 `scene.json` 做传输压缩（`scene_transport`）：合并重复点云层，优先保留 `render_points`，并在地图旁缓存 `scene.web.json`。

地图与视频联动：

1. 录制 / 导入时写入 `video_start_ts` / `video_end_ts`
2. 若轨迹时间戳与视频时钟已同原点（RTSP timeline 共享），直接绝对时间同步
3. 否则线性重映射轨迹到视频时钟（后端 `align_scene_timestamps_to_video`，前端 `sceneTime.ts`）
4. 视口点击轨迹点 → 视频按 `(ts - video_start_ts) / 1000` 跳转；视频播放 → 高亮对应位姿

项目路径（`scene_path`、视频路径等）写入数据库时使用相对路径，避免 Windows 绝对路径在 Docker/Linux 下失效。
## 5. 文档索引

- 架构说明：`ARCHITECTURE.md`
- 配置说明：`CONFIGURATION.md`

## 6. 问题说明

- 主项目容器默认包含 `ffmpeg`，但不包含 `COLMAP` 与 YOLO 运行时
- YOLO 服务需单独启动，且权重文件 `security_check_540.pt` 需自行放置到 `backend/models/YOLO/`
- `provider` / `provider_yolo` 主链路当前按 DashScope 兼容接口设计
- Docker 部署时，后端通过 `host.docker.internal:8001` 访问宿主机 YOLO 服务
- Compose 开发模式下前端为 Vite HMR；若需要 Nginx 生产静态资源，请改用 `web/Dockerfile`
- 导入的 rosbag 数据需符合当前自定义消息结构
- 车端地图需自行放到 `.runtime/robots/<vehicle_id>/maps/scene.json`；缺失时三维预览不可用，但不影响录制与分析
## 7. 配置说明

推荐在项目根目录创建 `.env` 作为主配置文件：

```powershell
Copy-Item .env.example .env
```

本地仅调试后端时，也可使用 `backend/.env`（见 `backend/.env.example`，含 RTSP 看门狗等后端专属项）。

**大模型（Provider / provider_yolo 必需）：**

```env
VISION_PROVIDER=dashscope
VISION_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_API_KEY=你的真实APIKey
VISION_MODEL=qwen3.5-plus
```

**YOLO 服务：**

```env
# 本地开发：后端与 YOLO 同机
YOLO_API_URL=http://127.0.0.1:8001

# Docker 后端访问宿主机 YOLO
# YOLO_API_URL=http://host.docker.internal:8001

YOLO_WEIGHTS_PATH=backend/models/YOLO/security_check_540.pt
YOLO_SERVICE_PORT=8001
```

**RTSP 看门狗（可选，默认开启）：**

```env
RTSP_VEHICLES_PATH=backend/config/rtsp_vehicles.yaml
RTSP_WATCH_ENABLED=true
RTSP_WATCH_AUTO_ANALYSIS_MODE=provider_yolo
RTSP_WATCH_TEST_MODE=true
```

更详细的配置方式、加载顺序和敏感信息管理说明见：

- `CONFIGURATION.md`

说明：

- 真实 API Key 推荐只写在部署机器本地的 `.env` 中。
- `.env.example` 只是模板，不包含真实密钥。
- 只要不手工把 `.env` 打进交付包，当前代码仓库默认不会携带真实 API Key。

## 8. 部署与交付

### 8.1 部署模式

推荐三种模式：

- **开发模式**
  - 本地 Python 后端（8010）
  - 本地 YOLO 服务（8001）
  - 本地 Vite 前端（5173）
  - 本地 `.runtime/`

- **Docker 模式**
  - `backend` 容器（8010）
  - `web` 容器（8700，默认 Vite 开发服；生产可换 Nginx 镜像）
  - 宿主机 YOLO 服务（8001）
  - 宿主机挂载：`.runtime/`、`inputs/`
- **交付模式**
  - 同上 Docker 模式，配合预置 `inputs/` 与配置文件

### 8.2 环境要求

- 操作系统：
  - Windows 10/11
  - Linux
- Docker 24+（容器部署）
- Docker Compose v2（容器部署）
- Python 3.11+（本地开发）
- Node.js 18+（前端开发）

容器镜像当前内置：

- Python 3.11
- FFmpeg

YOLO 服务额外需要（宿主机或独立环境）：

- Python 3.11+
- CUDA（可选，用于 GPU 加速）
- 权重文件 `security_check_540.pt`

如需启用 SFM 场景重建，还需要宿主机额外提供：

- COLMAP

如需真实分析，需要准备：

- DashScope 兼容多模态接口（`provider` / `provider_yolo`）
- 自托管 YOLO 服务（`provider_yolo`）

### 8.3 Docker 部署

启动：

```powershell
Copy-Item .env.example .env
.\docker-run.ps1
# 或：docker compose up --build -d
```

停止：

```powershell
.\docker-run.ps1 -Action down
# 或：docker compose down
```

查看日志：

```powershell
.\docker-run.ps1 -Action logs
# 或：docker compose logs -f backend web
```

访问地址：

- 前端：`http://127.0.0.1:8700`
- 后端 API：`http://127.0.0.1:8010`
- 健康检查：`http://127.0.0.1:8010/healthz`

### 8.4 输入数据准备

推荐目录结构：

```text
inspection_vue_platform/
├─ inputs/
│  ├─ bags/
│  │  └─ your_bag/
│  │     ├─ metadata.yaml
│  │     └─ *.db3
│  └─ standards/
│     └─ your_rule_docs/
│        ├─ *.docx
│        └─ *.xlsx
├─ backend/models/YOLO/
│  └─ security_check_540.pt    # YOLO 权重（需自行提供）
└─ backend/config/
   ├─ rtsp_vehicles.yaml       # RTSP 车辆列表
   └─ object_aliases.yaml      # 检测类别别名
```

要求：

- rosbag 目录至少包含：
  - `metadata.yaml`
  - 一个或多个 `.db3`
- 标准目录至少包含一种：
  - `.docx`
  - `.xlsx`

### 8.5 运行时数据说明

SQLite 数据库位于：

```text
.runtime/inspection.db
```

每个项目的运行时产物位于：

```text
.runtime/projects/<project_id>/
```

关键产物包括：

- `artifacts/inspection.mp4`
- `summaries/rules.json`
- `summaries/dataset_summary.json`
- `scenes/scene.json`
- `summaries/analysis_summary.json`

其他运行时目录：

- `.runtime/rtsp_recordings/` — RTSP 看门狗录制文件（同名 `*.meta.json` 记录 `video_start_ts` 来源）
- `.runtime/robots/<vehicle_id>/`
  - `recordings/` — 按车归档的录制
  - `maps/scene.json` — 车端点云地图（可选旁路缓存 `scene.web.json`）
- `.runtime/YOLO_log/` — YOLO 检测日志
### 8.6 常见问题

Docker 启动成功但导入失败时，优先检查：

- `inputs/` 是否已挂载并包含实际数据
- rosbag 路径是否正确
- standards 路径是否正确
- `config/security_check.yaml` 是否存在

分析接口返回 400 时，常见原因：

- `VISION_API_KEY` 未配置
- 模型名不在支持列表
- 视频切片超时
- 规则为空

`provider_yolo` 分析失败时，常见原因：

- YOLO 服务未启动（`http://127.0.0.1:8001/healthz` 不可达）
- `backend/models/YOLO/security_check_540.pt` 缺失
- Docker 环境下 `YOLO_API_URL` 未指向 `host.docker.internal:8001`
- `YOLO_FAIL_OPEN=false` 时 YOLO 报错会直接终止分析

RTSP 相关问题时，优先检查：

- `backend/config/rtsp_vehicles.yaml` 中 URL 是否可达
- `ffmpeg` 是否可用（录制依赖）
- 看门狗是否开启（`RTSP_WATCH_ENABLED`）
- 测试模式下录制时长/数量是否触达上限
- 时间轴不同步时：测试流是否发布了 `/time` 条码，或录制旁是否存在 `*.meta.json`
- 车端地图 404：`.runtime/robots/<id>/maps/scene.json` 是否存在，以及点云开关是否开启

三维场景加载缓慢或点云为空时，优先检查：

- 原始 `scene.json` 是否含多份重复点云层（传输压缩会合并为 `render_points`）
- `scene.web.json` 缓存是否过期（修改源地图后应自动按 mtime 失效）
- 数据库中 `scene_path` 是否仍为另一 OS 的绝对路径（当前会写回相对路径）
SFM 重建失败时，常见原因：

- `COLMAP_BIN` 未配置
- 宿主机没有安装 COLMAP
- 图像数量不足
- 对齐内点数不足

视频证据帧访问失败时，优先检查：

- 是否已经执行过分析
- `inspection.mp4` 是否存在
- `ffmpeg` 是否可用

### 8.7 交付建议

建议交付时至少包含：

- 整个仓库代码
- `.env.example`、`backend/.env.example`
- `docker-compose.yml`、`docker-run.ps1`、`docker-run.sh`
- `config/security_check.yaml`
- `backend/config/rtsp_vehicles.yaml`、`backend/config/object_aliases.yaml`
- `backend/rosenv/`
- `backend/models/YOLO/checklist_YOLO.txt`（类别说明）

不建议直接打包进交付件的内容：

- 真实 API Key
- 大体积输入数据
- YOLO 权重文件（体积大，单独分发）
- `.runtime/`
- 临时输出目录
- `.env`、`.env.local`、`backend/.env`、`backend/.env.local`

上线前建议检查：

1. `docker compose up --build` 能成功
2. `http://127.0.0.1:8010/healthz` 返回正常
3. `http://127.0.0.1:8700` 可访问
4. YOLO 服务 `http://127.0.0.1:8001/healthz` 返回正常
5. 能成功导入一个 rosbag 或 RTSP 示例项目
6. `demo` 分析可跑通
7. 若配置 provider，真实分析可跑通
8. 若配置 provider_yolo，YOLO + 大模型联合分析可跑通
9. 若启用 RTSP 看门狗，流上线后能自动录制，且 `*.meta.json` 中有合理的 `video_start_ts`
10. 选中配置了 `maps/scene.json` 的车辆后，三维视口能预览车端地图，并与视频时间联动
11. 若启用 SFM，COLMAP 可用