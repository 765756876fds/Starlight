"""Composable lexical concepts and one shared, side-effect-free scene simulator."""

import re


CONCEPTS = {
    "pause": ("暂停", "停一下", "停一停", "停会", "停一会", "停片刻", "歇会", "歇一会", "缓一缓"),
    "resume": ("继续", "接着", "恢复", "接上", "续播"),
    "stop": ("停止", "关掉", "结束播放", "别放了"),
    "change_content": (
        "换歌",
        "换首",
        "下一首",
        "换一个",
        "换一首",
        "换个",
        "切歌",
        "跳过",
        "换掉",
    ),
    "change_style": ("风格", "感觉", "口味", "不一样", "换种", "换一种"),
    "change_list": ("榜单", "榜", "播放集合"),
    "recommend": (
        "推荐",
        "想听",
        "想看",
        "来点",
        "来些",
        "找个",
        "找点",
        "放点",
        "听点",
        "看点",
        "挑点",
        "挑个",
    ),
    "search": ("搜索", "查找", "搜一下", "搜一搜"),
    "positive_feedback": ("不错", "好听", "喜欢", "挺棒", "好看", "合胃口", "对味", "满意"),
    "negative_feedback": (
        "不好听",
        "难听",
        "没劲",
        "无聊",
        "不喜欢",
        "不合胃口",
        "不对味",
        "不好看",
        "不行",
        "听腻",
        "看腻",
        "没意思",
    ),
    "neutral_feedback": ("一般般", "一般", "还凑合", "马马虎虎", "就那样", "普普通通"),
    "save": ("留着", "保存", "收藏", "收起来", "存着", "存起来", "收下", "留下"),
    "continue_watch": ("昨天", "上次", "没看完", "上回", "断点", "续看"),
    "select_previous": ("刚才那个", "刚刚那个", "前一个", "上一项", "刚才选的", "先前那个"),
    "volume_up": ("大声", "响一点", "音量加", "声音大", "音量调高"),
    "volume_down": ("小声", "轻一点", "音量减", "声音小", "音量调低"),
    "seek_forward": ("快进", "往前跳", "向前跳"),
    "seek_backward": ("快退", "倒回", "往回跳", "倒退"),
    "speed_up": ("加速", "快点播", "倍速快"),
    "speed_down": ("减速", "慢点播", "倍速慢"),
}
SIGNALS = sorted(CONCEPTS)
ACTS = ["command", "evaluation", "question", "reported", "chat", "unclear"]
TARGETS = ["music", "movie", "current_item", "previous_item", "speech", "unspecified"]
POLARITIES = ["affirmative", "negative", "correction", "unclear"]
SCHEMA = {
    "type": "object",
    "properties": {
        "speech_act": {"type": "string", "enum": ACTS},
        "signals": {"type": "array", "items": {"type": "string", "enum": SIGNALS}, "maxItems": 4},
        "target_hint": {"type": "string", "enum": TARGETS},
        "polarity": {"type": "string", "enum": POLARITIES},
    },
    "required": ["speech_act", "signals", "target_hint", "polarity"],
    "additionalProperties": False,
}


def semantic(signals=(), act="command", target="unspecified", polarity="affirmative"):
    return {
        "speech_act": act,
        "signals": sorted(set(signals)),
        "target_hint": target,
        "polarity": polarity,
    }


def valid(value):
    return (
        isinstance(value, dict)
        and set(value) == set(SCHEMA["required"])
        and value["speech_act"] in ACTS
        and value["target_hint"] in TARGETS
        and value["polarity"] in POLARITIES
        and isinstance(value["signals"], list)
        and len(value["signals"]) <= 4
        and all(s in SIGNALS for s in value["signals"])
    )


def clause(text):
    """Remove only an explicitly rejected clause, not arbitrary negation words."""
    text = text.strip(" 。！!，,")
    correction = re.search(r"(?:不是|不是要|不是让你|并非).+?[，,](?:而是|是)(.+)$", text)
    if correction:
        return correction[1], True
    correction = re.search(r"(?:别|不要).+?[，,](.+)$", text)
    if correction and not re.search(r"别|不要|不能", correction[1]):
        return correction[1], True
    return text, False


def lexical(text):
    """Concept lexemes compose; no utterance-to-business-command lookup."""
    text, corrected = clause(text)
    quoted = bool(re.fullmatch(r'[“"‘\'].+[”"’\']', text))
    reported = quoted or bool(
        re.search(r"(?:他说|她说|朋友说|别人说|转述|引用|老师说|问的那句)", text)
    )
    question = bool(re.search(r"是不是|为什么|怎么|如何|什么意思|[吗么？?]", text))
    scan = re.sub(r"《[^》]+》", "", text)
    signals = {s for s, words in CONCEPTS.items() if any(w in scan for w in words)}
    for verb, signal in (("停", "pause"), ("换", "change_content")):
        if re.search(r"(?:不要|别|不许)" + verb + r"(?:了|吧)?$", scan):
            signals.add(signal)
    if "《" in text and re.search(r"播放|查找|搜索", scan):
        signals.add("search")
    if "negative_feedback" in signals:
        signals.discard("positive_feedback")
    if "neutral_feedback" in signals:
        signals.discard("positive_feedback")
    if "change_list" in signals and not re.search(r"换|切|另|别的", scan):
        signals.discard("change_list")
    if "change_style" in signals:
        signals.discard("change_content")
        signals.discard("recommend")
    if "change_list" in signals:
        signals.discard("change_content")
        signals.discard("recommend")
    if "continue_watch" in signals and re.search(r"接|继续|恢复|续|播放|看", scan):
        signals.discard("resume")
        signals.discard("recommend")
    else:
        signals.discard("continue_watch")
    if "select_previous" in signals:
        signals.discard("recommend")
    if "search" in signals:
        signals.discard("recommend")
    if signals & {"recommend", "search"}:
        signals -= {"positive_feedback", "negative_feedback", "neutral_feedback"}
    if any(
        s in signals
        for s in (
            "seek_forward",
            "seek_backward",
            "volume_up",
            "volume_down",
            "speed_up",
            "speed_down",
        )
    ):
        signals.discard("change_content")
    if "save" in signals:
        signals.discard("recommend")
    if scan in ("停止播放", "别说了", "停止播报"):
        signals = {"stop"}
        target = "speech"
    elif re.search(r"电影|影片|电视|动漫|番剧|这剧|片子", scan):
        target = "movie"
    elif re.search(r"音乐|歌曲|首歌|这首|这歌|歌单|榜单|换歌|切歌", scan):
        target = "music"
    elif "select_previous" in signals:
        target = "previous_item"
    elif re.search(r"这个|这一|当前|它|给我留|收起来|留着", scan):
        target = "current_item"
    else:
        target = "unspecified"
    negated = bool(
        re.search(r"不要|不许|别(?:再)?(?:停|换|切|播|收|存)|不是(?:让你|要)|并非让你|不用", scan)
    )
    if scan in ("别说了",):
        negated = False
    evaluations = {"positive_feedback", "negative_feedback", "neutral_feedback"}
    act = (
        "reported"
        if reported
        else "question"
        if question
        else "evaluation"
        if signals & evaluations and not signals - evaluations - {"change_content"}
        else "command"
        if signals
        else "chat"
    )
    polarity = (
        "correction"
        if corrected
        else "negative"
        if negated
        else "affirmative"
        if signals
        else "unclear"
    )
    return semantic(signals, act, target, polarity)


ACTIONS = {
    "music.pause",
    "music.resume",
    "music.stop",
    "music.next",
    "music.recommend",
    "music.recommendation_feedback",
    "music.create_playlist",
    "music.add_to_playlist",
    "movie.pause",
    "movie.resume",
    "movie.stop",
    "movie.recommend",
    "movie.search",
    "movie.continue_watching",
    "movie.select_candidate",
    "music.search",
    "system.stop_tts",
    *{
        f"{d}.{a}"
        for d in ("music", "movie")
        for a in ("volume_up", "volume_down", "seek", "set_speed")
    },
}


def decision(actions=(), params=None, weak=None, reason="policy", disposition=None):
    assert set(actions) <= ACTIONS
    return {
        "disposition": disposition or ("execute" if actions else "ignore"),
        "actions": list(actions),
        "params": params or {},
        "weak_feedback": weak,
        "reason": reason,
    }


def clarify(reason):
    return decision(reason=reason, disposition="clarify")


def target_for(hint, state, needed=None):
    if state["stale"]:
        return None
    if hint in ("music", "movie"):
        return hint if needed is None or state[hint] in needed else None
    eligible = [d for d in ("music", "movie") if state[d] in (needed or ("playing", "paused"))]
    if len(eligible) == 1:
        return eligible[0]
    if len(eligible) > 1:
        if needed == ("paused",) and state["recent_pause"] in eligible:
            return state["recent_pause"]
        if state["focus"] in eligible:
            return state["focus"]
    return None


def decide(value, state, text=""):
    """Identical function for A/B/C. No imports of production controllers or bus."""
    if not valid(value):
        return clarify("invalid_semantic")
    act, hint, signals = value["speech_act"], value["target_hint"], set(value["signals"])
    if act in ("question", "reported", "chat") or value["polarity"] == "negative":
        return decision(reason="not_an_affirmative_request")
    if not signals and act == "evaluation":
        return decision(reason="observation_without_action_signal")
    if not signals or value["polarity"] == "unclear":
        return clarify("uncertain_semantic")
    if state["stale"]:
        return clarify("stale_state")
    primaries = signals - {"positive_feedback", "negative_feedback", "neutral_feedback", "save"}
    compatible = {"change_style", "change_list"}
    if len(primaries) > 1 and not primaries <= compatible:
        return clarify("multiple_actions")
    if "save" in signals and primaries:
        return clarify("saving_and_an_independent_action")
    if "stop" in signals and hint == "speech":
        return decision(["system.stop_tts"])
    for s in ("pause", "resume", "stop"):
        if s in signals:
            needed = (
                ("paused",)
                if s == "resume"
                else ("playing",)
                if s == "pause"
                else ("playing", "paused")
            )
            domain = target_for(hint, state, needed)
            return decision([domain + "." + s]) if domain else clarify("no_unique_player")
    if signals & {"negative_feedback", "neutral_feedback"}:
        domain = target_for(hint, state, ("playing",))
        rating = "neutral" if "neutral_feedback" in signals else "negative"
        weak = (
            {
                "domain": domain,
                "rating": rating,
                "strength": "weak",
                "item_id": state.get(domain + "_id"),
            }
            if domain
            else None
        )
        if domain == "music":
            return (
                decision(["music.next"], weak=weak)
                if state["queue_count"]
                else clarify("empty_music_queue")
            )
        if domain == "movie":
            return decision(
                ["movie.stop", "movie.recommend"],
                {"kind": state["kind"], "preserve_progress": True},
                weak,
            )
        if state["page"] == "movie" and state["candidate_ids"]:
            return decision(["movie.recommend"], {"kind": state["kind"]})
        return clarify("feedback_target_missing")
    if signals & {"positive_feedback", "save"}:
        domain = target_for(hint, state)
        if domain != "music" or not state["music_id"]:
            return clarify("no_supported_save_target")
        actions = ["music.recommendation_feedback"] if "positive_feedback" in signals else []
        params = (
            {"feedback": "positive", "item_id": state["music_id"]}
            if actions
            else {"item_id": state["music_id"]}
        )
        if "save" in signals:
            params["name"] = "我喜欢的音乐"
            if not state["liked_playlist_exists"]:
                actions.append("music.create_playlist")
            actions.append("music.add_to_playlist")
        return decision(actions, params)
    if signals & {"change_style", "change_list"}:
        domain = target_for(hint, state)
        if domain != "music" or not state["recommendation_session"]:
            return clarify("not_in_music_recommendation")
        params = {"change_chart": True}
        if "change_style" in signals:
            params["change_style"] = True
        return decision(["music.recommend"], params)
    if "change_content" in signals:
        domain = target_for(hint, state)
        if domain == "music":
            return (
                decision(["music.next"]) if state["queue_count"] else clarify("empty_music_queue")
            )
        if domain == "movie":
            return decision(
                ["movie.stop", "movie.recommend"],
                {"kind": state["kind"], "preserve_progress": True},
            )
        return clarify("no_unique_content")
    if "continue_watch" in signals:
        rows = [
            r
            for r in state["history"]
            if r["reliable"] and r["unfinished"] and ("昨天" not in text or r["day"] == "yesterday")
        ]
        if len(rows) != 1:
            return clarify("resume_record_not_unique")
        actions = (["music.pause"] if state["music"] == "playing" else []) + [
            "movie.continue_watching"
        ]
        return decision(actions, {"history_id": rows[0]["id"], "kind": rows[0]["kind"]})
    if "select_previous" in signals:
        cid = state["previous_candidate_id"]
        if cid not in state["candidate_ids"] or cid not in state["playable_ids"]:
            return clarify("previous_candidate_expired_or_unplayable")
        actions = (["music.pause"] if state["music"] == "playing" else []) + [
            "movie.select_candidate"
        ]
        return decision(actions, {"candidate_id": cid})
    if signals & {"recommend", "search"}:
        domain = (
            hint
            if hint in ("music", "movie")
            else state["page"]
            if state["page"] in ("music", "movie")
            else None
        )
        if not domain:
            return clarify("discovery_domain_missing")
        if "search" in signals:
            match = re.search(r"《([^》]+)》", text)
            return (
                decision([domain + ".search"], {"query": match[1]})
                if match
                else clarify("literal_query_missing")
            )
        kind = (
            "tv"
            if "电视剧" in text
            else "anime"
            if "动漫" in text
            else "movie"
            if "电影" in text
            else state["preferred_kind"]
        )
        params = {"kind": kind} if domain == "movie" else {}
        return decision([domain + ".recommend"], params)
    for s in (
        "volume_up",
        "volume_down",
        "seek_forward",
        "seek_backward",
        "speed_up",
        "speed_down",
    ):
        if s not in signals:
            continue
        domain = target_for(hint, state)
        if not domain:
            return clarify("no_unique_experience_target")
        if s.startswith("volume"):
            return decision([domain + "." + s], {"step": 5})
        if s.startswith("seek"):
            match = re.search(r"(\d+|十|十五|二十|三十)秒", text)
            if not match:
                return clarify("seek_amount_missing")
            nums = {"十": 10, "十五": 15, "二十": 20, "三十": 30}
            seconds = nums[match[1]] if match[1] in nums else int(match[1])
            return decision(
                [domain + ".seek"],
                {"seconds": seconds if s == "seek_forward" else -seconds, "mode": "relative"},
            )
        return decision([domain + ".set_speed"], {"direction": "up" if s == "speed_up" else "down"})
    return clarify("unimplemented_signal")
