---
name: starlight-media-engineering
description: 处理星芒 MPV、音乐播放队列、在线音乐与 MusicFree/LX 插件、电影本地库、字幕音轨、续播、影视榜单、缓存或异步媒体搜索时使用。复用已有媒体链路，不另建播放器、队列或音源体系。
---

# 星芒 Media Engineering

先遵守根目录 `AGENTS.md`，按实际问题读取以下入口；路径相对仓库根目录。

## 代码入口

- 播放与队列：`app/music/{mpv,queue,controller}.py`，`app/ui/widgets/music_queue_dialog.py`。
- 在线音源：`app/music/online/{manager,models,source}.py`、`app/music/online/sources/{js_source,http_source}.py`、`app/music/online/js_bridge/bridge.js`、`app/music/online/js_bridge/lib/mf_loader.js`。
- 音乐发现：`app/ui/widgets/music_discovery.py`、`app/ui/panels/music_panel.py`。
- 电影：`app/movie/{movie_controller,library,discovery,rank_sources}.py`、`app/ui/panels/movie_panel.py`、`app/ui/widgets/{movie_home,video_player_window}.py`。

## 不可破坏的语义

- “下一首播放”优先于随机选曲；连续指定 D、E 按 E → D 消费。快照不调用会推进游标的选曲方法。移除/排序以 entry ID 和 revision 为准，保护当前项、优先项、历史、随机状态及重复歌曲；随机模式不安全的拖动保持禁用。
- “加入播放队列”不等于“添加到歌单”，追加不打断播放。分页刷新替换相同页，不重复追加，也不按歌名消灭不同版本。
- MPV 明确成功才更新已确认状态；协议错误失败，无响应未知。保留现有调用契约，不重写 IPC。电影独立视频画面不能被“无黑窗”处理隐藏。
- 字幕复用 sub-add/轨道查询，避免自动发现与手动追加同一路径产生重复轨；无响应不能报加载成功，保留 Assrt 入口。
- 本地电影停止/退出前沿用原生 write-watch-later-config 与有界 IPC；失败不阻止退出，EOF、不同影片隔离和用户主动打开流程保持不变。不新建续播数据库。
- 插件能力以实际导出和真实返回为准。结构异常、请求失败和正常空数组分开，失败不缓存成空成功；多源保留部分成功，刷新失效前的旧请求不能回写新缓存。
- 影视榜单、信息、官方入口与播放片源分开；只接合法公开、可复现来源，不猜播放权限。未知年份/季数/地区不强合并同名作品；保留各平台榜名、排名与时间。
- 新分类复用服务和缓存，不修改用户实际插件或媒体。接口无分页时，限制 UI 数量不能宣传成减少服务端响应。

## 有针对性地验证

队列看 `tests/unit/{test_music_queue_snapshot,test_queue_reorder}.py` 和对应 UI；发现看 `tests/unit/{test_movie_discovery,test_public_rank_sources,test_discovery_reliability}.py` 与 `tests/ui/test_movie_card_interactions.py`；字幕/续播看 `tests/unit/{test_movie_subtitles,test_movie_subtitle_controls,test_movie_resume}.py`。

用隔离数据库、生成媒体和插件替身验证失败、未知、重试、旧结果。真实联网只做授权的只读请求，记录来源/分类/时间/数量；不自动播放、不上传私人录音。插件声明、导入、元数据读取、实际解码/播放分别报告。需要真实 MPV 时使用独立配置和 watch_later，避开用户进程。
