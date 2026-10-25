# Starlight 灾难恢复手册

本手册用于新电脑或重装系统后，从零恢复 Starlight 开发环境。

---

## 1. 安装基础工具

### Git
- 下载：https://git-scm.com/download/win
- 安装时默认选项即可

### Python 3.11
- 下载：https://www.python.org/downloads/release/python-3119/
- 安装时勾选 "Add Python to PATH"

---

## 2. 克隆仓库

```bash
git clone https://github.com/765756876fds/Starlight.git
cd Starlight
git checkout a0-cleanup
```

---

## 3. 建立虚拟环境

```bash
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
```

---

## 4. 安装依赖

```bash
pip install -r requirements.txt
```

注意：requirements.txt 中部分依赖（如 librosa, pyaudio, videofetch）可能需要系统级依赖。如果安装失败，先跑核心测试再逐步补装。

---

## 5. 运行单元测试

```bash
python -m unittest tests.unit.test_asr_bootstrap_r2 tests.unit.test_path_migration_r1 -v
```

预期：26 个测试全部通过。

如果测试通过 → **源码恢复成功**。

---

## 6. 运行时资源（当前全部缺失）

以下资源不在 GitHub 仓库中（.gitignore 已排除），需要手动准备：

| 资源 | 路径 | 状态 | 来源 |
|---|---|---|---|
| mpv.exe | third_party/mpv/mpv.exe | 缺失 | mpv 官方发布版（待确认版本） |
| llama-server (vulkan) | third_party/llama/vulkan/llama-server.exe | 缺失 | llama.cpp release（待确认版本） |
| llama-server (cpu) | third_party/llama/cpu/llama-server.exe | 缺失 | llama.cpp release（待确认版本） |
| Qwen3-ASR GGUF | models/*.gguf | 缺失 | HuggingFace（待确认文件名） |

缺少这些资源时：
- 音乐/电影播放：不可用（mpv 缺失）
- ASR 语音识别：不可用（llama + 模型缺失）
- LoL 面板：部分功能可用（只依赖 HTTP API，不需要 llama）

---

## 7. 用户数据位置（重装时备份）

| 数据 | 路径 | 说明 |
|---|---|---|
| 配置 | %LOCALAPPDATA%\Starlight\config\ | settings.yaml |
| 音乐数据库 | %LOCALAPPDATA%\Starlight\data\music.db | SQLite |
| 播放历史 | %LOCALAPPDATA%\Starlight\data\player.db | SQLite |
| 日志 | %LOCALAPPDATA%\Starlight\logs\ | 可丢弃 |
| Qwen Key | %LOCALAPPDATA%\Starlight\tools\.qwen_key | 敏感，备份时注意 |
| 模型 | %LOCALAPPDATA%\Starlight\models\ | 大文件，可重新下载 |

重装系统前，把 %LOCALAPPDATA%\Starlight\ 整个备份。重装后恢复到相同位置即可。

---

## 8. 验证状态

| 功能 | 验证方式 | 当前状态 |
|---|---|---|
| 路径迁移逻辑 | 单元测试 test_path_migration_r1 | 已验证（mock） |
| SQLite backup 成功/失败/WAL | 单元测试 | 已验证（mock） |
| ASR 双后端回退 | 单元测试 test_asr_bootstrap_r2 | 已验证（mock） |
| 真机 ASR 播放 | 手动启动应用说话 | 未验证（资源缺失） |
| 真机 mpv 音乐播放 | 手动播放音乐 | 未验证（mpv 缺失） |
| 真机 LoL 面板 | 启动游戏进对局 | 未验证 |
| SSL 真实请求 | 启动应用加载封面 | 未验证 |
| SQLite 真实迁移 | 旧版升级 | 未验证 |

---

## 9. 备份检查点

每次改代码前，先建 Git 检查点：
```bash
git add <具体文件>
git commit -m "描述改动"
```

不要用 `git add .` 或 `git add -A`。

---

## 10. 区分三种恢复

| 恢复级别 | 内容 | 标准 |
|---|---|---|
| 源码恢复 | Git 克隆 + pip install + 测试通过 | 单元测试 26/26 OK |
| 开发环境恢复 | 源码 + mpv + llama + 模型 | 能启动源码版 |
| 完整应用恢复 | 开发环境 + 用户数据 + 配置 | 与旧电脑功能一致 |

当前仓库只包含源码。用户数据和运行时资源需要单独备份。
