"""Public chart metadata only. Never resolve video streams or execute web scripts."""
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

from . import discovery


def image_url(value, domain):
    if not isinstance(value, str):
        return ""
    value = "https:" + value if value.startswith("//") else value
    if value.startswith("http://"):
        value = "https://" + value[7:]
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    return value if parsed.scheme == "https" and (host == domain or host.endswith("." + domain)) else ""


class _IqiyiPage(HTMLParser):
    """Read the SSR chart cards, not the executable Nuxt state or recommendations."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.card = None
        self.channel = None
        self.updated = ""
        self._meta = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "rtab__slider__link" in classes and attrs.get("aria-current") == "page":
            self.channel = attrs.get("data-rseat-v2", "").removeprefix("channel.")
        if tag == "span" and "erji__meta__txt" in classes:
            self._meta = True
        if tag == "a" and "rvi__box" in classes:
            self.card = {"url": attrs.get("href"), "rank": attrs.get("data-rseat-v2")}
        if self.card is not None:
            if "rvi__tit1" in classes:
                self.card["title"] = attrs.get("title", "").strip()
            if tag == "img" and "i71-img" in classes:
                self.card["cover_url"] = image_url(attrs.get("src"), "iqiyipic.com")

    def handle_endtag(self, tag):
        if tag == "span":
            self._meta = False
        if tag == "a" and self.card is not None:
            self.rows.append(self.card)
            self.card = None

    def handle_data(self, text):
        if self._meta:
            self.updated += text.strip()[:160]


class IqiyiRank:
    key, name = "iqiyi", "爱奇艺"
    CHANNELS = {"movie": "1", "tv": "2", "anime": "4", "variety": "6"}
    categories = tuple(CHANNELS)

    def __init__(self, fetch=None):
        self.fetch = fetch or (lambda url: discovery.fetch_public(url, text=True))

    def read(self, category):
        channel = self.CHANNELS[category]
        page = _IqiyiPage()
        content = self.fetch(f"https://www.iqiyi.com/ranks1/{channel}/0")
        if not isinstance(content, str):
            raise discovery.CatalogReadError("response")
        page.feed(content)
        if page.channel != channel:
            raise discovery.CatalogReadError("response")
        rows, seen = [], set()
        for card in page.rows[:100]:
            url = discovery.official_url(card.get("url"), self.key)
            rank = card.get("rank") or ""
            if not url or not card.get("title") or not rank.isdecimal() or not 1 <= int(rank) <= 100 or url in seen:
                continue
            seen.add(url)
            rows.append(dict(title=card["title"][:160], kind=category, year=None, region=None, season=None,
                             platform=self.key, platform_name=self.name, source_id=url, rank=int(rank), url=url,
                             cover_url=card.get("cover_url", ""), board="热度榜", source_updated=page.updated[:160]))
        if not rows:
            # Missing cards may mean an anti-bot/error page or changed markup.
            raise discovery.CatalogReadError("response")
        return rows


class TencentRank:
    key, name = "tencent", "腾讯视频"
    CHANNELS = {"movie": "1", "tv": "2", "anime": "3", "variety": "10"}
    categories = tuple(CHANNELS)
    ENDPOINT = "https://pbaccess.video.qq.com/trpc.tencentvideohotlistdata.hotlistdata.DoSearchHotListHttp/getSearchHotListHttp"

    def __init__(self, fetch=None):
        self.fetch = fetch or discovery.fetch_public

    def read(self, category):
        channel = self.CHANNELS[category]
        payload = self.fetch(self.ENDPOINT + "?" + urlencode(dict(ChannelID=channel, PageSize=50, PageNum=0, SceneID=2)))
        if not isinstance(payload, dict) or type(payload.get("ret")) is not int or payload["ret"] != 0:
            raise discovery.CatalogReadError("service_rejected")
        data = payload.get("data")
        if not isinstance(data, dict) or type(data.get("ErrCode")) is not int or data["ErrCode"] != 0:
            raise discovery.CatalogReadError("response")
        channels = data.get("Results")
        if not isinstance(channels, list):
            raise discovery.CatalogReadError("response")
        selected = [c for c in channels if isinstance(c, dict) and str(c.get("ChannelID")) == channel]
        if len(selected) != 1 or not isinstance(selected[0].get("Result"), dict):
            raise discovery.CatalogReadError("response")
        items = selected[0]["Result"].get("ListInfo")
        if not isinstance(items, list):
            raise discovery.CatalogReadError("response")
        rows = []
        for index, item in enumerate(items[:50]):
            if not isinstance(item, dict) or not isinstance(item.get("Query"), str) or not item["Query"].strip():
                continue
            # The official web page searches the Query; it does not promise a
            # playable cover from ID, which may denote an external work.
            title = item["Query"].strip()[:160]
            url = "https://v.qq.com/x/search/?" + urlencode(dict(q=title, stag=12))
            rows.append(dict(title=title, kind=category, year=None, region=None, season=None,
                             platform=self.key, platform_name=self.name, source_id=str(item.get("ID") or title),
                             rank=index + 1, url=url, url_kind="search", board="热搜榜",
                             cover_url=image_url(item.get("HzImgURL"), "qpic.cn")))
        if items and not rows:
            raise discovery.CatalogReadError("response")
        return rows
