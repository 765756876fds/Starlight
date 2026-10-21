"""豆瓣榜单数据模块（批 2：电影榜单）。

豆瓣网页版接口（movie.douban.com/j/）——之前 api.douban.com/v2 403 是旧接口反爬，
换 j/ 域带 UA 即 200（取证确认 10-17）。海报 CDN（img*.doubanio.com）防盗链：
必须带 Referer（裸连/只带 UA 均 HTTP 418，取证确认 10-17）。

降级契约：所有失败返回 []，绝不抛——UI 侧提示"榜单加载失败"，不崩。
"""
import requests

BASE = "https://movie.douban.com/j/"
TIMEOUT = 8.0
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://movie.douban.com/"}


def get_tags(timeout=TIMEOUT):
    """豆瓣标签列表（热门/最新/豆瓣高分/冷门佳片/华语/欧美/韩国/日本）。失败 → []。"""
    try:
        r = requests.get(BASE + "search_tags",
                         params={"type": "movie", "source": "index"},
                         headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
        return list(data.get("tags") or [])
    except Exception:
        return []


def get_subjects(tag, page_start=0, page_limit=20, timeout=TIMEOUT):
    """按标签拉榜单条目（title/rate/cover/url/id）。失败 → []。"""
    try:
        r = requests.get(BASE + "search_subjects",
                         params={"type": "movie", "tag": str(tag),
                                 "sort": "recommend",
                                 "page_limit": int(page_limit),
                                 "page_start": int(page_start)},
                         headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
        out = []
        for s in data.get("subjects") or []:
            if not isinstance(s, dict):
                continue
            out.append({
                "title": str(s.get("title") or ""),
                "rate": str(s.get("rate") or ""),
                "cover": str(s.get("cover") or ""),
                "url": str(s.get("url") or ""),
                "id": str(s.get("id") or ""),
            })
        return out
    except Exception:
        return []
