# Starlight（星芒）源码运行说明

目标环境：Windows + Python 3.11，直接 `python main.py` 运行（源码模式，不打包）。

## 一、自动初始化（无需手动操作）

首次启动时程序自动完成以下工作，可重复执行、幂等：

| 项目 | 行为 |
|---|---|
| 数据/日志目录 | 自动创建 `data/`、`logs/`（源码模式在仓库根目录；打包模式在 `%LOCALAPPDATA%\Starlight/`） |
| 默认配置 | 缺失 `config/settings.yaml` 时按内置默认值生成；已有配置**原样保留**，只补缺失键 |
| 音乐数据库 | 缺失时创建并初始化 FTS5 结构；已存在则复用，不重建、不覆盖 |
| 旧版本兼容 | 旧配置/数据库自动迁移（目录迁移、`music_dirs.json`/`ui_prefs.json`/`music_sources.json` 等），目标已存在即跳过、不删源文件 |
| 事件日志 | 自动按天轮转 |

## 二、必须由用户准备的资源（GitHub 仓库不含）

以下资源体积大、含第三方二进制或用户私有数据，**不提交到 GitHub**，需自行准备：

| 资源 | 位置 | 获取方式 |
|---|---|---|
| Python 依赖 | `.venv/` | `python -m venv .venv` 后 `.venv\Scripts\pip install -r requirements.txt` |
| **mpv** 播放引擎 | `third_party/mpv/mpv.exe` | 从 mpv 官方构建下载；或在 `config/settings.yaml` 的 `music.mpv_path` 填写已有 mpv 路径 |
| **Node.js**（在线音乐 JS 桥接） | `third_party/node/node.exe` | 从 nodejs.org 获取；缺失时在线音乐插件会提示，不自动下载 |
| **llama-server + ASR 模型**（语音识别，可选） | `third_party/llama/vulkan/`（GPU 加速，优先）、`third_party/llama/cpu/`（CPU 回退）+ `%LOCALAPPDATA%\Starlight\models\` | 后端按 vulkan→cpu 顺序探测；AMD/NVIDIA 等支持 Vulkan 的显卡推荐从 llama.cpp 官方 nightly 下载 `llama-bXXXX-bin-win-vulkan-x64.zip` 解压到 `third_party/llama/vulkan/`。模型缺失时程序提示并从 hf-mirror 自动下载（`asr.bootstrap_enabled` 默认开）；已有中文路径模型会自动回退到 ASCII 用户目录副本。`asr.llama_gpu_layers` 默认 99（全层 offload）。若本机已装 LM Studio 则自动复用其 llama-server |
| **在线音乐插件**（MusicFree/LX 的 .js） | `data/music_sources/musicfree/`、`data/music_sources/lx/` | 旧项目目录复制，或在应用的“音源管理”里导入 .js 文件（导入后自动刷新，无需重启） |
| **Qwen TTS 密钥**（可选） | 设置界面填写 | 界面保存到本地配置，不提交 |

> mpv / Node / llama 缺失时，程序**不会盲目下载**：相关功能打印明确提示并降级（音乐本地播放不可用、在线音乐不可用、ASR 未就绪），其余功能照常运行。

## 三、启动方式

1. 安装依赖：`.venv\Scripts\pip install -r requirements.txt`
2. 准备上述资源（至少 mpv，其余按需）
3. 运行：
   - 双击 `启动.bat`（自动优先使用 `.venv` 解释器、检查依赖与 ASR 端点、清理残留 mpv），或
   - 命令行：`.venv\Scripts\python main.py`

## 四、GitHub 仓库包含什么 / 不包含什么

**包含**：全部源码、测试、`config/settings.example.yaml`（脱敏模板）、LoL 静态数据（`data/lol_data/`）、构建脚本、CI 工作流、文档。

**不包含**（已被 `.gitignore` 排除）：数据库及备份（`data/*.db`、`music.db.bak_before_scan`）、模型（`*.gguf`、`models/`）、第三方运行时（`third_party/mpv|node|llama|ffmpeg/`）、用户配置（`config/settings.yaml`、`ui_prefs.json`、`music_dirs.json`、`music_sources.json`）、JS 插件（`data/music_sources/`）、音乐指纹库（`data/music/`）、虚拟环境、缓存、日志。

## 五、测试与 CI

- 本机：`.venv\Scripts\python -m pytest tests/unit -o addopts="" -q`（pytest.ini 的 addopts 引用了未安装的 xdist/cov 插件，故用 `-o addopts=""` 覆盖）
- CI：推送/PR 到 `a0-cleanup` 自动运行（Linux 纯逻辑 + Windows 全量单元），无需真实游戏、声卡、麦克风、GPU 或外部音乐 API。
