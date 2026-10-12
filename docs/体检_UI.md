# 全项目功能体检报告 —— UI 模块

> 只读调查（2026-10-12），不改代码。覆盖：5 个主 Tab 面板、设置页、悬浮窗 Overlay、启动页、系统托盘、状态栏。数据来源：app/ui/ 全目录（24 个 py + dark.qss + colors.py），55 处内联样式逐处定位。

---

## ① 现有功能

### 主窗口（1100×720，QSettings 记忆几何）
5 个 Tab（main_tabs.py）：
| Tab | 面板 | 行数 | 内容 |
|---|---|---|---|
| 当前对局 | CurrentGamePanel | 401 | 双列卡片（左己方/右敌方）：每卡片 = 名字/段位/绝活Top3/等级/KDA/连胜/本局英雄是绝活？ |
| 自动化 | AutomationPanel | 229 | 自动排队/自动接受/自动禁用/自动选择 4 行（各有 ⚙）+ 连招总开关 + 管理连招按钮 |
| 对局信息 | OngoingPanel | 637 | 10 人列表：段位/胜率/KDA/绝活/近 20 场胜负点阵/连胜 |
| 音乐 | MusicPanel | 429 | 播放控制/队列/当前歌曲/扫描按钮 |
| 设置 | SettingsPanel | 422 | 4 组：🎵音乐目录管理+音源管理 / 🎤TTS 音色语速音量 / 🎮LoL 开关 / 🖥️刷新间隔 |
- 计时器 Tab（CooldownPanel 82 行）**被注释隐藏**（main_window.py:68 / main_tabs.py:64）
- 启动页 → 主 Tab 用 QStackedWidget 切换

### 悬浮窗 Overlay（overlay_window.py 339）
- 透明无边框置顶（Frameless + StaysOnTop + Tool + 不抢焦点 + WS_EX_LAYERED）
- **固定右上角**（x=屏幕宽-382, y=80），100ms 刷新
- 显示：敌方五人 CD（Q/W/E/R 字母块 + 剩余秒）+ 召唤师技能图标（Data Dragon 真实头像/图标，可点击→触发 CD 记录）
- CD 中红框/橙数字（<10s 变橙），就绪绿色
- 主窗口"显示游戏内 Overlay"勾选框控制显隐

### 启动页（loading_panel.py 67）
- 9 模块启动状态灯（eventlog/music/lol/tts/speech/asr/intent/js/automation）：○等待 / ◐加载中 / ●完成 / ●失败
- 实时日志框（log_bus 信号，max 200 条）

### 系统托盘（tray.py 55）
- QSystemTrayIcon + 菜单（显示主窗口 / 退出）；图标 = 蓝底白环（代码绘制）
- 关窗 = 隐藏到托盘（closeEvent ignore + hide）

### 状态栏（status_bar.py 57）
- LCU 连接 ●绿/●红 + ASR 就绪状态 + 🎤 麦克风开关按钮

### 主题
- dark.qss（141 行）：全局深色（#1e1e2e 底 / #e0e0e8 字 / #4a9eff 主色），覆盖按钮/Tab/列表/滚动条/滑块/输入/下拉/GroupBox/菜单/工具提示——**框架级控件统一**
- colors.py（20 行）：定义标准色板（BG/TEXT/ACCENT/SUCCESS/WARNING/DANGER/TEAM_ALLY/TEAM_ENEMY）

---

## ② 实现方式

### 触发链 / 数据流
```
main.py 启动 → QStackedWidget[LoadingPanel] → 模块就绪 → MainTabs
各面板 _timer（500ms 默认，设置页可调 100ms~）
CurrentGamePanel → snapshot_provider 轮询 → 双列卡片 update_data
OngoingPanel → ongoing_service / account_history → 10 人卡片
Overlay → OverlayModel（读 snapshot + cooldown 状态）→ 100ms 重绘
状态栏/托盘 → 服务对象实时状态（connected / is_available / toggle_mic）
```

### 依赖
- PySide6（Qt Widgets）、QSettings（窗口记忆）、QSystemTrayIcon、Data Dragon 图片资源（overlay_assets 本地缓存）
- 数据全来自下层服务（ongoing/cooldown/music），UI 纯渲染

### 限制 / 已知问题

**① 内联样式 55 处（重点展开）**——已统一进 QSS 的是**框架级控件**（按钮/Tab/列表等，dark.qss 生效）；**面板级细节全部内联硬编码**，分布：
| 文件 | 内联数 | 内容 |
|---|---|---|
| current_game_panel | 15 | 敌我边框色/标题色/连胜绿/本局英雄状态色 |
| combo_panel | 9 | 删除按钮红 #d9534f/提示灰 |
| loading_panel | 6 | 状态灯色 |
| settings_panel | 5 | 删除红/提示灰/标签 |
| overlay_window | 10 | CD 边框/倒计时橙/就绪绿（另有 rgba 内联） |
| ongoing_panel | 3 | 敌边框 #c62828/胜负点阵红绿 |
| 其余 | 7 | tray/status_bar/automation/music/combo_dialogs |

**② 色板混乱（重点展开：没统一）**——`colors.py` 定义了标准色，但**硬编码绕过色板**，同一语义多种颜色：
| 语义 | 出现色值 | 位置 |
|---|---|---|
| **敌方/危险红** | `#ef4444`（标准 DANGER）`#ff4a4a`（当前对局标题/边框）`#c62828`（对局信息敌边框/败场）`#ff5252`（Overlay CD 边框）`#d9534f`（删除按钮） | **同一语义 5 种红** |
| **盟友/强调蓝** | `#4a9eff`（标准 ACCENT）`#1e88e5`（托盘图标）`#3f51b5`（Overlay 蓝框） | 3 种蓝 |
| **成功/就绪绿** | `#4ade80`（标准 SUCCESS）`#4caf50`（连胜/Overlay 就绪） | 2 种绿 |
| 杂色 | `#cccccc`（当前对局文本）`#ff9800/#ffcc80`（Overlay 倒计时）`#ffffff`（托盘） | — |

**结论：没统一。** 特别是"敌方"概念——四个面板四个红，用户视觉上同一事物颜色不一致。

**③ 体验痛点**
| 面板 | 痛点 |
|---|---|
| **对局信息（最重 637 行）** | 10 人卡片每卡字段多（段位/胜率/KDA/绝活/20 场点阵），信息密度高、易挤；双队列段位文本长 |
| **当前对局（双列）** | 双列滚动区在 1100 宽下每列 ~500px，长段位（"钻石 I 57胜点 · 铂金 III 12胜点"）+ 绝活 3 英雄拼接可能换行错位 |
| **Overlay（最不灵活）** | **固定右上角不可拖动、不可调位置**（无 mouseMove 逻辑）；不跟随游戏窗口（游戏窗口移动/全屏它不动）；380px 宽塞 5 人 ×(技能+召唤师技能) 密集 |
| 音乐/设置 | 相对干净；设置页已做过紧凑化（目录列表 100px） |
| 状态栏 | 信息少但够用（LCU/ASR/麦克风），无音乐状态/无对局状态 |

**④ 其他**
- LoadingPanel 的 MODULES 是**静态列表**（9 项写死），与真实启动模块无绑定关系（main.py 是否调用 set_module_status 待核实——可能是死 UI）
- CooldownPanel 隐藏但有代码（死代码 82 行）
- 托盘图标代码绘制（无 .ico 资源文件）

---

## ③ 对标（成熟软件）

| 能力 | 对标对象 | 本项目 | 差距 |
|---|---|---|---|
| 主题统一 | 网易云/VS Code（设计 token） | dark.qss 框架级统一 + **面板级 55 处内联/5 种红** | 中——色板抽到 colors.py 但没执行 |
| 悬浮窗 | OBS/Akari（**可拖动、位置记忆**） | **固定右上、不可拖、不跟随游戏** | **大**——最该补的体验项 |
| 信息密度 | Akari 对局面板（紧凑分组） | 卡片式但字段多 | 中——可折叠/分组 |
| 托盘 | 标准（菜单+图标+气泡） | 齐平 | ✅ |
| 启动页 | 现代（进度+模块状态） | 9 模块状态灯 | ✅ 够用 |
| 设置页 | 分组/搜索/重置 | 4 组 GroupBox | 中——无"恢复默认" |
| 响应速度 | — | 面板 500ms 轮询（可调 100ms） | ✅ 无阻塞（数据在后台线程） |

---

## ④ 增补候选

| 候选 | 标注 | 说明 |
|---|---|---|
| **色板统一**（5 红 3 蓝 2 绿 → colors.py 常量；敌方统一 #ef4444、盟友 #4a9eff、成功 #4ade80） | 🟢 好用且能做 | 纯替换 55 处硬编码为 Colors.xxx；工作量小、视觉立刻统一；风险极低（颜色不改值只改引用） |
| **Overlay 可拖动 + 位置记忆**（mousePress/mouseMove 记录偏移拖动，QSettings 存位置） | 🟢 好用且能做 | ~25 行，参考标准 Qt 拖动模式；解决"固定右上不可调" |
| **Overlay 跟随游戏窗口**（FindWindow("League of Legends")+GetWindowRect 或用户选区域） | 🟡 能做但工作量大 | 需处理全屏/窗口化/无边框切换、DWM 偏移；做"贴游戏窗口右上角"较稳 |
| **Overlay 缩放/密度选项**（紧凑/标准/大图标） | 🟢 好用且能做 | overlay_model 已有状态，加尺寸系数 |
| **设置页"恢复默认"** | 🟢 好用且能做 | 重置 settings.yaml 相关节，~20 行 |
| **对局信息卡片折叠**（段位/绝活可折叠） | 🟡 能做但工作量大 | 涉及卡片重排版 |
| **启动页真实模块绑定**（MODULES 静态 → 接真实就绪事件） | 🟡 能做但工作量大 | 需 main 启动流程埋事件；当前可能死 UI（待核实） |
| **内联样式全抽 QSS**（55 处 → 主题类名） | 🟡 能做但工作量大 | 属"第二批美化"，与色板统一配套 |
| **毛玻璃/动画/渐变** | ⚪ 花架子 | 对局场景性能/遮挡代价大 |
| **多主题（亮色/自定义）** | ⚪ 花架子 | 用户只有深色场景（游戏对局） |

---

## 附：待核实项
- LoadingPanel 的 set_module_status 是否被 main.py 调用（静态 MODULES 是死 UI 还是真实接线）
- overlay_assets 的 Data Dragon 图标缓存机制（离线是否退化）
- QSettings geometry 与 1100×720 默认值冲突时（用户曾拉伸到小窗口）行为

## 建议优先做（若采纳）
1. **色板统一**（🟢）——5 红 3 蓝 2 绿 → 4 个常量，成本最小收益最大，且为后续 QSS 抽取打底
2. **Overlay 拖动 + 位置记忆**（🟢）——悬浮窗当前"固定不可调"是最明显体验短板
3. **Overlay 跟随游戏窗口**（🟡）——第二步做，贴游戏窗口边缘
