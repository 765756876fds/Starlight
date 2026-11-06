"""CMS 响应与分集地址解析；只处理输入数据，不访问外部资源。"""

import json


def parse_cms_response(text):
    """苹果 CMS 响应解析。

    已知坑：接口 Content-Type 常标 text/html 但 body 实际是合法 JSON
    （如 360zyzz 的 /api.php/provide/vod/...）。因此解析必须按 body 首字符
    判断（{ 或 [），不能按 Content-Type。非 JSON body 返回 {}。
    """
    s = (text or "").lstrip()
    if not s or s[0] not in "{[":
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def split_play_url(vod_play_url):
    """拆分苹果 CMS 的 vod_play_url。

    格式：名字$URL#名字2$URL2（# 分集、$ 分名与 URL）。
    返回 [(name, url), ...]；无有效段时返回 []。
    """
    out = []
    for seg in str(vod_play_url or "").split("#"):
        seg = seg.strip()
        if not seg or "$" not in seg:
            continue
        name, _, url = seg.partition("$")
        if not url.strip():
            continue
        out.append((name.strip(), url.strip()))
    return out


def first_play_url(vod_play_url):
    """取第一条 (name, url)；无则 None。"""
    parts = split_play_url(vod_play_url)
    return parts[0] if parts else None


def episode_eps(item):
    """取 item 的分集列表（非 dict / 无 play_url → 空列表）——episodes
    与 is_multi_episode 共用，消重复取数逻辑。"""
    if not isinstance(item, dict):
        return []
    return split_play_url(item.get("play_url") or "")
