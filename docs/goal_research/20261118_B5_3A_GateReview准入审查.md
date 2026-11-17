# B5.3-A Gate Review：暂不批准真实麦克风试运行

日期：2026-11-18。HEAD仍为 `e4f4563d6a1acb212509f2b70aafcc1b418a34e8`，分支a0-cleanup。本轮是准入审查，没有修改生产代码、现有测试断言、开关默认值或用户数据，没有启动真实语音。

## A．冷启动体验：关闭时保护，开启时存在明确缺口

现有调用链已核对：Runtime仅为目标语句附许可，原CommandBus分发到Controller的薄推荐handler；ListenMusicTrial调用GoalAdapter.run，run先prepare、后检查allowed_plans，最后才可能调用B4。

问题明确发生于**B4之前**：当比较器选择TRY_CHART，allowed_plans检查返回 `rejected_unavailable / plan_not_authorized`；Trial将其转成UNAVAILABLE，Controller因结果非None不会进入旧recommend_music。这里没有B4受理，也没有播放副作用，但旧版本来可用的在线推荐被试验入口挡住。

本轮22项矩阵使用临时数据库、真实Controller/B4结构、替身IPC；普通旧路径用原返回对象spy区分路由，不把spy当实际播放。电影活动/未知拆成两行，关闭后再次请求另列一行：

| 状态 | 关闭入口时 | 开启入口时 |
| --- | --- | --- |
| 1．可靠播放 | 原V1推荐，仍可能换内容；不是新NO_OP | NO_OP，经B4进度证明；无新loadfile |
| 2．可靠暂停 | 原V1推荐；不是强制新恢复语义 | RESUME_MUSIC，经B4恢复和证明 |
| 3．有效本地收藏有明确偏好优势 | 原V1推荐器，不强制播放收藏 | TRY_COLLECTION，经权威成员复核 |
| 4．无适合收藏、原推荐器可用 | 原V1推荐 | **拒绝TRY_CHART，未调用V1；不满足本轮准入要求** |
| 5．收藏文件失效、原推荐器可用 | 原V1推荐 | **同上，构成另一明确冷启动缺口** |
| 6．音乐状态未知 | 原V1仍按原就绪、代次与互斥契约处理 | needs_verification，不假定stopped，不执行 |
| 7a．电影活动，音乐停止 | 原V1保留原电影暂停确认与起播前复核 | needs_clarification，不抢占 |
| 7b．电影状态未知，音乐停止 | 原V1原有互斥确认契约，不新增“允许”推断 | needs_clarification，不抢占 |
| 8．新请求已ACCEPTED，未STARTED | 关闭不能撤销IPC；精确目标在在途收尾期间不自动转V1 | 仅报告受理/收尾，无进度证明不成功，不开第二计划 |
| 9．已有UNKNOWN_EFFECTS | 不自动转V1；收尾后新的明确关闭态请求才可进入原业务 | 原Adapter保留未决归属，拒绝再发计划，不自动重试 |
| 10．显式关闭后再次请求 | 空闲/收尾结束后的明确请求回原V1 | 开关事实上已关闭，后续行为同左；不是给旧请求追加重试 |

第8、9项的矩阵回执为隔离合成；另外执行了原有在途受理/关闭/重新开启测试，验证同一Adapter保留pending、只发一次加载和通知归属。不能把合成回执称为真实MPV起播。

### 真实隔离MPV冷启动对照

使用本轮拥有的四个MPV实例、临时music.db/music_preferences.db、生成WAV和独立IPC。榜单由完全本地确定性Source替身提供，仍经过原RecommendationService；未联网。

| 本地收藏 | 开关 | V1调用 | B4准入 | 音源替身调用 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 无 | 关 | 1 | 0 | 4 | CONFIRMED、两首队列，真实进度推进 |
| 无 | 开 | 0 | 0 | 0 | UNAVAILABLE，未加载 |
| 文件失效 | 关 | 1 | 0 | 4 | CONFIRMED、两首队列，真实进度推进 |
| 文件失效 | 开 | 0 | 0 | 0 | UNAVAILABLE，未加载 |

这既证明旧链在同一隔离资源条件下可用，也证明缺口是试验路由行为，不能归咎于外网不可用。真实在线平台仍未测试。

## B．三个旧并发超时：已定位既有耗时点，首次失败根因未最终确认

原始首败完整保留：`D:/starlight-b53a-b7968e76/regression-goals.log`、对应JUnit和退出记录。原批次94 passed/3 failed，退出1。三个节点：

1. `test_goal_adapter.py::test_race_immediately_before_b4_admission_rejected_by_its_owner[manual]`：执行a.execute的线程未在in_worker的1.5秒内返回；内部手动play_song已进入该线程。
2. `test_goal_admission.py::test_manual_new_song_waiting_for_admission_invalidates_before_mpv`：Goal线程已结束，手动play_song线程未在join(1)后退出；代次变化断言此前已经通过。
3. `test_goal_admission.py::test_database_removal_serializes_with_collection_admission`：受理线程未在join(1)后退出，删除成员线程仍需等待受理持有的数据库权威锁。

**原失败日志只有失败断言处的主线程调用栈，没有当时工作线程栈或CPU/磁盘调度证据。** 不能事后把“环境繁忙”写成已证实根因。

### 本轮有限对照与新证据

- 使用上轮保存的施工前副本（B4+R+B5.2，无B5.3接线）与B5.3-A隔离候选，原Python/同一主机/原三节点/原断言。两者各3通过，退出0；不是用这一结果直接宣称无回归。
- 另各进行一次相同三节点、仅观察的早期线程取样（0.1秒，单次采样、会话结束清理）。没有注入sleep、网络、锁延迟或额外负载，也未修改测试deadline。
- 两版本三节点的工作线程均采到 `PreferenceStore._run` 第48行 `db.executescript(...)`：首次建立偏好表，调用链来自`record → _play_song → _play_selected_song`。施工前/当前最大_run约580/586ms，手动播放或受理调用相应约半秒；两个版本均会出现该冷初始化耗时。
- 第三个节点同时采到remove_from_playlist停在数据库锁获取处；受理正持有成员权威锁并在上述偏好初始化路径中。这解释了该场景的具体等待关系，没有观察到双方循环等待。
- `_confirm_playback`约800ms的已有启动等待也被记录；没有证据说明它就是首次失败主因，未为避开它而禁用自愈/坏文件确认。
- 对比AST：play_song、_play_selected_song、_play_song、_confirm_playback、GoalAdmission.admit、GoalAdapter.execute、数据库计数/成员删除及PreferenceStore._run/record在前后完全一致。诊断中ListenMusicTrial.handle和Controller._cmd_recommend调用次数均为0。这排除了“这些测试实际走了新语音入口”的说法。

裁定：有证据表明存在**旧冷初始化耗时及既有锁等待**，没有发现新适配器在这三节点的直接执行或函数变更。首次1～1.5秒超时是否由同期负载把这些耗时放大，还是另有调度/收尾因素，仍未证明。它不是单凭串行通过就关闭的风险，也不能断言已排除所有组合回归。

本轮不改PreferenceStore初始化、Controller锁或测试夹具：这些会影响真实冷启动与并发语义，仅凭当前取样不足以宣布修复首次失败。尤其不预热夹具掩盖真实初始化成本、不延长join、不删除断言。

## C．实际差异与为什么没有临时补调用

正式业务源码、B5.2/B5.2-R/B5.3-A原测试及验收报告均保持原字节。新增工具/矩阵/取样只保存在工作区外`D:/starlight-b53gate-9571903a`；仓库仅新增本报告、追加五份维护交接记录。

冷启动缺口的后续最小修复应是**准入前、一次性选择V1或Goal执行入口**，不是处理某个失败返回后再调用recommend_music。两条路径不能同时拥有同一请求。

当前AdapterResult的未许可返回保留了Decision，但没有用于旧业务转交的完整Prepared快照；Command.is_current只验证语音代次。Controller.recommend_music会先推进原受理版本，再交给原推荐器。若简单在“plan_not_authorized”后返回None，虽然该特定结果本身未准入，仍可能遗漏准备期间的手动变化、资源变化或并发关闭；更不能把同一做法推广到其他REJECTED/UNKNOWN结果。

已有RecommendationService的expected_request、is_current、来源版本和起播前电影确认可复用，但需明确由所有者完成旧版本核验及路由转交，并保持这些守卫直到加载。该契约还没有在本轮被实施/验证。因此本轮保留明确失败证据，不在Gate Review里临时拼接自动回退或扩展Planner。

## D．默认关闭、回执和旧业务安全

- 正常启动不创建ListenMusicTrial；Runtime绑定方法才惰性导入新入口。显式创建但关闭时，仍按原命令参数返回同一V1对象，不读新快照、不初始化B4、不访问新资源。仅有已安装对象的短元数据检查。
- 所有已执行的新计划经B4；未许可TRY_CHART在B4之前拒绝。没有适配器直调MPV。
- 取消ticket/关闭入口不能撤销已发送IPC；已受理的未知结果不引起第二次加载，重新启用仍保留原pending。旧语音结果使用原通知ID，不能覆盖新语音完成状态。
- ACCEPTED/UNKNOWN_EFFECTS没有被当STARTED；只有原证明路径产生成功。矩阵对V1的ACCEPTEDspy也不解释为实际播放。
- 原推荐器保留语音代次、用户手动换歌后的请求版本、来源版本、电影暂停确认后再复核，以及旧结果不发布成功的约束。抽测对应取消、确认、自然接续、随机/顺序/单曲循环与语音下一首反馈，未改原断言。
- 当前实现的“无重复副作用”保护通过所选边界测试，但**保持安全拒绝并不等于保护了冷启动可用性**；第4/5项仍不通过。

## E．实际验证记录

全部在工作区外运行，未启动用户播放器/麦克风；没有真实外网请求。使用项目Python，关闭pytest默认coverage/cache。外部入口及完整参数保存在run.py/validate.py/probe.py/probe_stack.py，各批日志/XML/exit.json可复核。

| 本轮批次 | passed | failed | skipped | warnings | 退出码 |
| --- | --- | --- | --- | --- | --- |
| matrix：11状态×开关（未满足准入要求照常失败） | 20 | 2 | 0 | 0 | 1 |
| cold-native：两个冷启动场景×开关 | 2 | 2 | 0 | 2 | 1 |
| boundaries：选定旧语音/推荐/回执/队列边界 | 32 | 0 | 0 | 0 | 0 |
| probe-baseline / probe-current | 各3 | 0 | 0 | 0 | 各0 |
| stack-baseline / stack-current | 各3 | 0 | 0 | 0 | 各0 |

当前代码唯一节点去重：**57 passed、4 failed、0 skipped、2 warnings**；3个基线节点及重复取样不重复累计。四个失败就是两个真实冷启动缺口的单元路由与原生对照，未跳过、未改答案让它们通过。

原生批的两项warning为外部测试根未读项目marker配置，导入既有integration模块产生PytestUnknownMarkWarning；完整保留，非运行时异常。不要将本轮写成“全绿”。

本轮四份自有MPV清单对应进程均退出，日志未出现0xc0000374、0xc0000005、access violation或Windows fatal exception。未新增源码，因此不重复全量编译/Ruff；文档增量与git diff --check检查另在收尾记录。

## F．准入结论与停止点

**不建议批准B5.3-B真实麦克风有限试运行。**

1. 阻断项一：试验开启时，收藏缺失/失效会拒绝本来可用的旧推荐，已经隔离复现。需要完成准入前一次性路由及其过期守卫验证后再审。
2. 阻断项二：首次三个短超时尚未最终归因。本轮缩小到了旧同步偏好初始化及锁等待，但缺少首次工作线程栈，不能据通过对照排除全部时序风险；该项暂不签署无回归。

即便将来第二项证实为既有测试/环境问题，第一项仍独立阻断。上一轮“具备申请试用准备条件”保留为历史结论，以本次Gate Review的**未通过**裁定为准。

开关继续默认关闭，没有提交、推送、CI、打包或实际语音试用。用户本轮无需进行麦克风测试。完成审查后停止，不自动实施下一阶段。
