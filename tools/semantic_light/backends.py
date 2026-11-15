"""Text-only candidates: vector retrieval, feature-assisted retrieval and 0.8B."""

import copy
import json
import time

from tools.semantic_lab.embedding import embed

from .core import CONCEPTS, SCHEMA, lexical, semantic, valid


DESCRIPTION = {
    "pause": "短暂停住播放",
    "resume": "接续恢复播放",
    "stop": "结束当前播放",
    "change_content": "换掉当前内容",
    "change_style": "换一种风格感觉",
    "change_list": "改换榜单集合",
    "recommend": "想找内容或听看推荐",
    "search": "查找明确名字的内容",
    "positive_feedback": "表达喜欢与正面评价",
    "negative_feedback": "表达不喜欢难听难看",
    "neutral_feedback": "觉得一般或凑合",
    "save": "保存收藏留下当前内容",
    "continue_watch": "续看有记录的上次内容",
    "select_previous": "选刚才那个候选",
    "volume_up": "要求音量增大",
    "volume_down": "要求音量减小",
    "seek_forward": "向前跳进度",
    "seek_backward": "向后退进度",
    "speed_up": "提高播放速度",
    "speed_down": "降低播放速度",
}
INSTRUCTION = "Retrieve the basic meaning of a Chinese entertainment utterance, including speech act, negation, correction, evaluation and target reference. Do not infer player actions or parameters."


def prototypes(development):
    result = []
    for signal, description in DESCRIPTION.items():
        act = "evaluation" if signal.endswith("feedback") else "command"
        for text in [description, *CONCEPTS[signal][:2]]:
            result.append({"text": text, "semantic": semantic([signal], act)})
        for domain, prefix in [("music", "音乐"), ("movie", "电影")]:
            result.append(
                {"text": prefix + "：" + description, "semantic": semantic([signal], act, domain)}
            )
    for text, value in [
        ("这首满意，给我保存", semantic(["positive_feedback", "save"], target="music")),
        ("这个喜欢，帮我收藏", semantic(["positive_feedback", "save"], target="current_item")),
        ("别暂停", semantic(["pause"], polarity="negative")),
        ("别切换内容", semantic(["change_content"], polarity="negative")),
        ("为何要暂停", semantic(["pause"], "question")),
        ("有人说暂停", semantic(["pause"], "reported")),
        ("你吃饭了吗", semantic([], "chat", polarity="unclear")),
        ("唔那个啊", semantic([], "chat", polarity="unclear")),
    ]:
        result.append({"text": text, "semantic": value})
    result += [{"text": row["text"], "semantic": row["semantic"]} for row in development]
    return result


def feature_assist(value, text):
    """Disclosed local features; no state and no gold-answer access."""
    out = copy.deepcopy(value)
    features = lexical(text)
    if features["speech_act"] in ("question", "reported"):
        out["speech_act"] = features["speech_act"]
    if features["polarity"] in ("negative", "correction"):
        out["polarity"] = features["polarity"]
    if features["target_hint"] != "unspecified":
        out["target_hint"] = features["target_hint"]
    ratings = {"positive_feedback", "negative_feedback", "neutral_feedback"}
    if set(features["signals"]) & ratings:
        out["signals"] = sorted(
            (set(out["signals"]) - ratings) | (set(features["signals"]) & ratings)
        )
        out["speech_act"] = features["speech_act"]
    if "save" in features["signals"]:
        out["signals"] = sorted(set(out["signals"]) | {"save"})
    return out


class VectorIndex:
    def __init__(self, server, development):
        self.server = server
        self.entries = prototypes(development)
        self.vectors = []
        start = time.perf_counter()
        for offset in range(0, len(self.entries), 8):
            self.vectors.extend(
                embed(server, [p["text"] for p in self.entries[offset : offset + 8]])
            )
        self.index_seconds = time.perf_counter() - start

    def retrieve(self, text):
        (vector,) = embed(self.server, ["Instruct: " + INSTRUCTION + "\nQuery: " + text])
        scores = [sum(a * b for a, b in zip(vector, v)) for v in self.vectors]
        order = sorted(range(len(scores)), key=scores.__getitem__, reverse=True)
        distinct = []
        for i in order:
            if not any(
                self.entries[i]["semantic"] == self.entries[j]["semantic"] for j in distinct
            ):
                distinct.append(i)
            if len(distinct) == 3:
                break
        return copy.deepcopy(self.entries[distinct[0]]["semantic"]), {
            "scores": [scores[i] for i in distinct],
            "indices": distinct,
        }


def threshold(value, detail, minimum, margin):
    scores = detail["scores"]
    if scores[0] < minimum or len(scores) < 2 or scores[0] - scores[1] < margin:
        return semantic([], "unclear", polarity="unclear")
    return copy.deepcopy(value)


def prompt():
    return (
        "你只提取中文娱乐表达的基础语义，不操作软件、不输出业务API或参数。只输出四字段JSON。"
        "speech_act:command命令/evaluation评价/question问题/reported转述引用/chat无关含噪声/unclear不完整。"
        "polarity:affirmative肯定/negative否定执行/correction纠正后的肯定/unclear。负面评价不等于否定执行。"
        "target_hint:music音乐/movie影视/current_item这个或当前/previous_item刚才候选/speech播报/unspecified省略对象。"
        "signals仅包含本句话表达的概念，可组合喜欢+保存。否定/问句/转述仍提取被谈及概念。"
        "纠正句只保留最后肯定要求；书名号里是名字，不是控制动作。查找好看的属于推荐而非喜欢当前。"
        "声音有点大仅评价且signals=[]。非娱乐闲聊signals=[]。不猜名字、不考虑播放器状态。"
        "概念：" + json.dumps(DESCRIPTION, ensure_ascii=False, separators=(",", ":"))
    )


def development_examples():
    """Fixed from the original development partition only, never test feedback."""
    return [
        {"role": "user", "content": "音乐先暂停一会"},
        {
            "role": "assistant",
            "content": json.dumps(semantic(["pause"], target="music"), ensure_ascii=False),
        },
        {"role": "user", "content": "这首歌不好听"},
        {
            "role": "assistant",
            "content": json.dumps(
                semantic(["negative_feedback"], "evaluation", "music"), ensure_ascii=False
            ),
        },
    ]


def generate(server, text, timeout=30):
    response = server.request(
        "/v1/chat/completions",
        {
            "messages": [
                {"role": "system", "content": prompt()},
                *development_examples(),
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "seed": 42,
            "max_tokens": 96,
            "chat_template_kwargs": {"enable_thinking": False},
            "cache_prompt": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "basic_semantics", "strict": True, "schema": SCHEMA},
            },
        },
        timeout=timeout,
    )
    content = response["choices"][0]["message"]["content"]
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        value = None
    return value, {
        "content": content,
        "usage": response.get("usage"),
        "timings": response.get("timings"),
        "finish_reason": response["choices"][0].get("finish_reason"),
        "valid": valid(value),
    }
