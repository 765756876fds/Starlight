"""Conservative ASR text normalization; never remove negations or title words."""
import re
import unicodedata


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    # Preserve negative numeric intent instead of erasing a dash as punctuation.
    text = re.sub(r"[−–—](?=\s*[0-9零〇一二两三四五六七八九十百])", "-", text)
    # Decimal punctuation is a parameter, not an ASR sentence separator.
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", "", text)
    text = re.sub(r"(?<![A-Za-z])'|'(?![A-Za-z])", "", text)
    text = re.sub(r"[，。！？；：、,;:!?\"()\[\]{}<>《》【】「」『』〈〉…—～·]", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff\d])\s+(?=[\u4e00-\u9fff\d])", "", text)
    # Only a small, anchored courtesy wrapper. Do not strip narration such as
    # “我刚才说/有人说”, or arbitrary material around a command.
    text = re.sub(r"^(?:请帮我|麻烦你|麻烦|请)(?=打开|开启|关闭|播放|暂停|继续|把|电影|音乐|游戏|音量)", "", text)
    text = re.sub(r"([QqWwEeRr])\s*大招", r"\1", text)
    trimmed = re.sub(r"[啊呢呀哦吧哈嘛噢呗啦哟诶]+$", "", text)
    return trimmed or text
