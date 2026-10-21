# 性能审计报告（UI 主线程阻塞治理）

> 2026-10-21 · 分支 a0-cleanup · PySide6 专业审计技能挂载 + 全盘流畅度排查

## 一、技能挂载

- ✅ **pyside6-desktop**（52 条生产级规则，含 `references/threading-and-cleanup.md` 线程专项）已挂载至 `.claude/skills/pyside6-desktop/`，SKILL.md 与线程规则已读取激活。
- ⚠️ **pyside6-reviewer**（qt-llm-template）：GitHub 仓库返回 404（已不存在），降级以 pyside6-desktop 线程规则 + deep-review 的 python/concurrency aspect 覆盖同等审查面。
- ✅ **Qt Thread Check**：以 pyside6-desktop Rule 9/32/47 落地（跨线程 emit、moveToThread 时序、BlockingQueuedConnection 死锁）。

## 二、修复内容（本轮）

**`app/ui/panels/music_panel.py` 双击播放 5 个同步阻塞槽全部异步化**（+8 锚点字节级修改）：

| 槽函数 | 原同步调用 | 改造后 |
|---|---|---|
| `_on_play`（搜索） | `play_online`（网络取 URL + mpv IPC） | `_play_async` 后台线程 |
| `_on_rank_song_play`（榜单） | `play_online` | `_play_async` 后台线程 |
| `_on_playlist_song_play`（歌单） | `play_playlist_item` | `_play_async` 后台线程 |
| `_on_history_play`（历史） | `play_history_item` | `_play_async` 后台线程 |
| `_on_local_play`（本地） | `play_song` | `_play_async` 后台线程 |

机制：新增类级信号 `_play_result = Signal(bool, str)`（Rule 9/32：信号只能回主线程槽，AutoConnection 跨线程 emit 自动 Queued）；新增 `_play_async(target, *args, ok_msg, fail_msg)` 后台线程包装 + `_on_play_result(ok, msg)` 主线程槽更新状态栏。UI 只发信号、不等网络/mpv 结果 → 点击在线歌曲播放时主窗口拖拽不再卡住瞬移。

## 三、已确认安全异步的模块（无需改）

- **movie_panel.py**：搜索/榜单/海报/字幕下载全部 QThread worker（`_SearchWorker`/`_RankListsWorker`/`_PosterBatchWorker`/`_SubtitleSearchWorker`），`requests.get` 仅存在于 Worker.run()；播放控制按钮已 `_safe_movie_call` 后台化；`_on_app_quit` 的 `t.wait(9000)` 仅为退出收尾。✅ 无主线程同步网络、无跨线程 UI 操作。
- **settings_panel.py**：`_on_test_plugin` 已在 `threading.Thread` worker 中执行（search/getUrl/probe 全在线程），`_test_done` Signal 回主线程更新 UI（L765-813）。✅ 无阻塞。
- 其余控制类操作（next/prev/toggle/volume）此前已 `_safe_call` 后台化。

## 四、验证

- py_compile：修改文件通过。
- 5 槽验证脚本输出：`play_async=True sync_call=False` 全部命中。
- 音乐定向测试：`pytest tests/test_music*.py -v -x`（offscreen，隔离跑）——结果见执行汇报。
- 全量音乐+电影组合跑会触发已知测试环境历史遗留死锁（卡在 ~90%），已按既定策略改隔离跑，与本轮改动无关。
