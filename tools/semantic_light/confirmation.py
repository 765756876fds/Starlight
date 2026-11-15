"""Fresh confirmation wording after one development-only C prompt correction.

The initial 120 results remain untouched. No backend is tuned on these cases.
"""

import hashlib
import json
from pathlib import Path

from .core import decision, semantic
from .corpus import (
    CANDIDATES,
    FILM_PAUSED,
    HISTORY,
    MOVIE,
    MUSIC,
    NONE,
    PAUSED,
    ask,
    build,
    feedback,
    plan,
    positive,
    state,
)


def cases():
    rows = [dict(r) for r in build() if r["split"] == "dev"]

    def add(
        cat,
        text,
        sig,
        expected,
        scene=None,
        act="command",
        hint="unspecified",
        polarity="affirmative",
        implicit=False,
    ):
        index = sum(r["category"] == cat and r["split"] == "test" for r in rows)
        rows.append(
            {
                "id": f"confirm-{cat}-{index + 1:02}",
                "category": cat,
                "split": "test",
                "text": text,
                "state": dict(scene or MUSIC),
                "semantic": semantic(sig.split() if sig else (), act, hint, polarity),
                "gold": expected,
                "implicit": implicit,
            }
        )

    for text, sig, scene, domain, hint in [
        ("把歌先停片刻", "pause", MUSIC, "music", "music"),
        ("片子先歇一会吧", "pause", MOVIE, "movie", "movie"),
        ("现在音乐接上吧", "resume", PAUSED, "music", "music"),
        ("电影恢复着放", "resume", FILM_PAUSED, "movie", "movie"),
        ("咱先暂停播放呗", "pause", state(movie="playing", focus="movie"), "movie", "unspecified"),
        (
            "接着播起来",
            "resume",
            state(music="paused", movie="paused", focus="none", recent_pause="music"),
            "music",
            "unspecified",
        ),
        ("这就恢复播放", "resume", state(music="unknown", focus="none"), None, "unspecified"),
        ("先把音乐关掉吧", "stop", MUSIC, "music", "music"),
    ]:
        add("control", text, sig, plan(domain + "." + sig) if domain else ask(), scene, hint=hint)
    for i, (text, rating, hint) in enumerate(
        [
            ("这曲子太难听了", "negative", "music"),
            ("这歌忒没劲", "negative", "music"),
            ("这首勉强能听", "neutral", "music"),
            ("这歌有点拉胯", "negative", "music"),
            ("这个片太烂了", "negative", "movie"),
            ("这个剧情看不下去", "negative", "movie"),
            ("这首马马虎虎", "neutral", "music"),
            ("这个也一般", "neutral", "current_item"),
        ]
    ):
        scene = MOVIE if i in (4, 5) else CANDIDATES if i == 7 else MUSIC
        add(
            "evaluation",
            text,
            rating + "_feedback",
            plan("movie.recommend", kind="movie")
            if i == 7
            else feedback("movie" if i in (4, 5) else "music", rating),
            scene,
            act="evaluation",
            hint=hint,
            implicit=True,
        )
    for i, (text, sig, hint) in enumerate(
        [
            ("这曲儿挺合胃口", "positive_feedback", "music"),
            ("听着挺带劲，给我存起来", "positive_feedback save", "current_item"),
            ("这首真赞，帮我收下", "positive_feedback save", "music"),
            ("把这歌留住", "save", "music"),
            ("这一首不错", "positive_feedback", "current_item"),
            ("我挺中意这首，收藏吧", "positive_feedback save", "music"),
            ("这歌喜欢，保存好", "positive_feedback save", "music"),
            ("这首可以，给我留下", "positive_feedback save", "music"),
        ]
    ):
        expected = (
            plan("music.add_to_playlist", item_id="synthetic-track-1", name="我喜欢的音乐")
            if i == 3
            else positive("save" in sig)
        )
        add(
            "positive_save",
            text,
            sig,
            expected,
            act="evaluation" if i in (0, 4) else "command",
            hint=hint,
            implicit=True,
        )
    for text, sig, hint in [
        ("咱换个音乐口味吧", "change_style", "music"),
        ("另挑一张榜单", "change_list", "music"),
        ("来一首别的", "change_content", "music"),
        ("这段先跳过", "change_content", "current_item"),
        ("这首切走", "change_content", "music"),
        ("换一种听感", "change_style", "unspecified"),
        ("榜单和风格都换换", "change_list change_style", "music"),
        ("换一首别的吧", "change_content", "music"),
    ]:
        expected = (
            plan("music.next")
            if sig == "change_content"
            else plan(
                "music.recommend",
                **(
                    {"change_chart": True, "change_style": True}
                    if "change_style" in sig
                    else {"change_chart": True}
                ),
            )
        )
        add("change", text, sig, expected, hint=hint)
    for i, (text, sig, hint) in enumerate(
        [
            ("给我安排点音乐", "recommend", "music"),
            ("帮我挑个好看的片子", "recommend", "movie"),
            ("推荐些电影看看", "recommend", "movie"),
            ("想看一部番剧", "recommend", "movie"),
            ("给我搜一下《合成歌戊》", "search", "unspecified"),
            ("查找影片《合成片己》", "search", "movie"),
            ("挑点合适的看看", "recommend", "unspecified"),
            ("找个好看的东西", "recommend", "unspecified"),
        ]
    ):
        scene = (
            NONE
            if i == 7
            else state(music="stopped", page="movie", preferred_kind="anime" if i == 3 else "tv")
            if i in (1, 2, 3, 5, 6)
            else MUSIC
        )
        expected = (
            ask()
            if i == 7
            else plan("music.search", query="合成歌戊")
            if i == 4
            else plan("movie.search", query="合成片己")
            if i == 5
            else plan("movie.recommend", kind="movie" if i == 2 else "anime" if i == 3 else "tv")
            if i in (1, 2, 3, 6)
            else plan("music.recommend")
        )
        add("discovery", text, sig, expected, scene, hint=hint)
    for i, text in enumerate(
        [
            "昨天那集还没看完，接着看",
            "把上回的续上",
            "续看那部没看完的",
            "刚才那个就它了",
            "给我放先前那个",
            "就刚刚选的那部",
            "恢复昨天那部",
            "继续看没看完的",
        ]
    ):
        select = i in (3, 4, 5)
        scene = (
            dict(CANDIDATES) if select else state(music="stopped", page="movie", history=HISTORY)
        )
        if i == 2:
            scene["history"] = []
        if i == 3:
            scene["previous_candidate_id"] = "expired"
        if i == 4:
            scene["music"] = "playing"
        if i == 6:
            scene["history"] = [{**HISTORY[0], "reliable": False}]
        if i == 7:
            scene["history"] = HISTORY + [{**HISTORY[0], "id": "another-history"}]
        expected = (
            ask()
            if i in (2, 3, 6, 7)
            else plan(
                *((["music.pause"] if i == 4 else []) + ["movie.select_candidate"]),
                candidate_id="candidate-1",
            )
            if select
            else plan("movie.continue_watching", history_id="synthetic-tv-yesterday", kind="tv")
        )
        add(
            "history_selection",
            text,
            "select_previous" if select else "continue_watch",
            expected,
            scene,
            hint="previous_item" if select else "unspecified",
        )
    for i, (text, sig, hint) in enumerate(
        [
            ("音量再小一点", "volume_down", "unspecified"),
            ("影片大声一点", "volume_up", "movie"),
            ("把音乐往回跳20秒", "seek_backward", "music"),
            ("电影往前跳30秒", "seek_forward", "movie"),
            ("音乐慢点播吧", "speed_down", "music"),
            ("声音吵得慌", "", "unspecified"),
            ("让我听清，响一点", "volume_up", "unspecified"),
            ("稍微快进点", "seek_forward", "unspecified"),
        ]
    ):
        scene = MOVIE if i in (1, 3) else MUSIC
        expected = (
            plan("music.volume_down", step=5)
            if i == 0
            else plan("movie.volume_up", step=5)
            if i == 1
            else plan("music.seek", seconds=-20, mode="relative")
            if i == 2
            else plan("movie.seek", seconds=30, mode="relative")
            if i == 3
            else plan("music.set_speed", direction="down")
            if i == 4
            else decision()
            if i == 5
            else plan("music.volume_up", step=5)
            if i == 6
            else ask()
        )
        add(
            "experience",
            text,
            sig,
            expected,
            scene,
            act="evaluation" if i == 5 else "command",
            hint=hint,
        )
    for i, (text, sig, hint) in enumerate(
        [
            ("别再跳过这首啦", "change_content", "music"),
            ("不准暂停电影", "pause", "movie"),
            ("别给我换榜单", "change_list", "music"),
            ("我不是要暂停，是继续播", "resume", "unspecified"),
            ("不是收藏，是换首歌", "change_content", "music"),
            ("别恢复音乐", "resume", "music"),
            ("不是让你换风格，是换榜单", "change_list", "music"),
            ("不用关掉电影", "stop", "movie"),
        ]
    ):
        corrected = i in (3, 4, 6)
        scene = PAUSED if i in (3, 5) else MOVIE if i == 7 else MUSIC
        expected = (
            plan("music.resume")
            if i == 3
            else plan("music.next")
            if i == 4
            else plan("music.recommend", change_chart=True)
            if i == 6
            else decision()
        )
        add(
            "denial_correction",
            text,
            sig,
            expected,
            scene,
            hint=hint,
            polarity="correction" if corrected else "negative",
        )
    for i, (text, sig, hint) in enumerate(
        [
            ("朋友问我这首好不好听", "positive_feedback", "music"),
            ("她说这个难看", "negative_feedback", "current_item"),
            ("暂停会不会丢进度", "pause", "unspecified"),
            ("收藏这首能干嘛", "save", "music"),
            ("他说让你继续音乐", "resume", "music"),
            ("有人说换个榜单", "change_list", "music"),
            ("‘别给我换歌’", "change_content", "music"),
            ("音乐暂停了没", "pause", "music"),
        ]
    ):
        add(
            "question_report",
            text,
            sig,
            decision(),
            act="reported" if i in (0, 1, 4, 5, 6) else "question",
            hint=hint,
            polarity="negative" if i == 6 else "affirmative",
        )
    for i, text in enumerate(
        [
            "明天午饭吃啥",
            "这个菜味道不错",
            "今天工作挺没劲",
            "那个嗯就是",
            "接着那个啥",
            "音乐停鸦下",
            "不要不要了",
            "先暂停电影再保存歌曲",
        ]
    ):
        sig = "resume" if i == 4 else "pause save" if i == 7 else ""
        act = "unclear" if i in (4, 5, 6) else "command" if i == 7 else "chat"
        hint = "music" if i == 5 else "movie" if i == 7 else "unspecified"
        pol = "affirmative" if i == 7 else "unclear"
        add(
            "noise_ambiguity",
            text,
            sig,
            ask() if i in (4, 5, 6, 7) else decision(),
            act=act,
            hint=hint,
            polarity=pol,
        )
    return rows


def freeze(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cases.json"
    if path.exists():
        raise FileExistsError("preserve frozen confirmation corpus")
    path.write_text(json.dumps(cases(), ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    (directory / "frozen.sha256.json").write_text(
        json.dumps({"cases.json": hashlib.sha256(path.read_bytes()).hexdigest()}, indent=2) + "\n",
        encoding="utf8",
    )
