# Windows 10 x64 源码恢复说明

适用于重装全新 Windows 10 x64 后，从 GitHub 正式工程 + 用户私人恢复备份，恢复到可继续研发的源码模式。本文不含私人路径、账号或 API 参数。

## 1. 安装必要环境

- Git for Windows x64。
- Python **3.11.9 x64**；安装时勾选 “Add Python to PATH”。
- Node **22.23.2**（需要 JS 音源时）。
- 不自动安装来源不明的软件，不改全局安全策略。

## 2. 取得正式工程

```powershell
git clone --branch a0-cleanup https://github.com/765756876fds/Starlight.git <repo>
cd <repo>
git log -1 --format="%H %s"
```

以产品负责人确认的最终提交为准；不要用旧安装目录作为回滚基线。

## 3. Python 虚拟环境与依赖

```powershell
py -3.11 -c "import sys; print(sys.version)"
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

完整锁包含测试/打包工具；如果只要最小运行环境，可先安装 `requirements.txt`，再按功能安装可选依赖。详见 `docs/DEPENDENCIES.md`。

## 4. Node 桥接

```powershell
node -v
npm --prefix app/music/online/js_bridge ci
```

`package-lock.json` 已在仓库。若 `npm ci` 因 lock 与 `package.json` 不一致失败，再在干净目录执行 `npm install`；不要复制旧 `node_modules`。

## 5. 模型与运行组件

从私人备份 `<私人备份根>/04_必要模型与运行组件/` 复制：

- `models/`：两份 Qwen3-ASR GGUF（主模型 + mmproj）。
- `third_party/mpv/`、`third_party/node/`、`third_party/ffmpeg/`、`third_party/llama/`（保留 cpu 与 vulkan 两套后端及 DLL）。
- `node_bridge/package-lock.json` 与 `package.json` 仅用于核对。

MPV、FFmpeg、llama、Node、模型不进入 Git；干净 Windows 没有 LM Studio 时使用备份中的 `third_party/llama`，不要依赖安装包的 `llama-server.exe` 复制步骤。

## 6. 配置、数据库、音源

私人备份中两个保险箱分别恢复：

1. `<私人备份根>/03_API与私人凭据/凭据保险箱.vault`：完整 `settings.yaml`、`.qwen_key`、旧 `软件API`（仅备查）。
2. `<私人备份根>/01_个人配置与数据库/个人数据保险箱.vault`：数据库、播放历史、续播位置、个性化配置、旧安装库。

用备份中的 `解锁凭据保险箱.ps1`：

```powershell
.\解锁凭据保险箱.ps1 -VaultFile <vault> -OutputDir <临时目录> -PasswordFile <口令文件>
```

不要把解出的临时目录留在备份或网盘目录；恢复后删除。

恢复位置：

- `personal_data/config/*` → `<repo>/config/`
- 完整 `settings.yaml` → `<repo>/config/settings.yaml`
- `personal_data/database/*` → `<repo>/data/`
- `personal_data/state/movie_watch_later/*` → `<repo>/data/movie/watch_later/`
- `02_音乐源与影视源/music_sources/**` → `<repo>/data/music_sources/`
- `music_sources.json` → `<repo>/config/`

影视源以完整 `settings.yaml` 为准；`movie_sources.json` 只作对照。

## 7. 绝对路径与身份适配

至少核对：

- `app.data_dir` → `<repo>/data`
- `app.log_dir` → `<repo>/logs`
- `music.database` → `<repo>/data/music.db`
- `player.database` → `<repo>/data/player.db`
- `tts.output_dir` → `<repo>/data/tts`
- `music.scan_dirs` / `music.download_dir` → 新机音乐库
- `mpv_path` 可保持 `mpv`，或指向 `third_party/mpv/mpv.exe`

不要保留旧机器的用户目录、盘符或旧安装版数据目录；源码版与安装版数据库不要同时写。

## 8. 权限与登录

- 麦克风：Windows 设置 → 隐私 → 麦克风，允许桌面应用。
- 系统声音：WASAPI loopback，无需单独授权，但声卡/驱动要正常。
- 游戏助手：需要 LoL 客户端；不要为了权限关闭 Defender/防火墙或改全局安全策略。
- 第三方音源/影视源：可能需要重新登录或授权；本备份不携带明文 Cookie/Token；影视 token、Qwen key 从凭据保险箱恢复后按需重新授权。

## 9. 启动与最小检查

```powershell
.\.venv\Scripts\python.exe main.py
```

最小检查：

1. 设置/诊断显示版本、源码运行方式、MPV/Node/FFmpeg 路径。
2. 音乐：扫描一首本地歌、播放/暂停/续播；歌单/收藏/历史计数不为空。
3. 影视：搜索一部片、起播、暂停、续播位置；换源不崩。
4. 语音：麦克风录音、ASR 返回、TTS 播放；系统声音采集可用。
5. 游戏助手：LoL 客户端存在时能读状态；不要求全量回归。
6. 数据库：对 `data/*.db` 执行 `PRAGMA integrity_check`；确认 `music.db` 的 `music/playlists/play_history` 有数据。

## 10. 恢复脚本状态

私人备份中的 `bootstrap_windows.ps1` 与 `fix_paths.py` 是**候选**，未直接纳入正式仓库：它们仍依赖备份目录结构、需要两个口令文件，且会覆盖新工程配置/数据库。重装时优先按本文人工恢复；确认脚本行为后再决定是否收进仓库。
