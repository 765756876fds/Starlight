"""Bounded music vocabulary; candidates never execute or retain media context."""
from dataclasses import dataclass
import re
import unicodedata

from ..core.command_bus import CommandType
from .text import normalize_text


MUSIC_ACTIONS = {
    "pause": CommandType.MUSIC_PAUSE,
    "resume": CommandType.MUSIC_RESUME,
    "next": CommandType.MUSIC_NEXT,
    "previous": CommandType.MUSIC_PREV,
}


@dataclass(frozen=True)
class SemanticIntent:
    action: str
    target: str  # music / current_media; never inferred from a previous utterance

    @property
    def command(self) -> str:
        return MUSIC_ACTIONS[self.action]


# Finite action/object compositions, not arbitrary substring matching.
_OBJECT = r"(?:音乐|歌曲|这首歌)"
_PAUSE = r"(?:先)?(?:暂停(?:一下)?|停一下|别放了)"
_RESUME = r"(?:接着|继续)(?:播放|放|听)"
_PATTERNS = (
    (rf"(?:把)?{_OBJECT}{_PAUSE}", "pause", "music"),
    (rf"{_PAUSE}{_OBJECT}", "pause", "music"),
    (rf"{_OBJECT}{_RESUME}", "resume", "music"),
    (rf"{_RESUME}{_OBJECT}", "resume", "music"),
    (r"(?:帮我)?(?:换|切)(?:一)?首歌", "next", "music"),
    (r"(?:音乐)?(?:切到|换到|跳到)下一首(?:歌)?", "next", "music"),
    (r"(?:音乐)?(?:切回|回到|换回|切到)上一首(?:歌)?", "previous", "music"),
    (rf"{_PAUSE}|{_RESUME}", "pause", "current_media"),
)


def unsafe_control_text(text: str) -> bool:
    """Keep questions/quotations visible before normalizing ASR punctuation."""
    return bool(re.search(r"[?\"'“”‘’「」『』《》〈〉【】()\[\]{}<>`]",
                          unicodedata.normalize("NFKC", text)))


def parse_music_candidate(text: str) -> SemanticIntent | None:
    if not text or len(text) > 64 or unsafe_control_text(text):
        return None
    normalized = normalize_text(text)
    # A separator inside an utterance is not permission to combine commands.
    if re.search(r"[，,；;。！!]", text.strip().rstrip("。.!！")):
        return None
    for pattern, action, target in _PATTERNS:
        if re.fullmatch(pattern, normalized):
            if target == "current_media" and re.fullmatch(_RESUME, normalized):
                action = "resume"
            return SemanticIntent(action, target)
    return None
