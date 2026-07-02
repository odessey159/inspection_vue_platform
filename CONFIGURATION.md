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

但没有配置 `VISION_API_KEY`，分析会失败。

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

### 7.3 场景相关

- `SCENE_VOXEL_SIZE`
- `SCENE_MAX_POINTS`
- `SCENE_RENDER_MAX_POINTS`
- `SCENE_EGO_FILTER_ENABLED`

这些参数主要影响：

- 点云下采样
- 屋顶裁剪
- 地面裁剪
- 自车过滤
- 三维渲染点数

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
