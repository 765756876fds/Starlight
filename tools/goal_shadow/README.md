# B1：LISTEN_MUSIC 只读影子研究

本目录没有被`app/`、`main.py`或正式语音路由导入。没有正式影子钩子、默认开关、播放器执行器或常驻服务。显式创建`ShadowObserver`只返回建议，不运行方案。

## 契约及边界

- `contracts.py`：Goal/GoalConstraints、WorldStateSnapshot、CapabilityEvidence、CandidatePlan、DecisionResult。资源状态区分available/unavailable/unverified，未知不会升级为可用。
- `readonly.py`：仅读取控制器短锁下字段、PlayQueue计数/修订及RecommendationService已有缓存；SQLite使用独立`mode=ro&immutable=1`连接，不调用构造/迁移/profile/choices/play/get_top_lists。发现WAL或journal即拒绝主库文件快照，不隐藏WAL中的新数据。
- `mpv_read_port.py`：只连接调用方明确提供的Windows命名管道，独立连接、不复用正式播放器的回复流；仅允许4种get_property，禁止loadfile/pause/stop/quit。不会启动或关闭播放器。
- `providers.py`：三个Provider只提供候选和证据。`decision.py`统一比较；保持/恢复优先，明确偏好先于历史，电影活跃或未知时不自创跨媒体启动策略。
- `observer.py`：最多一个工作线程、无排队，忙碌返回False，关闭/超时后的结果丢弃。任意外部回调若忽略超时，Python不能强杀线程；close会返回False，不谎称退出。没有把该观察器接入正式软件。
- `audit.py`：显式文件位置的只读聚合检查；只输出计数/原因，不输出歌曲、歌单、路径、播放URL或完整语音。

数据库成员记录及榜单元数据都不是可播放证明。目前仅当前被独立MPV读取确认的同一媒体可获得即时播放能力证据。静止音乐＋任意收藏或旧榜单缓存通常需要待核验；本工具不会主动播放来验证。

`NO_OP`可以基于真实进度推进证明目标已满足；`RESUME_MUSIC`仅是恢复建议，不能声称已经恢复。`PLAY_PREFERRED/PLAY_CHART`需要新鲜、可信的特定内容能力证据；目前真实缓存/数据库不提供完整证明，此分支主要由隔离证据测试保护。

## 验证命令

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -B -m pytest tests/unit/test_goal_shadow.py -o addopts="" -p no:cacheprovider
& $py -B -m pytest tests/ui/test_goal_shadow_responsiveness.py -o addopts="" -p no:cacheprovider
$env:STARLIGHT_TEST_MPV = 'C:\path\to\existing\mpv.exe'
& $py -B -m pytest tests/integration/test_goal_shadow_mpv.py -o addopts="" -p no:cacheprovider
```

Qt测试应在独立进程中先创建QApplication与ThemeManager，遵守项目测试生命周期约定。native测试只启动自身生成WAV、静音输出、独立IPC的MPV，并回收自身进程。

显式数据库审查示例：

```powershell
& $py -B -m tools.goal_shadow.audit --music-db C:/path/music.db --preferences C:/path/music_preferences.db --output D:/new-audit.json
```

不会创建缺失库或覆盖既有报告。CLI不连接真实用户播放器；要观察同进程controller必须由独立实验显式传入，正式V1没有调用该接口。请先看B1中文报告，再讨论后续生产接线。
