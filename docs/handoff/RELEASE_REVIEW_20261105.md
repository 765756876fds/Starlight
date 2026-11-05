# 0.1.1.dev1 源码里程碑归档审查

本次审查基于 a0-cleanup、起点 a338d2718db4ba3068b87dbb8fef94071afd9273 的实际工作区。用户明确授权版本、文档、精确提交、正常推送、仓库介绍与Windows CI；不构建安装包，不改默认分支、可见性、权限或网络设置。

## 成果盘点

“纳入”指当前源码里程碑，不表示全部在本次新增，也不等于安装/实机验收。

| 成果 | 当前状态与入版方式 | 证据与边界 |
| --- | --- | --- |
| 日间/夜间主题 | 纳入本地增量；集中palette/token、ThemeManager、切换与记忆 | theme_palette/theme_switching；独立窗口、两主题/DPI有历史Qt证据，原生多屏仍需实机 |
| 统一通知中心/灵动岛 | 主体已在2fc87f4；仅纳入主题/反馈衔接增量 | notifications、notification_center/integrations；业务与播报独立，不重复提交主体 |
| 单实例WAKE | 已在a338d27，不重复提交 | single_instance真实Qt socket及此前Windows CI；保留分段与补读契约 |
| 多源音乐搜索 | 主体已提交；纳入发现目录、状态和换页衔接增量 | online_search_reliability、music_search_reliability；不把目录/网络失败说成空成功 |
| 队列V1/V2 | 主体已在a6c5a1f/e686786，不重复提交 | 追加/移除/指定下一首；随机自然接续及V1/V2有历史用户反馈 |
| 队列快照/安全编辑 | 纳入稳定条目标识与待播顺序编辑 | queue_snapshot/queue_reorder及Qt测试；当前/优先项保护，随机模式排序受限，E→D不变 |
| 音乐发现首页 | 纳入现有控制器与插件能力上的工作台 | product_discovery/media_homes；不伪造推荐、歌手/专辑或榜单数据 |
| 新歌错误反馈 | 纳入已完成修复 | catalog_feedback/catalog_errors；超时、插件、空、未知、不支持与部分结果区分，刷新不重复提交 |
| 电影分类与榜单 | 纳入六分类、有限预览、本地库/继续观看及公开榜单 | discovery/public_rank_sources/previews；B站/爱奇艺榜单、腾讯热搜；优酷/芒果仍仅官网 |
| 海报交互 | 纳入单击详情、双击一次搜索 | movie_card_interactions/movie_posters；榜单不自动播放，来源与播放权限区分 |
| SRT字幕 | 主体已在ce8ec8d；当前组合复核，不重复提交 | subtitle_controls/subtitles；GBK自动转码未实现，保留Assrt |
| 本地电影续播 | 主体已在6baaf01；纳入关闭自动连播仍记录历史的增量 | movie_resume、library及MPV集成历史；不新建续播数据库、不自动打开上次影片 |
| 海报URL/缓存 | 纳入可信CDN相同地址HTTPS升级、缓存/并发/缺图占位 | poster_urls/loader_reliability；172条地址丢失已复现修复，真实图片仍有超时/未测 |
| 音乐结束诊断 | 仅纳入脱敏日志与相关测试 | end_diagnostics、3项生成静音音频真实MPV；用户偶发停止历史根因仍未定，不宣称修好 |
| LoL别名/计时查询 | 纳入归一化取消、未知英雄拒绝、未到期REMINDED查询 | cooldown_cancel_and_list/voice_lol_execution；真实ASR、客户端与扬声器仍待场景验证 |
| Edge TTS可靠性 | 合成/超时/队列主体已在此前提交；纳入引擎设置同步与验证 | tts_reliability/tts_feedback/speech_queue；外部网络不保证，播报失败不反转业务结果 |
| 麦克风/识曲体验 | 纳入在线识曲无需先建本地库、临时文件隔离与真实反馈；恢复主体复核 | identify_readiness/voice_input_recovery/voice_activity；不把mock当采音/在线识别成功 |
| AGENTS与五Skills | 纳入项目规范与按需职责说明 | YAML/frontmatter、链接/路径与历史校验证据；不写死临时HEAD/数量为规范 |
| 测试/维护记录 | 纳入直接相关回归、产品路线记录和批次验收文档 | 历史与本次结果分开；首次失败、环境限制和未完成事项不删除 |

## 版本及文档

- 使用 `0.1.1.dev1`，唯一来源为根 `VERSION`；Python、构建清单和两安装器模板读取该文件。安装器数值资源由其派生，不另维护常量。
- README面向用户介绍入口、能力、源码准备与限制；CHANGELOG记录开发里程碑，未创建GitHub Release或下载地址。
- 旧私人配置中的app.version不覆盖产品版本，不重置配置；未升级任何运行依赖。安装模板仅作版本同步，本轮不编译安装器。

## 发布边界与排除范围

以下保留在本机，不进入提交：旧 `scripts/build_lite_installer.bat` 修改、数据库/备份、安装包/zip、artifacts审计片段，以及tools目录未完成实验、一次性CI脚本和旧审计材料。既有已提交文件不做删除或清理。

选择完整的已验证源码与测试，避免漏掉新模块；精确清单、原始快照、审阅diff、敏感字段检查和每次命令/退出码保留在本机独立的发布证据目录。日志、临时截图和实际插件不随仓库归档。

## 门槛与未完成项

- 本地候选树从HEAD加选定文件生成，排除未提交安装脚本；相关测试在隔离副本运行，不借用户数据或未提交依赖制造通过。
- 推送前检查暂存内容与私密数据、编译、JS语法及必要回归；Windows CI以最终SHA为准，当前工作流只编译并运行tests/unit。
- 音乐停止尚未定因、图片网络超时与腾讯电影榜当次失败、LCU401条件性恢复风险和历史混合Qt挂起继续记录，不为归档宣称根治。
- 源码开发里程碑满足审查与对应CI后可以归档；新冻结程序、干净Windows安装、真实麦克风/扬声器/客户端仍不能因此宣布通过。
- 实际提交SHA、远端核对和最终CI结果由本轮交付报告给出，不在准备阶段伪造通过状态。


## 本次实际验证及首败

- 在精确候选隔离树执行完整tests/unit：2593 passed、0 failed、9 skipped、1 warning、退出0；37项版本/打包验证已包含其中。
- Qt首轮408 passed/1 failed；识曲测试自建Track缺少真实OnlineTrack已有的album。仅改测试模型，保留过期结果、候选快照断言。补测曾在通用清理processEvents发生0xc0000374，首次堆栈和退出码保留；一次详细诊断同组合128 passed，未作无证据生产修改。最终覆盖的唯一Qt条目436个均有通过结果，不代表堆损坏已经解决。
- 新纳入的真实MPV集成4 passed、退出0：生成静音音频/视频、独立IPC/配置/数据库，验证EOF队列与原生检查点读取；没有读取私人媒体或验证扬声器。
- 跳过项为真实LoL/对局7项、实际插件文件1项、默认关闭的Edge联网1项；警告为既有asyncio标记。GUI采用offscreen、隔离设置和受控离线公共榜单，不称作真实联网。
- 稳定性边界：本次原生堆损坏尚未定因，源码开发版本可明确带风险归档，不能宣布稳定安装发行；Windows CI仍须读取实际最终SHA的结果。
