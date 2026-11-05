# -*- mode: python ; coding: utf-8 -*-
"""星芒（Starlight）打包 spec（onedir 绿色文件夹）。

布局说明（PyInstaller 6.x，contents dir = _internal）：
- 代码 + Python 库 → dist/Starlight/_internal/
- 以下资源初始落 _internal/ 对应路径，build.ps1 后处理移到 exe 目录
  （frozen 态 PROJECT_ROOT = exe 目录，代码按 PROJECT_ROOT/third_party|data 查找）：
    _internal/third_party/mpv/*   → dist/third_party/mpv/   （bundled_tool("mpv")）
    _internal/third_party/node/*  → dist/third_party/node/  （bundled_tool("node")）
    _internal/data/lol_data/*     → dist/data/lol_data/     （champion_catalog 等）
- 保持在 _internal（代码用 __file__ 相对定位，打包态自动命中）：
    _internal/app/music/online/js_bridge/node_modules（js_source._bridge_dir）
    _internal/app/third_party/fingerprint（humming._THIRD_PARTY，audiofp 源码）

说明：librosa 不排除——它是高光检测（跳高潮核心功能）的实际依赖
（highlight_detector 惰性 import），不属于"不需要的巨型库"；torch/torchaudio
项目未使用，排除。
"""
import os
import sys
import json
import tempfile
from PyInstaller.utils.hooks import collect_all

# spec 已移入 scripts/：PyInstaller 的 SPECPATH = scripts/ 目录，
# 所有相对路径必须回退一级到项目根（datas/binaries 源路径 + main.py + icon）
ROOT = os.path.dirname(os.path.abspath(SPECPATH))
sys.path.insert(0, ROOT)
from app.core.build_info import create_manifest
# Unique build-only file: no generated manifest overwrites source or user data.
manifest_dir = tempfile.mkdtemp(prefix='starlight-build-info-')
manifest_path = os.path.join(manifest_dir, 'build_info.json')
with open(manifest_path, 'w', encoding='utf-8') as stream:
    json.dump(create_manifest(ROOT), stream, ensure_ascii=False, indent=2)


def P(rel: str) -> str:
    """把相对项目根的路径绝对化（仅源路径；目标路径保持相对，进 _internal 布局不变）。"""
    return os.path.join(ROOT, rel)

# PySide6 全量收集（Qt 平台插件 / 翻译 / 动态库；官方 hook 之外的保险）
pyside_datas, pyside_binaries, pyside_hidden = collect_all('PySide6')
shazam_datas, shazam_binaries, shazam_hidden = collect_all('shazamio')
pydantic_datas, pydantic_binaries, pydantic_hidden = collect_all('pydantic')
pydantic_core_datas, pydantic_core_binaries, pydantic_core_hidden = collect_all('pydantic_core')

# 动态加载 / 间接依赖库：audiofp（third_party 源码）运行时 import scipy/soundfile/soxr，
# PyInstaller 静态分析看不到，必须显式 hiddenimports
hiddenimports = [
    'numpy',
    'scipy',
    'soundfile',
    'soxr',
    'pyaudio',      # 官方 hook 收集 _portaudio.pyd
    'mutagen',
    'psutil',
    'loguru',
    'yaml',
    'dateutil',
    'websocket',
    'requests',
    'edge_tts',
    'certifi',      # P0-A: overlay_assets.py 显式用 certifi.where() 做 SSL CA bundle
] + pyside_hidden + shazam_hidden + pydantic_hidden + pydantic_core_hidden

datas = [
    (manifest_path, '.'),
    (P('VERSION'), '.'),
    # UI 主题（dark.qss + icons/*.svg）：applier.py 用 __file__ 相对定位，
    # 打包态命中 _internal/app/ui/theme/ —— 缺失则新电脑 UI 崩溃（QSS 加载失败）
    (P('app/ui/theme'), 'app/ui/theme'),
    # JS 音源插件（musicfree/ lx/ 的 .js）：PROJECT_ROOT/data/music_sources，
    # 打包态 PROJECT_ROOT=exe 目录 → build.ps1 后处理移到 exe 目录（与 lol_data 同规则）
    (P('data/music_sources'), 'data/music_sources'),
    # JS 桥接模块整目录（桥接运行必需：bridge.js + lib/*.js + package.json +
    # node_modules；只收 node_modules 会漏 bridge.js 导致在线音乐桥接启动失败）
    (P('app/music/online/js_bridge'), 'app/music/online/js_bridge'),
    (P('app/third_party/fingerprint'), 'app/third_party/fingerprint'),
    (P('data/lol_data'), 'data/lol_data'),
    # 外部可写配置模板：只收 settings.example.yaml（空白模板，无任何真实凭据）。
    # 真实 config/settings.yaml 含用户 API Key，绝不进包；打包态首次启动由
    # config.py（R1）在 USER_DATA_DIR/config（%LOCALAPPDATA%\Starlight\config）
    # 自动生成默认 settings.yaml（不在 exe 目录——Program Files 只读）。
    # tools/.qwen_key 由 build.ps1 后处理创建空模板（避免误收开发目录真实 Key）。
    (P('config/settings.example.yaml'), 'config'),
    # 应用图标（运行时 QIcon 加载：main_window/tray 用 __file__ 相对定位，
    # 打包态命中 _internal/assets/icon/app.ico；EXE 图标通过 icon= 参数嵌入）
    (P('assets/icon'), 'assets/icon'),
] + pyside_datas + shazam_datas + pydantic_datas + pydantic_core_datas

binaries = [
    # 目录整体收集：mpv/node 下的 .exe 与全部 dll（防新增 dll 遗漏）
    (P('third_party/mpv'), 'third_party/mpv'),
    (P('third_party/node'), 'third_party/node'),
    # LGPL ffmpeg：指纹视频/M4A 等解码；运行时由 decoder 优先从 exe 同级目录查找
    (P('third_party/ffmpeg'), 'third_party/ffmpeg'),
    # R2: ASR 双后端——vulkan GPU + cpu AVX2，各自的 llama-server.exe + ggml-*.dll 全套
    (P('third_party/llama/vulkan'), 'third_party/llama/vulkan'),
    (P('third_party/llama/cpu'), 'third_party/llama/cpu'),
] + pyside_binaries + shazam_binaries + pydantic_binaries + pydantic_core_binaries

# R3: 模型种子由 setup_full.iss 单独安装到 {app}\models\（不进 PyInstaller datas，
# 避免 1.2GB 模型被打进 _internal 后又被 setup 重复装一次）。
# build.ps1 应在 PyInstaller 前检查 models/*.gguf 存在。

a = Analysis(
    [P('main.py')],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['torch', 'torchaudio'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Starlight',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # 禁用 UPX（杀软误报 + 无体积收益）
    console=False,    # 无黑窗：日志走控制台 Tab（LogBus.sig_log）+ 落盘，不弹外部控制台
    icon=P('assets/icon/app.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Starlight',
)
