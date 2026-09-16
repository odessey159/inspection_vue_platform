# AGENTS.md

本文件供 Cursor / 编码助手阅读。只依据仓库内实际文件操作；不要猜测客户路径、密钥或现场设备状态。

## 项目是什么

工业巡检隐患识别与三维工作台（Inspection Vue Platform）。

- 前端：`web/`（Vue 3 + Vite + Three.js）
- 后端：`backend/`（FastAPI + SQLModel + SQLite）
- YOLO 推理服务：`backend/yolo_service/`（默认 `127.0.0.1:8001`）

更完整的业务说明见 `README.md`、`ARCHITECTURE.md`、`CONFIGURATION.md`。

## 路径与启动

仓库根目录即项目目录。Windows 本机优先用控制台，而不是直接改业务代码里的启动参数：

- `start_console.bat`
- `start_console.ps1`
- `start_console.py`（仅标准库 + Tk；启动前即可打开）

环境检查（不拉起服务、不连设备）：

```powershell
python start_console.py --check
# 或
.\start_console.ps1 -Check
```

分服务手动启动、Docker 启动、配置复制方式见 `README.md` 第 3 节和第 7 节。

**验证时不要**启动真实巡检、操作现场设备、清空数据或改写现有数据库。`backend/app/main.py` 的进程生命周期会启动 RTSP 看门狗。

## Python 与依赖

- 运行版本：Python 3.11+。后端 Docker 镜像为 `python:3.11-slim`。
- 依赖管理：pip + 清单文件，虚拟环境放在 `backend/.venv`（Windows 解释器为 `backend\.venv\Scripts\python.exe`）。
- 后端：`pip install -r backend/requirements.txt`
- YOLO 服务额外依赖：`pip install -r backend/requirements-yolo.txt`
- 前端：`web/package.json` 与锁定文件 `web/package-lock.json`（`npm ci` 或 `npm install`）
- **不要**用当前环境的 `pip freeze` 覆盖上述清单。

## 配置如何从示例生成

1. 复制根目录 `.env.example` 为 `.env`（Docker 与主配置）。
2. 仅本机调试后端时，可复制 `backend/.env.example` 为 `backend/.env`。
3. 前端示例为 `web/.env.example`。
4. 把真实 `VISION_API_KEY`、客户 RTSP、数据库路径只写进本地 `.env` / `.runtime/`，不要提交。

车辆列表示例在 `backend/config/rtsp_vehicles.yaml`。界面改写会合并到 `.runtime/rtsp_vehicles.override.yaml`。

## Docker 文件位置与启动

本项目使用 Docker，配置已在仓库中：

| 文件 | 作用 |
| --- | --- |
| `backend/Dockerfile` | 后端镜像 |
| `web/Dockerfile` | 前端 Nginx 生产镜像 |
| `web/Dockerfile.dev` | Compose 开发用 Vite |
| `docker-compose.yml` | `backend` + `web` |
| `docker-run.ps1` / `docker-run.sh` | 必要时从 `.env.example` 生成 `.env`，再 `docker compose up --build -d` |

YOLO 默认在**宿主机** `:8001` 运行；容器后端通过 `YOLO_API_URL=http://host.docker.internal:8001` 访问。

## Git 提交范围（助手约束）

应当提交：源码、静态资源、依赖清单与锁定文件、Dockerfile / 已脱敏 Compose、启动与测试脚本、`.env.example`、运行说明。

不要提交：虚拟环境、缓存、日志、临时文件、密码与私钥、客户真实配置、数据库、备份、采集数据、Docker 数据目录、镜像导出、YOLO 权重（`backend/models/**/*.pt`）。

`.gitignore` 不会取消已跟踪文件。不要 `git add .`，不要 `reset --hard` / `clean` / 强推，不要重写历史。
