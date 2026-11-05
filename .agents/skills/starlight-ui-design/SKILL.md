---
name: starlight-ui-design
description: 在星芒中设计、修改或核查 PySide6 页面、QSS、日夜主题、通知中心、灵动岛、独立窗口和 DPI/界面性能时使用。聚焦现有 UI 与真实状态的接线，不负责重构播放器、识别或游戏业务。
---

# 星芒 UI Design

遵守仓库根目录 `AGENTS.md` 和当前任务权限。以下路径均相对仓库根目录，只读任务不得顺手修 UI。

## 定位当前接线

- 主窗口/主 TAB：`app/ui/main_window.py`、`app/ui/widgets/main_tabs.py`。
- 主题：`app/ui/theme/{manager,colors,tokens,styles,applier,notification_style}.py`；偏好沿用 `app/ui/settings_store.py`。
- 音乐/电影：`app/ui/panels/{music_panel,movie_panel}.py`、`app/ui/widgets/{music_discovery,movie_home,discovery_cards,music_queue_dialog,video_player_window}.py`。
- 通知与胶囊：`app/core/notifications.py`、`app/ui/panels/notification_panel.py`、`app/ui/widgets/{notification_stream,voice_island}.py`。

## 保持体验与状态准确

- 同类入口放回原模块，先核实现有能力，不增加平行 UI。电影内部导航沿用电影、电视剧、动漫、综艺、发现、本地影片；发现展示综合预览，不接管其他分类状态。
- 线上影视卡片单击详情、双击按真实片名搜索，不自动播放；本地影片/继续观看维持原播放入口。来源 ID/完整卡片数据随事件传递，不用显示行号猜目标。
- 使用 ThemeManager、Colors/Tokens 和既有通知语义样式；避免新增散落的颜色值。跟随 `main.py` 在业务控件前初始化主题，后台通过排队信号请求换肤。
- 独立窗口、菜单、Overlay 一并检查；原生选择器、视频画面、封面和用户自定义歌词色要单独说明边界，不强制重新着色。
- 所有 UI 回传在 GUI 主线程；沿用 LatestTask/现有 Qt 信号，旧结果不能抢回页面或覆盖新分类。不用关闭/重建窗口实现同步。
- 通知按原操作 ID 合并识别、执行、结果；胶囊只展示当前操作，历史由通知中心承载。业务成功后 TTS 失败只附加播报异常，不反转业务结果。
- 加载、空结果、请求失败、不支持和未知状态分别显示；错误应有下一步，不能把超时说成“不支持”。列表用有限预览和滚动，避免大段无边界堆叠。

## 验证与交付

按改动选择 `tests/ui/{test_movie_card_interactions,test_media_homes,test_notification_center,test_notification_integrations,test_theme_switching,test_music_queue_view}.py`。真实 Qt 信号和事件循环验证主线程、快速连续操作、关闭后的晚结果。

布局改动检查两主题、窄窗口及相关 DPI；需要时用隔离数据截图并实际查看，注明假数据/缓存数据。离屏截图不能代替 Windows 多屏、原生对话框、真实音视频验收。没有新问题就停止，不为了覆盖率重做页面。
