"""电影搜索结果的保守内容分类。未知信息不得伪装成正片。"""
import re

LABEL_UNKNOWN = "未知"
LABEL_FEATURE = "正片/剧集"
LABEL_EXPLAINER = "电影解说"
LABEL_SHORT = "短剧"
LABEL_REMAKE = "翻拍短剧"


def classify_content(item: dict) -> str:
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
