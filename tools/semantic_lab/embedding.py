"""Real Qwen embeddings -> prototype retrieval -> explicit, reviewable slot rules."""
import json
import math
import re
import time

from .corpus import ACTIONS
from .semantics import prediction


INSTRUCTION = "Given a Chinese entertainment request and current software state, retrieve its safe semantic action. Distinguish commands, negation, quotation, questions and implicit requests."


def describe(scene):
    states = {"playing": "播放中", "paused": "已暂停", "stopped": "未播放", "unknown": "未知"}
    return (f"音乐{states[scene['music']]}，影视{states[scene['movie']]}，当前焦点{scene['focus']}，"
        f"音乐队列{scene['queue_count']}首，推荐会话{scene['recommendation_session']}，"
        f"当前页{scene['page']}，影视候选{scene['candidate_count']}部，"
        f"最近可靠暂停对象{scene['recent_pause']}，弱评价切歌习惯{scene['weak_evaluation_means_skip']}")


def query(row):
    return "Instruct: " + INSTRUCTION + "\nQuery: " + row["text"] + "\n软件状态：" + describe(row["state"])


def norm(vector):
    length = math.sqrt(sum(x*x for x in vector))
    if not length or not all(math.isfinite(x) for x in vector):
        raise ValueError("invalid model embedding")
    return [x/length for x in vector]


def embed(server, texts):
    result = server.request("/v1/embeddings", {"input": texts}, timeout=30)
    rows = sorted(result["data"], key=lambda r: r["index"])
    if len(rows) != len(texts):
        raise ValueError("embedding result count mismatch")
    return [norm(row["embedding"]) for row in rows]


def extract_slots(action, row):
    """Small disclosed extraction layer, not a hidden generative model."""
    from app.voice.intent import IntentRouter
    from app.voice.numbers import parse_number
    text, slots = row["text"].strip("。.!！"), {}
    old = IntentRouter({}).parse(text)
    if old.intent_type == action:
        slots.update(old.params)
    number = re.search(r"([0-9零一二两三四五六七八九十百]+)(?:秒|分钟|分|小时|首|集|分的)", text)
    value = parse_number(number[1]) if number else None
    if action in ("music.seek", "movie.seek") and value is not None:
        slots.update(seconds=-value if re.search(r"倒回|后退|倒退|倒带", text) else value, mode="relative")
    if action == "music.play_rank_index" and value is not None:
        slots["index"] = value - 1
    if action.endswith("volume_set"):
        found = re.search(r"(?:音量|设成|设为)(?:设成|设为)?([0-9零一二两三四五六七八九十百]+)", text)
        if found:
            slots["volume"] = parse_number(found[1])
    if action == "music.set_speed":
        found = re.search(r"([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十]+(?:点[零一二两三四五六七八九]+)?)倍", text)
        if found:
            slots["speed"] = parse_number(found[1])
    if action == "music.set_play_mode":
        for word, mode in (("单曲", "repeat_one"), ("随机", "shuffle"), ("列表循环", "repeat_all"), ("顺序", "sequential")):
            if word in text:
                slots["mode"] = mode
    if action == "music.sleep_timer_set" and "半小时" in text:
        slots["seconds"] = 1800
    if action == "music.recommend":
        for style, terms in {"伤感": ("伤感", "伤心", "失恋"), "轻松": ("轻松", "放松", "治愈"),
            "摇滚": ("摇滚",), "怀旧": ("怀旧", "老歌", "经典"), "动感": ("动感", "节奏", "舞曲")}.items():
            if any(term in text for term in terms):
                slots["style"] = style
        if "换" in text and "榜" in text:
            slots["change_chart"] = True
        if "换" in text and re.search(r"风格|感觉", text):
            slots["change_style"] = True
    if action in ("movie.recommend", "movie.continue_watching"):
        slots["kind"] = "anime" if "动漫" in text else "tv" if "电视剧" in text or "剧" in text else "movie"
        for genre in ("悬疑", "科幻", "喜剧", "动作", "爱情", "恐怖", "剧情", "动画", "纪录片"):
            if genre in text:
                slots["genre"] = genre
        if "高分" in text:
            slots["min_score"] = 8
        if "评分" in text and value is not None:
            slots["min_score"] = value
    if action in ("music.search_play", "movie.search_play", "music.add_next", "music.add_to_queue") and "query" not in slots:
        found = re.search(r"《(.+?)》", text)
        if found:
            slots["query"] = found[1]
        else:
            cleaned = re.sub(r"^(?:播放|放一首|放|我想听|听)(?:歌曲|电影)?", "", text)
            cleaned = re.sub(r"^(?:把|将)", "", cleaned)
            slots["query"] = re.split(r"(?:放到|安排在|加入|放在)", cleaned)[0]
    if "playlist" in action:
        if action == "music.create_playlist":
            found = re.search(r"(?:叫|叫做|名为)(.+)$", text)
            if found:
                slots["name"] = found[1]
        else:
            for name in row["state"]["playlists"]:
                if name in text:
                    slots["name"] = name
            found = re.search(r"改名为(.+)$", text)
            if action == "music.rename_playlist" and found:
                slots["new_name"] = found[1]
            if action in ("music.add_to_playlist", "music.remove_from_playlist"):
                found = re.search(r"(?:把|将)(.+?)(?:从|加入|加到|存进|存入)", text)
                if found and found[1] not in ("这首歌", "当前歌曲"):
                    slots["song_query"] = found[1]
    if action == "music.next" and re.search(r"不好听|不想听|听腻|不太想听|一般", text):
        slots["feedback"] = "negative"
    return slots


class EmbeddingAdapter:
    def __init__(self, server, development):
        self.server = server
        self.prototypes, texts = [], []
        for action, description in ACTIONS.items():
            for phrase in (description, "用户想要的操作是：" + description + "。不等于其他动作。"):
                self.prototypes.append(prediction(action))
                texts.append(phrase)
        for row in development:
            gold = row["gold"]
            self.prototypes.append(prediction(gold["intent"], polarity=gold["polarity"],
                speech_act=gold["speech_act"], implicit_action=gold["implicit_action"]))
            texts.append(row["text"] + "\n软件状态：" + describe(row["state"]))
        start = time.monotonic()
        self.vectors = []
        for index in range(0, len(texts), 8):
            self.vectors.extend(embed(server, texts[index:index+8]))
        self.index_seconds = time.monotonic() - start
        self.threshold, self.margin = .5, 0
        self.prototype_texts = texts

    def raw(self, row):
        vector, = embed(self.server, [query(row)])
        scores = [sum(a*b for a, b in zip(vector, doc)) for doc in self.vectors]
        order = sorted(range(len(scores)), key=scores.__getitem__, reverse=True)
        distinct = []
        for i in order:
            action = self.prototypes[i]["intent"]
            if all(self.prototypes[j]["intent"] != action for j in distinct):
                distinct.append(i)
            if len(distinct) == 3:
                break
        best = self.prototypes[distinct[0]]
        result = prediction(best["intent"], extract_slots(best["intent"], row),
            polarity=best["polarity"], speech_act=best["speech_act"], implicit_action=best["implicit_action"],
            evidence="真实last-token向量余弦检索，不是执行概率",
            action_candidates=[self.prototypes[i]["intent"] for i in distinct])
        return result, {"scores": [scores[i] for i in distinct], "prototype_indices": distinct}

    def filter(self, value, detail):
        if len(detail.get("scores", [])) < 2:
            return prediction("none", evidence="检索失败")
        if detail["scores"][0] < self.threshold or detail["scores"][0] - detail["scores"][1] < self.margin:
            return prediction("clarify", evidence="检索分数或候选差距不足")
        return value

    def freeze(self, path):
        path.write_text(json.dumps({"threshold": self.threshold, "margin": self.margin,
            "instruction": INSTRUCTION, "prototype_count": len(self.prototypes),
            "prototypes": self.prototype_texts, "index_seconds": self.index_seconds},
            ensure_ascii=False, indent=2), encoding="utf-8")
