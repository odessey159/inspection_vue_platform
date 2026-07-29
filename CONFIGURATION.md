# Configuration Guide

## 1. 配置入口

后端配置定义在：

- `backend/app/settings.py`

项目当前采用：

- 环境变量
- `.env` 文件

两种方式共同管理配置。

## 2. 支持的配置文件位置

### 2.1 根目录配置

位于项目根目录：

- `.env`
- `.env.local`
- `.env.example`

用途：

- 推荐作为项目的主配置入口
- 推荐用于本地开发
- 推荐用于 Docker / Compose 部署

### 2.2 backend 目录配置

位于 `backend/` 下：

- `backend/.env`
- `backend/.env.local`

用途：

- 仅适合后端单独调试
- 不建议作为正式交付的主配置

### 2.3 前端配置

位于 `web/` 下：

- `web/.env.example`

当前前端配置项较少，主要依赖同域访问和 Nginx 反代。

## 3. 配置读取顺序

后端启动时，会先读取进程环境变量，再按文件顺序补充未设置的配置项。

### 3.1 最高优先级

进程环境变量。

例如：

- PowerShell 手动设置的环境变量
- Docker Compose `environment:` 注入的变量
- 宿主机系统环境变量

这些值一旦存在，`.env` 文件里的同名值不会覆盖它们。

### 3.2 文件加载顺序

当某个配置项尚未出现在进程环境中时，后端会按以下顺序读取：

1. 根目录 `.env`
2. 根目录 `.env.local`
3. `backend/.env`
4. `backend/.env.local`

这意味着：

- 根目录 `.env` 是推荐主配置
- `backend/.env.local` 更适合作为后端本地兜底配置

## 4. 推荐配置策略

### 4.1 本地开发

推荐在项目根目录创建：

- `.env`

### 4.2 Docker 部署

同样推荐使用根目录 `.env`。

原因：

1. `docker compose` 默认会读取根目录 `.env`
2. 应用本身也优先读取根目录 `.env`
3. 文档、交付和部署方式一致

## 4.4 输入目录与扫描行为

默认情况下，系统会同时扫描：

- 项目根目录
- `inputs/`

对应配置项是：

- `DISCOVERY_ROOTS`

默认模板里一般会写成类似：

```env
DISCOVERY_ROOTS=./inputs,./
```

说明：

- 文档中的 `./` 表示项目根目录
- 这里使用相对路径只是为了说明目录关系，便于交付
- 如果目录结构保持仓库默认布局，通常不需要手工改这些目录类配置

这意味着：

- 如果 rosbag 当前就在项目根目录下，系统也可以发现它
- 并不是必须先移动到 `inputs/` 才能运行

但仍建议在交付和容器部署时采用更规整的方式：

- rosbag 放到 `inputs/bags/`
- 标准文档放到 `inputs/standards/`

## 5. API Key 配置

### 5.1 相关配置项

当前大模型 provider 的核心配置项是：

- `VISION_PROVIDER`
- `VISION_API_URL`
- `VISION_API_KEY`
- `VISION_MODEL`

其中真正存放密钥的是：

- `VISION_API_KEY`

### 5.2 推荐写法

在项目根目录 `.env` 中填写：

```env
VISION_PROVIDER=dashscope
VISION_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_API_KEY=你的真实APIKey
VISION_MODEL=qwen3.5-plus
```

### 5.3 本地运行配置示例

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

```env
VISION_API_KEY=你的真实APIKey
```

再启动后端：

```powershell
Set-Location backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload
```

### 5.4 Docker 部署配置示例

根目录 `.env` 中写：

```env
VISION_PROVIDER=dashscope
VISION_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_API_KEY=你的真实APIKey
VISION_MODEL=qwen3.5-plus
```

然后执行：

```powershell
docker compose up --build -d
```

由于 `docker-compose.yml` 已经把这些变量映射进后端容器，因此不需要再进容器单独设置。

### 5.5 如果不配置 API Key

如果只使用：

- `demo` 模式

可以不配置 `VISION_API_KEY`。

如果使用：

- `provider` 模式
- `provider_yolo` 模式

但没有配置 `VISION_API_KEY`，分析会失败。

`provider_yolo` 还额外需要可达的 YOLO 服务（`YOLO_API_URL`）。
## 6. `.env.example` 的作用

`.env.example` 是模板文件，不应该放真实密钥。

它的作用是：

1. 展示有哪些配置项
2. 提供默认值
3. 作为复制 `.env` 的起点

推荐操作：

```powershell
Copy-Item .env.example .env
```

## 7. 常用配置项说明

### 7.1 目录相关

- `APP_HOME`
  - 应用根目录

- `RUNTIME_DIR`
  - SQLite 与运行时目录

- `PROJECTS_DIR`
  - 项目产物目录

- `DATABASE_PATH`
  - SQLite 数据库路径

- `INPUTS_DIR`
  - 输入数据目录

- `CONFIG_DIR`
  - 静态配置目录

- `ROSENV_DIR`
  - rosbag 提取脚本与消息定义目录

- `DEFAULT_STANDARDS_DIR`
  - 默认标准目录

- `SECURITY_CHECK_CALIBRATION_PATH`
  - 相机校准文件路径

- `DISCOVERY_ROOTS`
  - 自动扫描目录，多个路径用逗号分隔

### 7.2 模型相关

- `VISION_PROVIDER`
- `VISION_API_URL`
- `VISION_API_KEY`
- `VISION_MODEL`
- `VISION_CLIP_SECONDS`
- `VISION_VIDEO_FPS`
- `VISION_MAX_CLIP_BYTES`
- `VISION_REQUEST_TIMEOUT_SECONDS`

### 7.3 YOLO 相关

- `YOLO_API_URL`
  - 主后端访问 YOLO 服务的地址；本地默认 `http://127.0.0.1:8001`，Docker 后端常用 `http://host.docker.internal:8001`
- `YOLO_DETECT_PATH` / `YOLO_RTSP_DETECT_PATH`
  - 视频与 RTSP 检测端点路径
- `YOLO_RTSP_SEGMENT_SECONDS` / `YOLO_RTSP_TRANSPORT`
  - 直播分段时长与传输协议
- `YOLO_CONFIDENCE_THRESHOLD` / `YOLO_FAIL_OPEN`
  - 置信度阈值；失败时是否放行继续分析
- `YOLO_WEIGHTS_PATH` / `YOLO_SERVICE_PORT` / `YOLO_LOG_DIR`
  - 仅 YOLO 服务进程使用

### 7.4 RTSP 相关

- `RTSP_VEHICLES_PATH`
  - 巡检小车列表 YAML
- `RTSP_WATCH_ENABLED` / `RTSP_WATCH_POLL_INTERVAL_SECONDS`
  - 看门狗开关与轮询间隔
- `RTSP_WATCH_TEST_MODE` / `RTSP_WATCH_TEST_MAX_SECONDS` / `RTSP_WATCH_TEST_MAX_RECORDINGS`
  - 测试模式录制上限
- `RTSP_WATCH_AUTO_ANALYSIS_MODE`
  - 流断开或稳定后自动分析模式（如 `provider_yolo`）；空字符串表示关闭
- `RTSP_YOLO_MONITOR_LLM_ENABLED` / `RTSP_YOLO_MONITOR_LLM_ON_EMPTY` / `RTSP_YOLO_MONITOR_CAPTURE_CLIP`
  - 直播 YOLO 监控是否联动大模型复核、空检测是否仍送审、是否截取 clip
- `RTSP_RECORD_VIDEO_CODEC` / `RTSP_FFMPEG_RW_TIMEOUT_US` / `RTSP_PUBLISH_PROBE_TIMEOUT_SECONDS`
  - 录制编码与超时
- `RTSP_TRANSPORT` / `RTSP_TIMELINE_PROBE_TIMEOUT_SEC`
  - 时间轴采样时的传输协议与探测超时（`rtsp_timeline`）

### 7.5 Rule RAG 相关

- `RULE_RAG_ENABLED`
  - 默认关闭；开启后按 YOLO 类别检索规则片段
- `RULE_RAG_FALLBACK_TOP_K`
  - 检索失败时的兜底条数

### 7.6 场景相关

- `SCENE_VOXEL_SIZE`
- `SCENE_MAX_POINTS`
- `SCENE_RENDER_MAX_POINTS`
- `SCENE_EGO_FILTER_ENABLED`
- `POINT_CLOUD_ENABLED`
  - 是否允许加载 / 展示车端点云地图

这些参数主要影响：

- 点云下采样
- 屋顶裁剪
- 地面裁剪
- 自车过滤
- 三维渲染点数
- 车端地图预览开关

传输层压缩（`scene_transport`）不依赖额外环境变量：API 返回时自动合并重复点云层，并在地图旁缓存 `scene.web.json`。
## 8. 最小可用示例

### 8.1 仅运行 demo 模式

如果保持仓库默认目录结构，目录类配置通常可以不写，直接沿用程序默认值。

如需在文档中按“相对项目根目录”的方式表达，可参考下面的示意写法：

```env
APP_HOME=.
RUNTIME_DIR=./.runtime
PROJECTS_DIR=./.runtime/projects
DATABASE_PATH=./.runtime/inspection.db
INPUTS_DIR=./inputs
CONFIG_DIR=./config
ROSENV_DIR=./backend/rosenv
DEFAULT_STANDARDS_DIR=./inputs/standards
SECURITY_CHECK_CALIBRATION_PATH=./config/security_check.yaml
DISCOVERY_ROOTS=./inputs,./
FFMPEG_BIN=ffmpeg
VISION_PROVIDER=dashscope
VISION_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_API_KEY=
VISION_MODEL=qwen3.5-plus
```

更稳妥的交付方式仍然是：

1. 复制 `.env.example` 为 `.env`
2. 只改业务相关配置，例如 `VISION_API_KEY`、`VISION_MODEL`
3. 目录结构不变时，尽量不要手工改目录类配置

### 8.2 运行大模型分析模式

只需在上面的基础上补上：

```env
VISION_API_KEY=你的真实APIKey
```

### 8.3 运行 provider_yolo / RTSP 巡检

在 8.2 基础上补充：

```env
YOLO_API_URL=http://127.0.0.1:8001
RTSP_WATCH_ENABLED=true
RTSP_WATCH_AUTO_ANALYSIS_MODE=provider_yolo
# 可选：开启 Rule RAG
RULE_RAG_ENABLED=false
```

并将车端地图（如有）放到：

```text
.runtime/robots/<vehicle_id>/maps/scene.json
```
