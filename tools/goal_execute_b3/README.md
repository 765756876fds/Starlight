# B3：LISTEN_MUSIC 独立执行研究

本目录没有生产导入或语音钩子。只绑定自己创建的 `Lab`，不能传入正在运行的 MusicController，不能发现用户 IPC 或曲库。

继承 B1 `Goal/GoalConstraints`、只读 MPV 端口和稳定身份；继承 B2 `World/Capability/Decision/Plan/Action/Outcome`。`Observation`、`Result` 只是隔离适配封套，不是第三套规划器。ACCEPTED 用独立字段记录，不等于 STARTED。

## 运行

在项目开发Python环境下，提供现有 MPV 和一个**不存在**的项目外输出目录：

```powershell
python -B -X faulthandler -m tools.goal_execute_b3.run --mpv C:/娱乐助手/Starlight/third_party/mpv/mpv.exe --output D:/new-b3-evidence
```

每个场景独立Python子进程，最多20秒，失败立即停止；已有结果拒绝覆盖。进程恢复只针对本轮清单中PID、创建时间、可执行文件和唯一IPC均匹配的MPV，不执行全局终止。日志、数据库、静音WAV、损坏文件和原始回执留在新实验目录，不入Git。

实际业务复用：

- NO_OP：只复核真实状态，零业务调用。
- RESUME：独立CommandBus → 既有MUSIC_RESUME handler → 音乐控制器 → MPV。
- TRY_COLLECTION：正常WAL只读成员快照 → 稳定成员身份 → 既有play_playlist_item → play_song → MPV。
- TRY_CHART：本地确定性音源 → 既有OnlineSourceManager/RecommendationService/CommandBus → play_online → 建队列 → MPV。

MPV使用无配置、禁用脚本、null音频/视频输出和唯一IPC；播放适配器拒绝生成目录外路径。不会联网或加载实际插件。第二个MPV仅在影视边界探针中播放生成音频，验证状态隔离，不验证真实视频画面。

## 测试

`tests/unit/test_goal_execute_b3.py` 不需要MPV。

`tests/integration/test_goal_execute_b3_mpv.py`、`tests/ui/test_goal_execute_b3_responsiveness.py` 必须显式设置 `STARLIGHT_TEST_MPV`；缺少条件直接失败，不跳过或伪造解码。Qt用offscreen，并先初始化QApplication/ThemeManager。pytest关闭默认coverage，指定项目外basetemp/JUnit，避免触碰用户文件。

## 尚不能生产接线的原因

实验 `action_gate/manual_revision` 只约束本轮自己创建的调用方，正式界面不使用这个门闩。收藏播放入口没有expected-request参数，也没有与成员revision一致的原子compare-and-play契约；不能把事前只读检查声称为全局原子授权。

已有收藏调用即使MPV发送前失败，也可能已经修改临时队列；接受损坏媒体时还可能记录尝试历史。B3不回滚这些既有业务副作用、不重写自动跳过，只拒绝继续外层回退。仅“注入业务前拒绝”探针证明无副作用时允许第二个计划，绝不宣称所有解码失败都能安全换源。

总外层预算最高5秒，最多2个计划、推荐宏最多一次；通过已有is_current/expected_request限制推荐，不叠加多个45秒预算。轮询/IPC/系统调度可能越过名义毫秒截止，不能承诺硬实时。真实线上源、扬声器、麦克风和用户正式程序均不属于此次实测。

完整数据与限制见 `docs/goal_research/20261117_B3真实隔离执行闭环验收.md`。本轮结束后不自动接入VoiceRuntime。
