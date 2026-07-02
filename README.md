# Inspection Vue Platform

工业巡检隐患识别与三维工作台系统。

本项目提供一套从 ROS 2 rosbag 导入、标准规则解析、巡检视频生成、隐患识别、证据帧缓存，到三维场景联动展示的完整工作台。前端为 Vue 3 + Vite + Three.js，后端为 FastAPI + SQLModel + SQLite。

## 1. 项目概览

### 1.1 目标

系统用于支撑以下完整业务链路：

1. 导入 ROS 2 rosbag 数据目录
2. 解析相机、激光雷达、位姿等主题数据
3. 解析行业标准文档并生成结构化隐患规则
4. 生成巡检视频与三维场景
5. 通过演示模式或大模型模式生成隐患识别结果
6. 在视频、证据帧、规则详情和三维场景之间建立联动

### 1.2 当前交付范围

当前仓库已经包含以下能力：

- 项目导入与运行时项目管理
- rosbag 元数据识别和主题推断
- 相机图像 / 点云 / 位姿提取
- 巡检视频 `inspection.mp4` 生成
- 标准文档规则解析
- 基于规则的 `demo` 分析模式
- 基于 DashScope 兼容接口的 `provider` 分析模式
- 三维场景重建与展示
- 基于图像的 SFM 场景重建的初步实现
- 证据帧缓存与 findings 复核

### 1.3 交付包含内容

- 后端服务代码：`backend/`
- 前端工作台代码：`web/`
- 容器化交付文件：
  - `backend/Dockerfile`
  - `web/Dockerfile`
  - `web/nginx.conf`
  - `docker-compose.yml`
- 配置模板：
  - `.env.example`
  - `web/.env.example`
- 校准与 rosbag 工具内置资源：
  - `config/security_check.yaml`
  - `backend/rosenv/`

## 2. 仓库结构

```text
inspection_vue_platform/
├─ backend/                     # FastAPI 后端
│  ├─ app/
│  │  ├─ main.py                # FastAPI 入口
│  │  ├─ db.py                  # SQLite / SQLModel 初始化
│  │  ├─ models.py              # 数据模型
│  │  ├─ schemas.py             # API schema
│  │  ├─ routers/               # API 路由
│  │  └─ services/              # 核心业务服务
│  ├─ rosenv/                   # 内置 rosbag 提取脚本与消息定义
│  ├─ tests/                    # 后端测试
│  ├─ requirements.txt
│  └─ Dockerfile
├─ web/                         # Vue 3 前端
│  ├─ src/
│  ├─ package.json
│  ├─ nginx.conf
│  └─ Dockerfile
├─ config/                      # 校准等静态配置
├─ inputs/                      # 推荐挂载的输入目录（rosbag / standards）
├─ .runtime/                    # 运行时数据库和项目产物
├─ docker-compose.yml
├─ ARCHITECTURE.md
└─ CONFIGURATION.md
```

## 3. 快速开始

### 3.1 本地开发运行

后端：

```powershell
Set-Location backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload
```

前端：

```powershell
Set-Location web
npm install
npm run dev
```

访问地址：

- 前端开发地址：`http://127.0.0.1:5173`
- 后端 API：`http://127.0.0.1:8010`
- 健康检查：`http://127.0.0.1:8010/healthz`

### 3.2 Docker 一键启动

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

启动后访问：

- Web：`http://127.0.0.1:8080`
- API：`http://127.0.0.1:8010`

### 3.3 首次导入前准备

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

## 4. 核心能力说明

### 4.1 项目导入

导入接口：`POST /api/projects/import`

导入时后端会执行：

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

系统支持两种分析模式：

- `demo`
  - 不调用真实大模型
  - 依据前几条可视规则生成演示 findings
  - 用于联调前端和工作台流程

- `provider`
  - 调用大模型进行真实分析
  - 当前主项目内置路径为 DashScope 兼容接口
  - 识别结果写入 findings，并缓存证据帧

### 4.3 三维场景

系统支持两种场景来源：

- `lidar`
  - 基于点云和位姿生成主场景
- `sfm`
  - 基于图像和 COLMAP 重建场景，该功能处于初步开发阶段

## 5. 文档索引

- 架构说明：`ARCHITECTURE.md`
- 配置说明：`CONFIGURATION.md`

## 6. 问题说明

- 主项目容器默认包含 `ffmpeg`，但不包含 `COLMAP`
- `provider` 主链路当前按 DashScope 兼容接口设计
- 导入的 rosbag 数据需符合当前自定义消息结构

## 7. 配置说明

推荐在项目根目录创建 `.env` 作为主配置文件：

```powershell
Copy-Item .env.example .env
```

如果需要启用真实大模型分析，至少配置：

```env
VISION_PROVIDER=dashscope
VISION_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_API_KEY=你的真实APIKey
VISION_MODEL=qwen3.5-plus
```

更详细的配置方式、加载顺序和敏感信息管理说明见：

- `CONFIGURATION.md`

说明：

- 真实 API Key 推荐只写在部署机器本地的 `.env` 中。
- `.env.example` 只是模板，不包含真实密钥。
- 只要不手工把 `.env` 打进交付包，当前代码仓库默认不会携带真实 API Key。

## 8. 部署与交付

### 8.1 部署模式

推荐两种模式：

- 开发模式
  - 本地 Python 后端
  - 本地 Vite 前端
  - 本地 `.runtime/`

- 交付模式
  - `backend` 容器
  - `web` 容器
  - 宿主机挂载：
    - `.runtime/`
    - `inputs/`

### 8.2 环境要求

- 操作系统：
  - Windows 10/11
  - Linux
- Docker 24+
- Docker Compose v2

容器镜像当前内置：

- Python 3.11
- FFmpeg

如需启用 SFM 场景重建，还需要宿主机额外提供：

- COLMAP

如需真实分析，需要准备至少一种模型服务：

- DashScope 兼容多模态接口
- 独立测试样例中的 Ollama 本地视觉模型

### 8.3 Docker 部署

启动：

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

停止：

```powershell
docker compose down
```

查看日志：

```powershell
docker compose logs -f backend
docker compose logs -f web
```

访问地址：

- 前端：`http://127.0.0.1:8080`
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
- `.env.example`
- `docker-compose.yml`
- `config/security_check.yaml`
- `backend/rosenv/`

不建议直接打包进交付件的内容：

- 真实 API Key
- 大体积输入数据
- `.runtime/`
- 临时输出目录
- `.env`
- `.env.local`
- `backend/.env`
- `backend/.env.local`

上线前建议检查：

1. `docker compose up --build` 能成功
2. `http://127.0.0.1:8010/healthz` 返回正常
3. `http://127.0.0.1:8080` 可访问
4. 能成功导入一个示例项目
5. `demo` 分析可跑通
6. 若配置 provider，真实分析可跑通
7. 若启用 SFM，COLMAP 可用
