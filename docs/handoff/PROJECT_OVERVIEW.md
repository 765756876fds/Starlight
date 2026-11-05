# Starlight 2.0 — 架构概览

## 2026-11-05 用户通知边界

- `core/notifications.py` 是有限内存记录：关联操作、合并阶段、保留 200 条，业务状态和播报状态分开。没有数据库、原始日志解析或业务任务队列。
- 语音入口创建操作标识，`ContextVar` 仅传递通知上下文；`Command.notification_id` 让现有异步命令保留关联。CommandBus 的业务返回值不变，通知观察异常只记日志，不能改变已完成的业务结果。
- `SpeechItem.notification_id` 关联播报状态。原有优先级、过期和独立游戏提醒保留；播报失败只更新附注，不改变业务状态或重新执行命令。
- Qt 的 `NotificationStream` 将后台变化排队回 GUI；原控制台入口改为通知中心，StatusBar 的胶囊只展示当前交互，点击进入已有通知页。没有增加顶层 TAB 或第二套语音服务。
- 新组件的语义颜色及图标角色集中在 `ui/theme/notification_style.py`。目前接入暗色主题，浅色调色板只作为未来统一适配接口，未增加主题切换功能。
- `LogBus`、TeeStream 和原有日志落盘机制保持；普通通知不订阅原始技术日志。开发人员仍从既有日志文件排查问题。

## 定位

Starlight（星芒）是一个 Python + PySide6 桌面整合应用，整合本地音乐播放、电影播放、LoL 游戏辅助（连招/对局信息/英雄数据）和语音助手（ASR + TTS）。

## 技术栈

- GUI：PySide6 (Qt)
- 播放器：MPV（外部进程 + IPC）
- 数据库：SQLite（music.db / player.db）
- LoL 接口：LCU（League Client）+ Live Client API（2999 端口）
- ASR：Qwen3-ASR + llama-server（vulkan/cpu 双后端）
- TTS：Edge TTS / Qwen TTS
- 打包：PyInstaller onedir + Inno Setup

## 主要模块

- app/core/：配置、路径解析、日志、事件总线
- app/music/：音乐播放、扫描、队列
- app/movie/：电影播放、海报缓存
- app/lol/：LoL 相关（连招、对局信息、英雄数据、LCU）
- app/voice/：ASR 启动器、语音识别
- app/speech/：TTS
- app/ui/：Qt UI 面板和对话框

## 路径解析（R1 后）

- 程序资源目录（只读）：frozen 态 = exe 目录（Program Files\星芒）
- 用户数据目录（可写）：frozen 态 = %LOCALAPPDATA%\Starlight
  - config/settings.yaml
  - data/music.db, player.db
  - data/lol_data/（用户更新的英雄数据）
  - data/movie/posters/（电影海报缓存）
  - models/（ASR 模型）
  - logs/
  - tools/.qwen_key

源码模式：两者都在项目目录。

## 已知约束（待核实）

- EventBus 并发模型：待核实
- mpv 生命周期：子进程管理，待核实
- Qt 线程安全：待核实

## 统一主题（2026-11-05 增补）

- `app/ui/theme/colors.py`：两套语义调色板；`tokens.py`：现有外观 token 的派生值。
- `app/ui/theme/manager.py`：每个 QApplication 一个 GUI 线程主题管理器，统一更新既有控件，不重建业务状态；后台请求通过排队信号进入 GUI 线程。
- `app/ui/theme/styles.py` 适配已有内联语义颜色；原生 QIcon 缓存由主题切换统一更新。不使用全局 StyleChange 拦截或 Python QIconEngine。
- 偏好沿用 `ui_prefs.json`，默认 dark，允许 light；通知中心与灵动岛直接读取相同语义颜色。完整验证与边界见 [日间夜间主题验收](../日间夜间主题验收.md)。


## 2026-11-05 娱乐中心增量结构

- 保持5主TAB。MusicDiscovery是在线音乐子页的轻量目录入口，MusicFree能力元数据/分类/歌单统一经现有Node桥；CatalogResults保留部分失败，未建立第二套音源或播放队列。
- PlayQueue在既有列表旁维护稳定的入队条目标识；move_waiting在原锁下重排并重映射索引/历史，不重新生成随机序列，随机模式拒绝拖动。界面提交ID与revision，最终以控制器快照为准。
- MovieHome复用现有电影控制器/历史/原生watch_later；MovieLibrary仅管理用户选择目录的文件名索引（data/movie/library.json），MovieDiscovery独立管理公开信息缓存（data/movie/public_ranks.json）。两者均位于现有USER_DATA_DIR，不把信息源当作播放源。
- LatestTask是内容读取的单后台任务/最后待办助手，Qt排队回传；只忽略旧完成结果，不宣称取消已发HTTP，也不是第二套语音任务队列。
- GameWorkbench包裹CurrentGamePanel，保留原current面板引用供旧刷新逻辑使用；使用现有连接状态和游戏时间计时，不增加LCU请求、游戏自动操作或新提醒时钟。
- 所有新增UI继承现有主题token/QSS，结果走既有通知中心。架构与限制详见 [阶段性娱乐中心升级验收](../阶段性娱乐中心升级验收.md)。


## 2026-11-05 首页分类与按需展示增量

- MovieHome仍在电影TAB内部：发现页为有限卡片预览，分类页为独立滚动网格；继续复用MovieLibrary、观看历史、原生watch_later。MovieDiscovery可按每类limit交付，底层保持完整缓存与部分失败语义，信息源不等于播放源。
- MusicDiscovery仍在原在线子页：六区工作台+内部分类，按插件导出读取目录/详情，有限内存缓存；最近播放和我的歌单调用原MusicPanel入口。搜索结果用同一子页内QStackedWidget与首页分离，不新建播放系统。
- discovery_cards.py为两模块共享的轻量预览轨道/虚拟网格和绘制组件，复用既有主题token和异步封面加载器；不新增外部框架、媒体数据库或任务队列。
- MusicFree新增浏览操作只转接已存在的search/getArtistWorks/getAlbumInfo，保持原音频搜索和播放接口。播放、随机、插队优先、字幕、ASR/TTS及LoL未改。
- 完整本轮差异、测试及来源边界见 [首页体验验收](../音乐电影首页体验升级验收.md)。


## 2026-11-05 公开榜单扩充与发现可靠性（本次实际施工）

- rank_sources.py仅适配公开排名元数据：IqiyiRank读取SSR排名DOM，不执行下载JS；TencentRank复用官方公开热搜请求，保留热搜属性和官方搜索动作。与播放解析和本地MovieLibrary独立。
- MovieDiscovery沿用缓存文件/TTL/并发池，新增每来源分类的写入代次保护，只阻止旧结果污染缓存，不撤销HTTP或业务操作。
- MusicDiscovery在当前浏览集合内按页替换曲目；不对歌曲ID去重、不修改MusicQueue。Node桥明确区分结构异常和成功空数据。
- 全部新界面仍复用现有主题token、LatestTask排队回传和通知中心；未新建音源系统、数据库、任务队列或播放器。
