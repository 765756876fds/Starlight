# Quick Start

## 在线音乐音源配置

本项目的在线音乐支持两类 JS 音源：

**MusicFree 音源**（放在 `data/music_sources/musicfree/`）：
- 从网络下载 .js 音源文件放入此目录
- 例如 kg.js（酷狗）/ kw.js（酷我）/ wy.js（网易）/ tx.js（QQ）
- 启动时自动加载

**LX Music 音源**（放在 `data/music_sources/lx/`）：
- 从 LX Music 客户端复制音源文件到此目录
- 启动时自动加载（部分音源可能不兼容，见启动日志）

**依赖**：
- 需要 Node.js >= 18（已安装）
- 首次使用需要：
  ```
  cd app/music/online/js_bridge
  npm install
  ```

**注意**：
- `data/music_sources/` 已加入 .gitignore，不会提交到仓库
- 音源文件来自第三方，本项目不为其内容负责
- VIP 歌曲可能无法播放

### Windows CMD 中文显示
如果启动日志里中文显示为乱码（如鍒濆鍖?），在运行 main.py 前先执行：
``nchcp 65001
``n这会把 cmd 切到 UTF-8 代码页。或直接用 Windows Terminal（默认 UTF-8）。

