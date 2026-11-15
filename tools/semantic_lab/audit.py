"""Reproducible source/call-chain inventory. Never constructs controllers or dispatches."""
import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path


SOURCES = ["app/core/command_bus.py", "app/voice/intent.py", "app/voice/runtime.py",
    "app/music/controller.py", "app/movie/movie_controller.py", "app/ui/panels/music_panel.py",
    "app/ui/widgets/music_discovery.py", "app/ui/panels/movie_panel.py", "app/ui/widgets/movie_home.py",
    "app/ui/widgets/music_queue_dialog.py", "app/ui/widgets/video_player_window.py",
    "app/movie/recommendation.py", "app/music/database.py"]


def catalog():
    rows = []
    def add(status, domain, title, text, command, path, method, tests, note=""):
        rows.append(dict(status=status,domain=domain,title=title,example=text,command=command,
            source=path,method=method,tests=tests,note=note))
    music = "app/music/controller.py"
    movie = "app/movie/movie_controller.py"
    voice = "app/voice/runtime.py"
    panel = "app/ui/panels/music_panel.py"
    basic = [
        ("本地开始播放","播放音乐","music.play","play","tests/unit/test_music_sleep_timer.py","本地库需有可播文件；不是智能推荐"),
        ("明确暂停音乐","音乐暂停","music.pause","pause","tests/unit/test_music_semantics_v1.py","按音乐领域，不抢电影"),
        ("明确恢复音乐","音乐继续","music.resume","resume","tests/unit/test_music_semantics_v1.py","保持位置；状态确认与业务结果分开"),
        ("播放暂停切换","播放暂停","music.toggle","toggle","tests/unit/test_music.py","当前音乐对象"),
        ("停止当前音乐","停止音乐","music.stop","stop_current","tests/unit/test_music_stop_semantics.py","保留临时队列，区别于暂停/停止播报"),
        ("下一首","下一首","music.next","next","tests/unit/test_music_next_play.py","当前队列；顺序/随机/单曲循环规则不变"),
        ("上一首","上一首","music.previous","prev","tests/unit/test_music_next_play.py","播放历史与队列边界"),
        ("按歌名歌手搜索播放","播放稻香","music.search_play","_cmd_search_play","tests/unit/test_voice_dispatch.py","本地优先/在线需要音源，不能仅据元数据报起播"),
        ("偏好/热门推荐并起播","我想听歌了","music.recommend","recommend_music","tests/unit/test_music_recommendation.py","本地记忆+真实榜单；有界失败；队列连续播放"),
        ("明确风格推荐","来点伤感的","music.recommend","recommend_music","tests/unit/test_music_recommendation.py","现有五种风格：伤感/轻松/摇滚/怀旧/动感"),
        ("重新选择榜单","换个榜单","music.recommend","recommend_music","tests/unit/test_recommendation_ensemble.py","change_chart；不把普通下一首当换榜"),
        ("重新选择风格","换个风格","music.recommend","recommend_music","tests/unit/test_music_recommendation.py","change_style；标签不足会诚实降级"),
        ("推荐集合下一首","换一个","music.recommend_next","_cmd_recommend_next","tests/unit/test_recommendation_queue_lifecycle.py","要求推荐会话且无电影活跃；不重选榜"),
        ("音乐音量数值","音乐音量五十","music.volume_set","_cmd_volume_set","tests/unit/test_voice_semantics.py","0-100；底层旧音量接口不完整回传IPC结果，不保证已听到改变"),
        ("音乐音量增加","声音大一点","music.volume_up","volume_up","tests/unit/test_voice_semantics.py","未指定时runtime默认5；数字写法音量加10可解析，中文步进见C类"),
        ("音乐音量降低","声音小一点","music.volume_down","volume_down","tests/unit/test_voice_semantics.py","不是跨媒体自动音量仲裁"),
        ("音乐静音","音乐静音","music.volume_set","_cmd_volume_set","tests/unit/test_voice_semantics.py","显式设置0，不同于恢复原音量的静音切换"),
        ("音乐相对进度","快进十秒","music.seek","_cmd_seek","tests/unit/test_voice_semantics.py","语音相对1-60秒；绝对进度有业务但未注册语音"),
        ("音乐倍速","倍速一点五倍","music.set_speed","set_speed","tests/unit/test_music_speed_voice.py","九档0.5/0.75/1/1.25/1.5/1.75/2/2.5/3；缺参追问"),
        ("顺序播放","顺序播放","music.set_play_mode","_cmd_set_play_mode","tests/unit/test_music_next_play.py","播完停止"),
        ("随机播放","随机播放","music.set_play_mode","_cmd_set_play_mode","tests/unit/test_music_next_play.py","保留插队与随机游标"),
        ("列表循环","列表循环","music.set_play_mode","_cmd_set_play_mode","tests/unit/test_music_next_play.py","repeat_all"),
        ("单曲循环","单曲循环","music.set_play_mode","_cmd_set_play_mode","tests/unit/test_music_next_play.py","repeat_one，保留用户下一首优先语义"),
        ("跳至高潮","跳到高潮","music.highlight","jump_to_highlight","tests/unit/test_voice_semantics.py","已有分析数据/能力才可用"),
        ("高潮模式开关","开启高潮模式","music.highlight_mode","_cmd_highlight_mode","tests/unit/test_voice_dispatch.py","开关不等于一次跳高潮"),
        ("播放用户歌单","播放通勤歌单","music.play_playlist","_cmd_play_playlist","tests/unit/test_music_playlist_controller.py","精确名称、唯一且非空；本地/在线歌曲既有路径"),
        ("新建用户歌单","新建歌单叫通勤","music.create_playlist","_cmd_create_playlist","tests/unit/test_music_playlist_voice.py","缺名称不执行；保存SQLite"),
        ("当前歌曲加入歌单","把这首歌加到通勤歌单","music.add_to_playlist","_cmd_add_to_playlist","tests/unit/test_music_playlist_controller.py","当前歌曲明确；目标歌单唯一"),
        ("指定本地歌加入歌单","把稻香加入通勤歌单","music.add_to_playlist","_cmd_add_to_playlist","tests/unit/test_music_playlist_controller.py","指定歌曲仅走本地搜索；无本地文件不会替你联网加歌"),
        ("指定歌曲下一首播放","把稻香放到下一首","music.add_next","_cmd_add_next","tests/unit/test_music_next_play.py","本地优先/在线兜底；连续D/E保持E→D"),
        ("当前榜单序号播放","播放当前榜单第三首","music.play_rank_index","_cmd_play_rank_index","tests/unit/test_music_playlist_voice.py","榜单须由UI加载set_voice_rank_tracks，index=2"),
        ("清空临时播放队列","清空队列","music.clear_queue","clear_queue","tests/unit/test_music_clear_queue.py","不删用户歌单/本地文件；现有“清空歌单”别名实际也走此路径"),
        ("音乐睡眠定时","睡眠定时三十分钟","music.sleep_timer_set","set_sleep_timer","tests/unit/test_music_sleep_timer.py","语音只接受30/60/90分钟；控制器支持0-24小时，未全部开放"),
        ("取消音乐定时","取消睡眠定时","music.sleep_timer_cancel","cancel_sleep_timer","tests/unit/test_music_sleep_timer.py","不停止当前歌"),
        ("更新本地音乐库","重新扫描","music.rescan","rescan","tests/unit/test_voice_dispatch.py","异步扫描既有目录，不猜目录路径"),
        ("清理失效音乐记录","清理失效歌曲","music.cleanup","cleanup_missing","tests/unit/test_voice_dispatch.py","影响数据库记录，应保持明确命令，不泛化为删文件"),
    ]
    for title,text,command,method,test,note in basic:
        add("A","音乐",title,text,command,music,method,[test],note)
    add("A","音乐","听歌/哼唱识曲入口","识曲","music.identify",panel,"_on_identify_cmd",
        ["tests/ui/test_identify_readiness.py"],"MusicPanel构造时注册；信号回GUI打开IdentifyDialog，仍需采音/指纹或在线识曲条件；不是直接知道歌名")
    for title,text,action in (("无对象暂停","停一下","pause"),("无对象恢复","继续播放","resume")):
        add("A","跨媒体",title,text,"player.context",voice,"_context_control",
            ["tests/unit/test_context_language.py","tests/unit/test_media_context.py"],f"action={action}；真实状态/焦点/可靠暂停对象/代次，冲突不猜测")
    add("A","跨媒体","裸暂停/继续兼容契约","暂停","music.pause",voice,"_global_pause",
        ["tests/unit/test_runtime_global_pause.py"],"全局暂停快照；裸继续只恢复确认过的对应代次，不能与自然省略对象混为一谈")
    add("A","系统","停止语音播报","停止播放","system.stop_tts",voice,"_stop_tts_now",
        ["tests/unit/test_voice_adversarial.py"],"最高安全优先级，非音乐/电影停止；不要停止播放不触发")
    films = [
        ("明确作品搜索播放","播放电影流浪地球","movie.search_play","_cmd_search_play","tests/unit/test_voice_movie.py","搜索Top1后请求播放并显示视频窗口；受片源/网络限制"),
        ("指定作品与集数","播放电影甄嬛传第五集","movie.search_play","_cmd_search_play","tests/unit/test_voice_movie.py","episode=5一开始集数，越界不播放"),
        ("电影候选推荐","我想看电影了","movie.recommend","_cmd_recommend","tests/unit/test_film_recommendation.py","展示候选，不自动播放；电影页面presenter须已接线"),
        ("电视剧候选推荐","我想看电视剧","movie.recommend","_cmd_recommend","tests/unit/test_film_recommendation.py","kind=tv"),
        ("动漫候选推荐","我想看动漫","movie.recommend","_cmd_recommend","tests/unit/test_film_recommendation.py","kind=anime；没有综艺语音推荐规则"),
        ("类型及评分过滤","给我找几部高分悬疑电影","movie.recommend","_cmd_recommend","tests/unit/test_film_recommendation.py","genre=悬疑/min_score=8；真实字段不足提示不足，不捏造高分"),
        ("继续观看","继续看上次的电视剧","movie.continue_watching","_cmd_recommend","tests/unit/test_film_recommendation.py","唯一可靠对象沿用续播；不唯一展示历史供选，三种kind可用"),
        ("影视暂停","电影暂停","movie.pause","pause","tests/unit/test_voice_movie.py","独立视频/内嵌和Qt窗口接线条件"),
        ("影视恢复","电影继续","movie.resume","resume","tests/unit/test_voice_movie.py","保留进度与窗口模式"),
        ("下一集","下一集","movie.next_episode","next_episode","tests/unit/test_voice_movie.py","当前剧集eps与索引有效；不是下一部推荐"),
        ("上一集","上一集","movie.prev_episode","prev_episode","tests/unit/test_voice_movie.py","不能越界"),
        ("停止影视","停止电影","movie.stop","stop","tests/unit/test_movie_resume.py","本地保存原生watch_later；失败日志不阻止关闭"),
        ("影视音量数值","电影音量五十","movie.volume_set","set_volume","tests/unit/test_voice_semantics.py","0-100；旧setter正常分支None，不能凭入队宣称IPC确认"),
        ("影视音量增加","电影声音大一点","movie.volume_up","volume_up","tests/unit/test_voice_semantics.py","依赖get_volume；不推测当前音乐音量"),
        ("影视音量降低","电影声音小一点","movie.volume_down","volume_down","tests/unit/test_voice_semantics.py","同上"),
        ("影视音量置零","电影静音","movie.volume_set","set_volume","tests/unit/test_voice_semantics.py","volume=0已接通；不是切换mute再恢复旧音量"),
        ("影视相对进度","电影快进十秒","movie.seek","seek","tests/unit/test_voice_semantics.py","明确电影域，相对1-60秒；不等于指定集数"),
    ]
    for title,text,command,method,test,note in films:
        add("A","影视",title,text,command,movie,method,[test],note)
    gaps = [
        ("追加队尾",music,"add_to_queue","tests/unit/test_music_queue_snapshot.py","已有右键入口；语义需解析歌曲，不打断播放"),
        ("移除待播条目",music,"remove_from_queue","tests/ui/test_music_queue_view.py","必须稳定条目ID/revision；不能按歌曲名误删重复歌曲"),
        ("队列排序",music,"move_queue_entry","tests/unit/test_queue_reorder.py","当前/优先项/随机保护，随机拖动禁用；不能让模型产出随意索引"),
        ("查看播放队列",panel,"_show_queue","tests/ui/test_music_queue_view.py","只读快照，不能调用get_next"),
        ("歌单重命名",music,"rename_playlist","tests/unit/test_playlist.py","与新建、清空临时队列不同；同名消歧与名称参数"),
        ("删除歌单记录",music,"delete_playlist","tests/unit/test_playlist.py","不删除媒体文件；建议二次确认"),
        ("从歌单移除指定歌曲",music,"remove_from_playlist","tests/unit/test_playlist.py","移除持久化关系，不是队列项或本地下载文件"),
        ("明确喜欢/减少推荐",music,"recommendation_feedback","tests/unit/test_music_recommendation.py","UI喜欢歌手/减少推荐歌曲；普通下一首不等于不喜欢"),
        ("在线发现/新歌/歌手专辑浏览",music,"browse_online","tests/ui/test_product_discovery.py","插件声明/真实返回决定支持项，未建立完整语音浏览会话"),
        ("音乐榜单目录浏览",music,"get_top_lists","tests/ui/test_music_ranks.py","只加载榜单与按已缓存序号播放是两回事"),
        ("歌词窗口",panel,"_on_toggle_lyrics","tests/ui/test_lyrics.py","已有界面/歌词搜索，尚无语音显示隐藏"),
        ("音乐绝对跳转",music,"seek","tests/unit/test_voice_semantics.py","mode=absolute业务已有，语音现有只相对秒数"),
        ("下载在线歌曲",music,"download_song","tests/unit/test_downloader.py","需合法音源与下载器，明确磁盘/版权边界后再接语音"),
        ("删除本地下载文件",music,"remove_download","tests/unit/test_downloader.py","会删除实际文件，不能与移出歌单/队列混淆"),
        ("音乐历史选择播放",music,"play_history_item","tests/ui/test_history_playlist_covers.py","有界面记录，没有可靠“播放第几个历史”语音上下文"),
        ("在线候选换源重播",music,"play_online_resume","tests/ui/test_candidate_bypass.py","已有候选代次与源兜底；尚无明确语音选择候选/音源入口"),
        ("手动标记高潮起点",music,"mark_highlight_start","tests/unit/test_mark_highlight.py","业务能记录当前起点；不能与跳高潮或高潮模式开关混为一谈"),
    ]
    for title,path,method,test,note in gaps:
        add("B","音乐",title,"","",path,method,[test],note)
    film_gaps = [
        ("推荐候选语音选第N个","app/ui/widgets/movie_home.py","_activate_recommendation","tests/ui/test_film_recommendation.py","现用鼠标选候选，不存在通用语音候选播放API"),
        ("影视明确喜欢/不感兴趣","app/movie/recommendation.py","feedback","tests/unit/test_film_recommendation.py","UI当前稳定work key；不能把关闭影片当不喜欢"),
        ("在线字幕查找与加载",movie,"load_subtitle","tests/unit/test_movie_subtitle_controls.py","已有Assrt搜索/加载；需要现有授权条件，不自动猜字幕版本"),
        ("本地SRT加载",movie,"load_local_subtitle","tests/unit/test_movie_subtitles.py","UI选择实际字幕文件；无语音文件选择和代次参数入口"),
        ("字幕轨道切换",movie,"select_subtitle","tests/unit/test_movie_subtitle_controls.py","需稳定轨道sid及媒体token，不能让模型瞎编轨道编号"),
        ("字幕显示隐藏",movie,"set_subtitle_visible","tests/unit/test_movie_subtitle_controls.py","已有业务与UI，未注册显示/隐藏语音命令"),
        ("全屏/退出全屏",movie,"set_fullscreen","tests/ui/test_movie_voice_window.py","独立窗口与MPV窗口模式需区分；无语音入口"),
        ("影视倍速",movie,"set_speed","tests/ui/test_movie_speed.py","UI可设倍速，没有电影倍速语音意图"),
        ("影视静音切换",movie,"toggle_mute","tests/ui/test_movie_fixes.py","仅UI；音量设0不等于恢复先前音量"),
        ("打开本地影片","app/ui/panels/movie_panel.py","_open_local_video","tests/ui/test_product_discovery.py","文件选择器不可让模型猜测私人路径"),
        ("官方观看入口","app/ui/widgets/movie_home.py","open_official","tests/ui/test_movie_card_interactions.py","合法官网链接，不能当作已有可播放地址"),
        ("影视绝对进度",movie,"seek","tests/unit/test_voice_semantics.py","支持absolute业务，现语音仅relative"),
        ("删除观看历史记录",movie,"delete_history","tests/unit/test_film_recommendation.py","持久化修改，语音未接通且应确认"),
        ("六分类及完整榜单浏览","app/ui/widgets/movie_home.py","show_category","tests/ui/test_movie_card_interactions.py","电影/电视剧/动漫/综艺/发现/本地影片UI既有；无语音导航"),
        ("影视榜单刷新","app/ui/widgets/movie_home.py","refresh_boards","tests/ui/test_discovery_reliability.py","有界异步与缓存；不把刷新成功当作可播放"),
    ]
    for title,path,method,test,note in film_gaps:
        add("B","影视",title,"","",path,method,[test],note)
    add("C","影视","“下一部电影”语义错位","下一部电影","movie.next_episode",movie,"next_episode",
        ["tests/unit/test_voice_movie.py"],"现有规则映射的是下一集，不是推荐另一部电影；不可宣称已具备换片推荐")
    add("C","音乐","任意睡眠时长尚未开放","睡眠定时二十分钟","music.sleep_timer_set",voice,"_execute_intent_body",
        ["tests/unit/test_music_sleep_timer.py"],"解析出1200，但runtime只放行1800/3600/5400；业务本身可到24小时")
    add("C","音乐","在线指定歌曲加入歌单","把未下载歌曲加入通勤歌单","music.add_to_playlist",music,"_cmd_add_to_playlist",
        ["tests/unit/test_music_playlist_controller.py"],"指定歌曲只查本地；UI可加在线歌但不能据此宣称该语音已支持在线查找加入")
    add("C","影视","影视独立收藏/稍后看","收藏这部电影","", "app/ui/widgets/movie_home.py","remember_selected",
        ["tests/ui/test_film_recommendation.py"],"已有明确偏好反馈，不等于独立持久收藏夹/稍后看列表；未发现完整收藏业务")
    add("C","音乐","中文音量步进未覆盖","音量加十","music.volume_up",voice,"_execute_intent_body",
        ["tests/unit/test_voice_semantics.py"],"当前完整解析返回unknown；音量加10可识别，不能假设中文数词已全面兼容")
    add("C","音乐","清空队列自然别名缺口","清空播放队列","music.clear_queue",music,"clear_queue",
        ["tests/unit/test_intent_music_clear_queue.py"],"当前返回unknown；清空队列可用。此次仅审计，不追加规则")
    add("C","音乐","独立收藏入口未确认","收藏这首歌","",music,"add_to_playlist",
        ["tests/unit/test_playlist.py"],"未发现独立favorite业务/命令；命名收藏的歌单或喜欢歌手偏好不能冒充完整收藏系统")
    add("C","跨媒体","音量/进度结果确认边界","","",music,"get_volume",
        ["tests/unit/test_voice_semantics.py"],"音量/seek部分旧handler返回None；存在执行链，但不能以CommandBus接收等同IPC成功；本轮不修生产")
    add("C","影视","综艺语音推荐缺口","我想看综艺","movie.recommend",movie,"_cmd_recommend",
        ["tests/unit/test_film_recommendation.py"],"UI真实综艺榜单可浏览，但现有film_request只注册电影/电视剧/动漫，不能宣称综艺推荐已接通")
    for domain,title,note in [
        ("音乐","展示队列/歌词与推荐理由","只读UI动作，低风险；需GUI线程桥"),
        ("音乐","明确喜欢/减少推荐与弱评价切歌","明确偏好先接；隐含评价仅候选，需用户决定自动执行范围，不每次都持久记为讨厌"),
        ("影视","稳定候选选择与倍速/全屏","先保存候选代次与ID，过期不执行；复用当前窗口"),
        ("跨媒体","统一音量/进度情境仲裁","先采用已验证真实媒体状态，不凭上句语言记忆"),
        ("跨媒体","当前曲目/推荐来源/播放进度只读查询","复用当前状态和推荐会话，低风险；定时剩余需先补准确状态接口"),
        ("音乐","来点类似的音乐","可以评估当前可验证歌手/标签的临时条件；没有完整相似歌曲索引，不冒充已有语音功能"),
    ]:
        add("D",domain,title,"","","","",[],note)
    for title,note in [
        ("模型自由调用API/执行脚本","禁止；候选只能来自审计白名单并经参数/状态/代次/权限校验"),
        ("删除真实文件/清理数据库/清空全部收藏","不可根据隐含评价执行；须独立明确授权和确认"),
        ("从评价自动永久写入负偏好","首次关闭或下一首不能代表讨厌；隐含反馈持久化需用户产品取舍"),
        ("付费播放/登录/绕过DRM","不建议；不把信息榜单当播放权限，不读取Cookie或账号"),
        ("LoL自动化扩展","本轮仅记录计时创建/查询/取消、提醒、玩家查询及已有开关；不启动hook、不升级游戏功能"),
    ]:
        add("E","边界",title,"","","","",[],note)
    return rows


def audit(root):
    from app.voice.intent import IntentRouter
    functions, bindings, hashes = {}, {}, {}
    for relative in SOURCES:
        path = root/relative
        raw = path.read_bytes()
        hashes[relative] = hashlib.sha256(raw).hexdigest()
        tree = ast.parse(raw)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[(relative,node.name)] = node.lineno
            if isinstance(node, ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=="register" and len(node.args)>=2:
                first=node.args[0]
                if isinstance(first,ast.Attribute) and isinstance(first.value,ast.Name) and first.value.id=="CommandType":
                    bindings.setdefault(first.attr,[]).append({"path":relative,"line":node.lineno,"handler":ast.unparse(node.args[1])})
    constants={}
    tree=ast.parse((root/"app/core/command_bus.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):
            constants[node.value.value]=node.targets[0].id
    rows=catalog()
    for index,row in enumerate(rows):
        row["id"]=f"cap-{index+1:03d}"
        row["line"]=functions.get((row["source"],row["method"]))
        row["registrations"]=bindings.get(constants.get(row["command"]),[])
        row["evidence_test_files_exist"]={p:(root/p).is_file() for p in row["tests"]}
        if row["example"]:
            intent=IntentRouter({}).parse(row["example"])
            row["parsed"]={"intent":intent.intent_type,"params":intent.params,"scope":intent.scope}
            row["parse_matches"]=intent.intent_type==row["command"]
        row["runtime_path"]="app/voice/runtime.py:_execute_intent_body" if row["command"] else "未注册语音执行链"
    return {"capabilities":rows,"source_sha256":hashes,"registrations":bindings,
            "counts":dict(Counter(r["status"] for r in rows))}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,default=Path.cwd())
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    result=audit(args.root)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Capability rows",len(result["capabilities"]),result["counts"])
    for row in result["capabilities"]:
        issues=[]
        if row["source"] and row["line"] is None:
            issues.append("method_not_found")
        if False in row["evidence_test_files_exist"].values():
            issues.append("test_path_not_found")
        if row["status"]=="A" and row.get("parse_matches") is False:
            issues.append("utterance_mismatch")
        if issues:
            print(row["id"],row["title"],issues,row.get("parsed"))


if __name__=="__main__":
    main()
