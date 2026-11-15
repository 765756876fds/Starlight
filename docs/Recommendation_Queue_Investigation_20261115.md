# 音乐推荐队列专项核查（2026-11-15）

## 结论

基线 a0-cleanup / 962b5f7877a8e4afd707930def596cead5fd8c17。用户明确确认经“为我播放／我想听歌了”起播后切歌遇到空队列，不能将其归因为普通搜索单曲。现有日志确认切歌时为空，但不能还原该次榜单歌曲数量、推荐回调是否执行、是否存在随后清空或替换；隔离环境尚未复现“多首榜单推荐成功后队列意外为空”。根因未确认，本轮不修改生产代码，不声称已修复。

## 脱敏日志时间线

- 13:23:19.608：进入在线播放入口；该入口为普通单曲、榜单与推荐共用，日志没有区分调用来源。
- 13:23:19.634：loadfile得到明确success，queue_size=0、index=-1、mode=shuffle。这里位于推荐填充回调之前，不能直接判为缺陷。
- 13:23:19.636：记录mpv loadfile ok。
- 13:23:56.387：next记录queue_end，queue_size=0、index=-1、mode=shuffle；此时为空是真实诊断结果。
- 13:23:56.682：MPV end-file reason=stop，与控制器队列到尾后停止相符。
- 13:24:19：随后又一次在线起播；没有依据将这次起播当作前次队列状态。

没有复制私人歌曲名、完整播放URL、原始插件响应或用户数据库。当前未检测到可用于核对该次会话的运行中主程序；不以当前源码推断当时实际执行的安装版或进程版本。

## 调用与写入边界

- `RecommendationService.choices`对每个榜单详情取本次tracks列表（最多50首），从前20首选择起点。
- `RecommendationService.play`经`play_online(..., _recommendation=True, _on_loaded=loaded)`起播。回调将所选歌曲旋转到首位，clear/add_all/play_at(0, collection_start=True)，再写入推荐session。
- `MusicController.play_online`持同一业务锁执行_apply_stream与loaded。首次loadfile诊断发生在回调之前；回调成功后才返回。
- `next`取消未完成推荐代次，但保留已建立session，消费现有PlayQueue；`_auto_next`依实时idle状态调用同一路径。单曲循环的自然结束重播当前歌曲，手动下一首按原规则推进。
- “换一首”走MUSIC_NEXT，“换一个”走真实媒体情境判断；当唯一可靠对象为音乐且队列有效时，最终均消费MUSIC_NEXT，不重新读取榜单。
- 明确clear_queue会清空队列及推荐session；普通play_online没有凭空创建榜单集合。后者可以产生同样的日志，但这只是对照，不能据此否定用户确认的推荐入口。

## 隔离测试与结果

测试在项目外副本运行，来自HEAD，并叠加本轮开始时controller中已有的41行诊断增量；未混入其他历史业务修改。配置、日志、SQLite、pytest缓存和生成音频均隔离，关闭自己创建的MPV，不操作用户曲库或播放器。

统一入口：`.venv/Scripts/python.exe -B -X faulthandler <证据目录>/run_tests.py <隔离副本> <证据目录> <组名> [--qt] <测试节点>`。证据目录为 `D:/starlight-recommend-queue-10hp54w7`，每组`*-command.json`记录准确节点与参数，`*.xml`记录JUnit。

| 组名 | 范围 | passed | failed/errors | skipped | warnings | exit |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| baseline-queue | 新增18项：5首集合，三模式，手动/自然/两种语音，单首/空榜/清空/单曲对照 | 18 | 0 | 0 | 0 | 0 |
| load-boundary | 新增1项：首次loadfile时0/-1，推荐返回后5/0 | 1 | 0 | 0 | 0 | 0 |
| related-unit | 推荐、插队、快照、媒体情境、电影语音及播放 | 243 | 0 | 0 | 0 | 0 |
| related-qt | 推荐控件、队列窗口、语音电影窗口 | 50 | 0 | 0 | 0 | 0 | 0 |
| native-queue-final | 新增3项：生成WAV经隔离音源及真实MPV起播/EOF/队列接续 | 3 | 0 | 0 | 0 | 0 |

唯一用例315项：新增22项、既有回归293项，不累计重叠执行。真实MPV只证明本地生成音频、IPC及接续，不是公开平台联网或扬声器实机验收。新测试的5首/2首/1首数据均为明确的隔离测试数据；无法由旧日志恢复用户实际榜单首数。

首败与测试清理记录：第一次原生运行3项setup error，隔离副本不含未跟踪的mpv.exe；通过STARLIGHT_TEST_MPV明确使用项目现有二进制后运行。其后3项通过，但测试先看到loadfile返回就退出，出现一次自己的worker在fixture关闭数据库后写历史的错误；补充Event等待本测试worker完成（包含历史写入），最终3项通过且无该错误。原始日志均保留，未改生产生命周期代码或跳过断言。未见Windows fatal exception、access violation或0xc0000374/0xc0000005。

两份新测试Ruff零违规，compileall通过，git diff --check及cached检查通过。生产源码未改，因此没有“修复前失败、修复后通过”的业务证据，也不提交/推送或重复触发Windows CI。已有基线CI结果不能冒充本轮新增测试已在远端运行。

## 后续最少实机证据

只需在自然使用时点击一次“为我播放”，确认界面报告的榜单名称，并立即打开播放队列记录当前及待播条数，再说一次“换一首”。若已经为空，记录时间和当前源码/安装版启动方式；若先有多首后变空，记录哪一步变空。无需提供私人歌曲名、播放URL、数据库或录音。

在得到队列首次建立/丢失时点之前，保留未定位状态，不声称用户误操作，也不通过自动重建队列掩盖问题。不改电影推荐、跨媒体暂停/继续、LoL、网络及安装包。
