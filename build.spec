# -*- mode: python ; coding: utf-8 -*-
"""LoL_Music_Agent 打包 spec（onedir 绿色文件夹）。

布局说明（PyInstaller 6.x，contents dir = _internal）：
- 代码 + Python 库 → dist/LoL_Music_Agent/_internal/
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
from PyInstaller.utils.hooks import collect_all

# PySide6 全量收集（Qt 平台插件 / 翻译 / 动态库；官方 hook 之外的保险）
pyside_datas, pyside_binaries, pyside_hidden = collect_all('PySide6')

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
] + pyside_hidden

datas = [
    ('app/music/online/js_bridge/node_modules',
     'app/music/online/js_bridge/node_modules'),
    ('app/third_party/fingerprint', 'app/third_party/fingerprint'),
    ('data/lol_data', 'data/lol_data'),
] + pyside_datas

binaries = [
    ('third_party/mpv/mpv.exe', 'third_party/mpv'),
    ('third_party/mpv/d3dcompiler_43.dll', 'third_party/mpv'),
    ('third_party/node/node.exe', 'third_party/node'),
] + pyside_binaries

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['torch', 'torchaudio', 'pydantic'],  # pydantic 未被项目 import（requirements 冗余），排除避免 hook 报错
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LoL_Music_Agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # 禁用 UPX（杀软误报 + 无体积收益）
    console=True,     # 保留控制台日志（对齐启动.bat 习惯，便于实机诊断）
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='LoL_Music_Agent',
)
