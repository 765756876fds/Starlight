"""Finite compositional media language, independent of players, SQLite and Qt.

An accepted meaning is a candidate, never permission to execute. Unknown words
cannot be discarded to turn an arbitrary sentence into a command.
"""

from dataclasses import asdict, dataclass
import re
import unicodedata


@dataclass(frozen=True)
class Meaning:
    action: str
    target: str = "current"
    rating: str = ""
    save: bool = False
    correction: bool = False
    feature: str = ""
    kind: str = ""
    value: float | None = None
    period: str = ""

    def to_dict(self):
        return asdict(self)


ACTIONS = {
    "pause",
    "resume",
    "stop",
    "next",
    "previous",
    "evaluate",
    "save",
    "style",
    "chart",
    "recommend",
    "continue_watch",
    "select",
    "queue",
    "lyrics",
    "fullscreen",
    "subtitle",
    "speed",
    "volume_up",
    "volume_down",
    "seek_forward",
    "seek_backward",
    "confirm",
}
WORDS = {
    "pause": (
        "暂停",
        "停片刻",
        "停一会儿",
        "停一会",
        "停一下",
        "停会儿",
        "停会",
        "停一停",
        "歇一会",
        "歇会",
        "缓一缓",
    ),
    "resume": (
        "继续播放",
        "恢复播放",
        "接着播放",
        "接着放",
        "接着播",
        "继续放",
        "继续播",
        "接上",
        "续播",
        "恢复",
        "继续",
    ),
    "stop": ("停止", "关掉", "停掉", "结束播放"),
    "next": ("下一首", "换一首", "换首", "换歌", "切歌", "换一个", "跳过", "换掉", "切走"),
    "previous": ("上一首", "前一首", "切回"),
    "style": ("风格", "感觉", "口味", "听感", "不一样", "不同的", "换一种", "换种"),
    "chart": ("榜单", "排行榜"),
    "recommend": (
        "推荐",
        "想听",
        "想看",
        "来点",
        "来些",
        "找个",
        "找点",
        "挑个",
        "挑点",
        "听点",
        "看点",
        "放点",
    ),
    "save": ("留着", "保存", "收藏", "收起来", "存起来", "留住", "存着", "收下", "留下"),
    "positive": (
        "不错",
        "好听",
        "喜欢",
        "挺棒",
        "真棒",
        "真赞",
        "合胃口",
        "对味",
        "中意",
        "带劲",
        "满意",
    ),
    "negative": (
        "不好听",
        "难听",
        "没劲",
        "无聊",
        "不喜欢",
        "不合胃口",
        "不对味",
        "不好看",
        "难看",
        "不行",
        "听腻",
        "看腻",
        "没意思",
        "拉胯",
        "太烂",
        "看不下去",
    ),
    "neutral": ("一般般", "一般", "还凑合", "马马虎虎", "就那样", "普普通通", "勉强能听", "还成"),
}
_NEGATION = r"不要|不准|不许|不必|不用|别|莫|不能|没让|不是(?:要|让)|并非|勿"
_REPORT = r"(?:他|她|别人|有人|朋友|老师|人家|同事|你)(?:刚才|之前)?(?:说|讲|问)|听说|转述|引用|(?:我(?:刚才|之前)?(?:说|问))"
_QUESTION = r"[?？]|为什么|为何|怎么|如何|是不是|是否|会不会|能不能|可不可以|好不好|能干嘛|什么意思|怎么样|会怎样|[吗么]|(?:了没|行不行|对不对|可以不)$"
_FOREIGN_SUBJECT = r"天气|午饭|晚饭|吃饭|味道|这道菜|这个菜|工作|股票|老板|作业|考试|同学|电脑配置"
_MEDIA_NOUN = r"音乐|歌曲|曲子|曲儿|这歌|这首|这一首|这曲|电影|影片|片子|这片|这个片|电视剧|动漫|番剧|影视|这剧"
_REFERENCE = r"这(?:个|一|首|部|段|曲)|当前|它|刚才|刚刚|先前|上次|上回|没看完|昨天"


def normalize(text):
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def correction_clause(text):
    """Only an explicit rejected action followed by a positive correction."""
    parts = re.split(r"[，,；;]", text)
    if len(parts) == 2 and re.search(r"不是|并非|没让", parts[0]):
        first = parts[0]
        if any(w in first for words in WORDS.values() for w in words):
            tail = re.sub(r"^(?:而是|是)(?:让你|要)?", "", parts[1].strip())
            if tail != parts[1].strip() and tail:
                return tail, True
    return text, False


def safety(text):
    """Return accepted clause, correction flag and a non-executable reason.

    Evaluate quotation/report/question BEFORE removing a rejected clause. A
    quoted correction is still reported speech, never a new command.
    """
    raw = normalize(text)
    if not raw or len(raw) > 160:
        return raw, False, "incomplete"
    outside = re.sub(r"《[^》]*》", "", raw)
    if re.search(r"[\"'“”‘’「」『』`]|" + _REPORT, outside):
        return raw, False, "reported"
    check = outside.rstrip("。.!！ ")
    if re.search(_QUESTION, check):
        return raw, False, "question"
    if re.search(_FOREIGN_SUBJECT, outside):
        return raw, False, "unrelated_subject"
    clause, corrected = correction_clause(raw)
    # A negative feeling belongs to evaluation, not to a negated action.
    denied = re.sub(r"不好听|不好看|不喜欢|不合胃口|不对味|不一样|不同的|看不下去", "评价", clause)
    if re.search(_NEGATION, denied):
        return clause, corrected, "negative_request"
    if "《" in raw or "》" in raw:
        return clause, corrected, "content_title"
    return clause, corrected, ""


def _target(text):
    movie = bool(re.search(r"电影|影片|片子|电视剧|动漫|番剧|影视|这片|这剧|这部", text))
    music = bool(
        re.search(r"音乐|歌曲|曲子|曲儿|这歌|这首|这一首|这曲|首歌|歌单|榜单|换歌|切歌", text)
    )
    return "conflict" if movie and music else "movie" if movie else "music" if music else "current"


def rejected_previous_action(text):
    """Explicit first-person correction; never permission to execute its tail."""
    match = re.fullmatch(r"我(?:不是让你|并非让你|没让你)(.+)", normalize(text).rstrip("。.!！ "))
    if match is None:
        return None
    meaning = extract(match[1])
    return meaning if meaning and meaning.action in ("next", "stop") else None


def extract(text):
    """Meaning/None. This function never inspects live state or old utterances."""
    clause, corrected, rejected = safety(text)
    if rejected:
        return None
    if re.fullmatch(r"对|没错|对就这样|就这样|对的", re.sub(r"[，,。!！\s]", "", clause)):
        return Meaning("confirm", feature="confirmation")
    if re.search(r"闪现|技能|计时|连招|队友|对面|英雄", clause):
        return None  # Existing game rules alone retain game execution authority.
    if re.match(r"播放(?:停止|暂停|恢复|继续)", clause):
        return None  # Reversed predicates are not an imperative control clause.
    found = {key for key, words in WORDS.items() if any(w in clause for w in words)}
    unrated = clause
    for term in sorted(WORDS["negative"], key=len, reverse=True):
        unrated = unrated.replace(term, "")
    independent_positive = any(w in unrated for w in WORDS["positive"])
    if ("negative" in found and ("neutral" in found or independent_positive)) or (
        "neutral" in found and independent_positive
    ):
        return None
    if "save" in found and found & {"negative", "neutral"}:
        return None
    target = _target(clause)
    if target == "conflict":
        return None
    # Unary concept precedence, not a lookup of entire user sentences.
    rating = (
        "negative"
        if "negative" in found
        else "neutral"
        if "neutral" in found
        else "positive"
        if "positive" in found
        else ""
    )
    if found & {"recommend"}:
        rating = ""  # “找个好听的” describes requested content, not the current song.
    primary = found & ACTIONS - {"save"}
    if "style" in primary:
        primary -= {"next", "recommend", "chart"}
    elif "chart" in primary and re.search(r"换|另|切|别的", clause):
        primary -= {"next", "recommend"}
    else:
        primary.discard("chart")
    if re.search(r"昨天|上次|上回|没看完|续看", clause) and re.search(
        r"继续|接着|恢复|续|播放|看", clause
    ):
        primary -= {"resume", "recommend"}
        primary.add("continue_watch")
    select = re.search(
        r"(?:第([一二两三四五六七八九十0-9]+)(?:个|部)|刚才那个|刚刚那个|先前那个)", clause
    )
    if select and not primary and not rating:
        primary.add("select")
    if re.search(r"(?:播放)?队列", clause) and re.search(r"查看|看看|显示|打开", clause):
        primary.add("queue")
    if "歌词" in clause and re.search(r"打开|显示|看看|关闭|隐藏", clause):
        primary.add("lyrics")
    if "字幕" in clause and re.search(r"打开|显示|关闭|隐藏", clause):
        primary.add("subtitle")
        target = "movie"
    if "全屏" in clause:
        primary.add("fullscreen")
        target = "movie"
    if "recommend" in primary and "好看" in clause:
        target = "movie"
    if re.search(r"倍速", clause):
        primary.add("speed")
    experience = (
        ("volume_up", r"(?:音量|声音).*(?:调高|增大|加)|大声(?:一)?点|响一点"),
        ("volume_down", r"(?:音量|声音).*(?:调低|减小|减)|小声(?:一)?点"),
        ("seek_forward", r"快进|往前跳"),
        ("seek_backward", r"倒回|快退|往回跳|倒退"),
    )
    for key, pattern in experience:
        if re.search(pattern, clause):
            primary = {key} if not primary else primary | {key}
    if len(primary) > 1 or ("save" in found and primary):
        return None
    # Evaluations must refer to media/current content, not arbitrary sentences
    # containing “不错”. Bare “一般/不错” stay a capability gap in V1.
    if rating or "save" in found:
        if not re.search(_MEDIA_NOUN + "|" + _REFERENCE, clause) and not re.fullmatch(
            r"(?:给我|帮我)?(?:留着|收起来|存起来|保存)", clause.rstrip("。.!！ ")
        ):
            return None
        if rating and primary == {"next"}:
            primary.clear()
        if primary:
            return None
        action = "evaluate" if rating else "save"
    else:
        action = next(iter(primary), "")
    if not action:
        return None
    if action in ("pause", "resume", "stop", "next", "previous"):
        tokens = "|".join(re.escape(w) for w in sorted(WORDS[action], key=len, reverse=True))
        if len(re.findall(tokens, clause)) > 1:
            return None
    # Reject unparsed factual/third-person subjects; a command can contain only
    # concept spans, media nouns and a finite set of grammatical modifiers.
    residue = clause
    concepts = sorted({w for words in WORDS.values() for w in words}, key=len, reverse=True)
    residue = re.sub("|".join(re.escape(w) for w in concepts), "", residue)
    residue = re.sub(
        r"播放队列|队列|歌词|字幕|全屏|倍速|快进|快退|倒回|倒退|往[前回]跳|音量|声音|调[高低]|大声|小声|响|秒|分钟",
        "",
        residue,
    )
    residue = re.sub(_MEDIA_NOUN + "|" + _REFERENCE + "|歌", "", residue)
    residue = re.sub(r"[0-9零一二两三四五六七八九十点.]+", "", residue)
    residue = re.sub(
        r"打开|显示|关闭|隐藏|退出|接着|续看|稍微|麻烦|咱们|咱|帮我|给我|把|让我|请|先|再|一下|一会儿|一会|会儿|会|一|点|首|部|个|来|放|播|看|听|换|另|切|第|的|了|着|起来|好|挺|真|太|有点|这个|叫|现在|也|就|呗|吧|嘛|啊|啦|呢|儿|[\s，,。!！;；]",
        "",
        residue,
    )
    if residue:
        return None
    if action == "select":
        value = float(select[1]) if select and select[1] and select[1].isdigit() else None
        if select and select[1] and value is None:
            from .numbers import parse_integer

            value = parse_integer(select[1])
    elif action in ("speed", "seek_forward", "seek_backward"):
        from .numbers import parse_number

        match = re.search(
            r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十]+(?:点[零一二两三四五六七八九]+)?)(?:倍|秒)",
            clause,
        )
        value = parse_number(match[1]) if match else None
    elif action in ("lyrics", "fullscreen", "subtitle"):
        value = 0 if re.search(r"关闭|隐藏|退出", clause) else 1
    else:
        value = None
    kind = (
        "tv"
        if "电视剧" in clause
        else "anime"
        if re.search(r"动漫|番剧", clause)
        else "movie"
        if "电影" in clause
        else ""
    )
    feature = ":".join(
        (
            action,
            target,
            rating,
            "save" if "save" in found else "",
            "corrected" if corrected else "",
        )
    )
    return Meaning(
        action,
        target,
        rating,
        "save" in found,
        corrected,
        feature,
        kind,
        value,
        "yesterday" if action == "continue_watch" and "昨天" in clause else "",
    )
