# 依赖与恢复信息

## Python

- 当前实际环境：**CPython 3.11.9 x64**（Windows）。
- `requirements-lock.txt`：由当前项目 `.venv` 的 `pip freeze` 生成，**未为了生成清单升级环境**；它记录整个开发/运行环境的精确版本，包含测试、打包和可选工具。
- `requirements.txt`：正式运行依赖的声明式清单（含 PySide6、requests、aiohttp、pydantic、mutagen、音频分析、ASR 采集、TTS、MPV 控制、videofetch 等）。
- `requirements-dev.txt`：开发工具（当前只有 `ruff`）。
- `requirements-optional.txt`：可选在线识曲 `shazamio`，普通启动不需要。
- 测试依赖（如 `pytest`）和打包依赖（如 `PyInstaller`、`pyinstaller-hooks-contrib`）存在于完整锁文件，但**不属于应用运行时必需**；不要把它们打进最终用户运行包。

恢复顺序：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

如果需要最小运行环境，可先安装 `requirements.txt`，再按实际功能安装可选依赖；完整锁用于复现当前开发/测试环境。

## Node

- 当前 Node：**v22.23.2**。
- `app/music/online/js_bridge/package-lock.json` 已纳入仓库；依赖来自公开 `registry.npmmirror.com`，未发现 `_auth`、Token、Cookie、密码或本机绝对路径。
- 恢复：`npm --prefix app/music/online/js_bridge ci`。如果 lock 与 `package.json` 不一致导致失败，再在干净目录执行 `npm install`，不要复制旧 `node_modules`。

## 外部运行组件

- MPV、FFmpeg、llama、Node、Qwen3-ASR GGUF 不提交 Git；由用户私人恢复备份提供。
- 当前版本和文件哈希见私人备份的 `04_必要模型与运行组件/versions.json` 与 `components_manifest.json`。
- 干净 Windows 上没有 LM Studio 时，使用备份中的 `third_party/llama`，不要依赖安装包的 `llama-server.exe` 复制步骤。
