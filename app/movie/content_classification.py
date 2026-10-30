"""电影搜索结果的保守内容分类。未知信息不得伪装成正片。"""
import re

LABEL_UNKNOWN = "未知"
LABEL_FEATURE = "正片/剧集"
LABEL_EXPLAINER = "电影解说"
LABEL_SHORT = "短剧"
LABEL_REMAKE = "翻拍短剧"
TAG_REMAKE = "翻拍"


def classify_content_info(item: dict) -> dict:
    """返回稳定的基础类别和独立标签，证据不足时保守归类。"""
    if not isinstance(item, dict):
        return {"category": LABEL_UNKNOWN, "tags": []}
    explicit = " ".join(str(item.get(k) or "") for k in
                         ("vod_class", "vod_type", "type", "category", "content_type")).lower()
    text = " ".join(str(item.get(k) or "") for k in
                     ("vod_name", "vod_remarks", "vod_blurb", "vod_content")).lower()
    evidence = f"{explicit} {text}"
    tags = [TAG_REMAKE] if re.search(r"翻拍|改编短剧|remake", evidence) else []
    if any(x in explicit or x in text for x in ("解说", "解读", "commentary")):
        category = LABEL_EXPLAINER
    elif any(x in explicit or x in text for x in ("短剧", "微短剧", "竖屏", "短片", "short")):
        category = LABEL_SHORT
    elif any(x in evidence for x in ("电影", "电视剧", "剧集", "movie", "series")):
        category = LABEL_FEATURE
    else:
        category = LABEL_UNKNOWN
    return {"category": category, "tags": tags}


def classify_content(item: dict) -> str:
    """兼容旧调用者的单值分类接口。"""
    if not isinstance(item, dict):
        return LABEL_UNKNOWN
    explicit = " ".join(str(item.get(k) or "") for k in
                         ("vod_class", "vod_type", "type", "category", "content_type")).lower()
    if explicit:
        if any(x in explicit for x in ("解说", "解读", "commentary")):
            return LABEL_EXPLAINER
        if any(x in explicit for x in ("翻拍", "改编短剧", "remake")):
            return LABEL_REMAKE
        if any(x in explicit for x in ("短剧", "短片", "short")):
            return LABEL_SHORT
        if any(x in explicit for x in ("电影", "电视剧", "剧集", "movie", "series")):
            return LABEL_FEATURE
    text = " ".join(str(item.get(k) or "") for k in ("vod_name", "vod_remarks")).lower()
    if re.search(r"翻拍|改编短剧|remake", text):
        return LABEL_REMAKE
    if re.search(r"电影解说|精彩解说|解说合集|解读", text):
        return LABEL_EXPLAINER
    if re.search(r"短剧|微短剧|短片", text):
        return LABEL_SHORT
    return LABEL_UNKNOWN
