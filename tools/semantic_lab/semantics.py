"""Shared experiment contract, deterministic safety gate and rule baseline. No execution."""
import copy
import json
import re

from .corpus import ACTIONS


SLOT_TYPES = {key: "string" for key in
    ("query", "name", "new_name", "song_query", "style", "kind", "genre", "feedback", "mode")}
SLOT_TYPES.update({key: "number" for key in
    ("volume", "step", "seconds", "index", "episode", "speed", "min_score")})
SLOT_TYPES.update({key: "boolean" for key in ("change_chart", "change_style")})
SCHEMA = {"type": "object", "properties": {
    "domain": {"type": "string", "enum": ["music", "movie", "current_media", "none"]},
    "intent": {"type": "string", "enum": list(ACTIONS)},
    "target": {"type": "string", "enum": ["current_song", "current_movie", "current_media", "playlist", "chart", "candidate", "none"]},
    "slots": {"type": "object", "properties": {key: {"type": kind} for key, kind in SLOT_TYPES.items()},
              "additionalProperties": False},
    "polarity": {"type": "string", "enum": ["affirmative", "negative", "unclear"]},
    "speech_act": {"type": "string", "enum": ["command", "evaluation", "question", "reported", "chat"]},
    "implicit_action": {"type": "boolean"},
    "action_candidates": {"type": "array", "items": {"type": "string", "enum": list(ACTIONS)}, "maxItems": 3},
    "evidence": {"type": "string", "maxLength": 48}},
    "required": ["domain", "intent", "target", "slots", "polarity", "speech_act",
                 "implicit_action", "action_candidates", "evidence"], "additionalProperties": False}


def prediction(action="none", slots=None, **extra):
    domain = action.split(".")[0]
    target = "current_song" if domain == "music" else "current_movie" if domain == "movie" else "none"
    if "playlist" in action:
        target = "playlist"
    if action == "movie.recommend":
        target = "candidate"
    if action == "music.recommend":
        target = "chart"
    result = {"domain": domain if domain in ("music", "movie") else "none", "intent": action,
        "target": target, "slots": dict(slots or {}), "polarity": "affirmative", "speech_act": "command",
        "implicit_action": False, "action_candidates": [action] if action not in ("none", "clarify") else [],
        "evidence": "有限动作候选；未执行"}
    result.update(extra)
    return result


def valid(value):
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        return False
    for key in ("domain", "intent", "target", "polarity", "speech_act"):
        if value[key] not in SCHEMA["properties"][key]["enum"]:
            return False
    if type(value["implicit_action"]) is not bool or not isinstance(value["evidence"], str):
        return False
    if not isinstance(value["slots"], dict) or set(value["slots"]) - SLOT_TYPES.keys():
        return False
    for key, item in value["slots"].items():
        kind = SLOT_TYPES[key]
        if (kind == "string" and not isinstance(item, str) or kind == "boolean" and type(item) is not bool
                or kind == "number" and type(item) not in (int, float)):
            return False
    candidates = value["action_candidates"]
    return isinstance(candidates, list) and len(candidates) <= 3 and all(x in ACTIONS for x in candidates)


def resolve_context(action, scene):
    """Same live arbitration policy on synthetic snapshots, with no real IPC."""
    candidates = [d for d in ("music", "movie")
                  if scene[d] == ("paused" if action == "resume" else "playing")]
    if action == "next" and not candidates:
        candidates = [d for d in ("music", "movie") if scene[d] == "paused"]
    focus = scene.get("focus")
    chosen = focus if focus in candidates else None
    if not chosen and action == "resume" and not scene.get("pause_generation_stale"):
        last = scene.get("recent_pause")
        chosen = last if last in candidates else None
    if not chosen and len(candidates) == 1 and "unknown" not in (scene["music"], scene["movie"]):
        chosen = candidates[0]
    if not chosen or action == "next" and (chosen != "music" or scene["queue_count"] < 1):
        return "clarify"
    return chosen + "." + action


def language_guard(text):
    """Conservative fixed safety policy, frozen before model evaluation."""
    compact = re.sub(r"\s", "", text)
    if re.match(r"^(?:他说|她说|朋友|他认为|有人说|我在学习|我(?:刚才)?说的是|今早听朋友)", compact):
        return "reported"
    if re.match(r"^(?:不要|别换|先别|我没让|不许|不是让|不是叫|别再|别接|并不是|不用|别把)", compact):
        return "negative"
    if re.search(r"[?？]", compact) or re.match(r"^(?:为什么|怎么|你知道|如果说|是不是)", compact):
        return "question"
    if re.match(r'''^[“‘"']''', compact):
        return "reported"
    return None


def normalize_slots(slots):
    result = {}
    for key, value in slots.items():
        if value is None or value == "" or key in ("change_chart", "change_style") and value is False:
            continue
        if key in ("query", "song_query") and isinstance(value, str):
            value = re.sub(r"[《》‘’“”\s]", "", value)
            value = re.sub(r"^(?:歌曲|电影|影片)", "", value)
        result[key] = value
    return result


def gate(value, row):
    if not valid(value):
        return prediction("none", evidence="输出未通过白名单结构校验")
    value = copy.deepcopy(value)
    action, text, scene = value["intent"], row["text"], row["state"]
    guard = language_guard(text)
    if guard:
        return prediction("none", polarity="negative" if guard == "negative" else "unclear",
                          speech_act="command" if guard == "negative" else guard, evidence="共享安全门拒绝执行候选")
    if action not in ("none", "clarify") and (value["polarity"] != "affirmative"
            or value["speech_act"] not in ("command", "evaluation")
            or value["speech_act"] == "evaluation" and not value["implicit_action"]):
        return prediction("none", evidence="未提供肯定命令或明确隐含意图")
    if action in ("music.pause", "movie.pause", "music.resume", "movie.resume", "music.next"):
        verb = action.split(".")[1]
        explicit = bool(re.search(r"音乐|歌曲|首歌|这首|这歌|歌单|电影|影片|电视剧|这剧|片子", text))
        if not explicit:
            resolved = resolve_context(verb, scene)
            value["intent"] = resolved
            value["domain"] = resolved.split(".")[0] if "." in resolved else "none"
        else:
            domain = action.split(".")[0]
            need = "paused" if verb == "resume" else "playing"
            if scene[domain] != need or verb == "next" and scene["queue_count"] < 1:
                value = prediction("clarify", evidence="目标状态或播放集合不足")
    if value["intent"] in ("movie.next_episode", "movie.prev_episode") and scene["media_kind"] not in ("tv", "anime"):
        value = prediction("clarify", evidence="没有可靠连续剧集信息")
    if value["intent"] == "music.next" and scene["music"] not in ("playing", "paused"):
        value = prediction("clarify", evidence="没有可切换的音乐")
    value["slots"] = normalize_slots(value["slots"])
    return value


def rule_baseline(row):
    from app.voice.intent import IntentRouter
    intent = IntentRouter({}).parse(row["text"])
    action, slots = intent.intent_type, dict(intent.params)
    if action == "player.context":
        action = resolve_context(slots.get("action"), row["state"])
        slots = {}
    elif action == "music.recommend_next":
        action = "music.next" if row["state"]["recommendation_session"] and row["state"]["movie"] == "stopped" else "clarify"
    elif action == "player.next":
        action = resolve_context("next", row["state"])
    elif action == "music.pause" and intent.scope != "music":
        action = resolve_context("pause", row["state"])
    elif action == "music.resume" and intent.scope != "music":
        action = resolve_context("resume", row["state"])
    if action not in ACTIONS:
        action, slots = ("clarify" if action == "system.clarify" else "none"), {}
    return prediction(action, slots)


def score(value, row):
    gold = row["gold"]
    correct_action = value["intent"] == gold["intent"]
    correct_slots = normalize_slots(value["slots"]) == normalize_slots(gold["slots"])
    return {"action_correct": correct_action, "full_correct": correct_action and correct_slots,
        "speech_act_correct": value["speech_act"] == gold["speech_act"],
        "polarity_correct": value["polarity"] == gold["polarity"],
        "wrong_action": value["intent"] not in ("none", "clarify") and not correct_action,
        "false_execution_proposal": gold["intent"] in ("none", "clarify") and value["intent"] not in ("none", "clarify")}


def system_prompt():
    return ("你是星芒娱乐软件的语义解释器。只根据用户原话、虚拟软件状态、白名单输出一个JSON候选，不聊天，不执行。"
        "slots只放必要参数，不猜歌名和不存在的元数据。无关内容intent=none；缺参或状态冲突intent=clarify。"
        "polarity表示是否否定执行，不是情绪好坏。否定执行、问句、转述、引用不执行；明确播放作品名不把标题当命令。"
        "无对象暂停恢复按唯一状态、当前焦点、可靠recent_pause判断；否则clarify。音乐有队列才可换歌，电影换一个不能当音乐下一首。"
        "推荐音乐播放中明确不好听可候选music.next并slots.feedback=negative，speech_act=evaluation，implicit_action=true。"
        "一般般仅在weak_evaluation_means_skip=true时隐含切歌，否则clarify。声音有点大只是评价，none。"
        "影视候选浏览不满意可推荐新候选；电影播放中说不好看需clarify。明确音乐换感觉表示change_style=true。"
        "影视推荐只展示候选。高分默认阈值8，明确九分则9。name为歌单名，query为原始内容名，index从0开始。"
        "普通下一首不代表不喜欢，不自行加feedback。停止播放/别说了是system.stop_tts。"
        "清空队列不删除歌单；删除本地文件、笼统收藏、多个动作需clarify。后续执行权限不由你决定。"
        "白名单与说明：" + json.dumps(ACTIONS, ensure_ascii=False, separators=(",", ":")))


def messages(row):
    # Two fixed development examples, shared by all generation models. Never
    # construct examples from held-out rows or previous model predictions.
    from .corpus import state
    return [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": json.dumps({"text": "音乐暂停", "state": state()}, ensure_ascii=False)},
        {"role": "assistant", "content": json.dumps(prediction("music.pause", evidence="明确暂停正在播放的音乐"), ensure_ascii=False)},
        {"role": "user", "content": json.dumps({"text": "不要停", "state": state()}, ensure_ascii=False)},
        {"role": "assistant", "content": json.dumps(prediction("none", polarity="negative", evidence="否定暂停，不操作"), ensure_ascii=False)},
        {"role": "user", "content": json.dumps({"text": row["text"], "state": row["state"]}, ensure_ascii=False)},
    ]
