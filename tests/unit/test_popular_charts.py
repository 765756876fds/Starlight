from app.music.online.models import OnlineTrack
from app.music.online.popular_charts import ChartEntry, choose_chart, group_charts, read_chart
from types import SimpleNamespace


def song(source, title="海边", artist="甲", **extra):
    return OnlineTrack(source, source + title, title, artist, "专辑", 181, extra=extra)


def test_groups_are_stable_and_preserve_versions_and_original_entries():
    a, b = song("A"), song("B")
    versions = [song("B", "海边 Live"), song("B", "海边 Remix"), song("B", "海边 伴奏"),
                song("B", artist="乙"), song("A", _raw={"version": "翻唱"}), song("B", artist="")]
    entries = [ChartEntry(t, t.source_name, "123", "热歌榜", i + 1)
               for i, t in enumerate([a, b, *versions])]
    groups = group_charts([entries[:1], entries[1:]])
    assert len(groups) == 7
    assert groups[0].entries[0].track is a and groups[0].entries[1].track is b
    assert groups == group_charts([entries[:1], entries[1:]])
    assert choose_chart([{"title":"新歌", "lists":[{"id":"1", "title":"热歌榜"}]}]) is None


def test_bounded_first_page_does_not_mix_catalogue_or_playable_identities():
    rows = [song("unrelated")] + [song("A", title=str(i)) for i in range(20)]
    calls = []
    def detail(source, board, *, limit):
        calls.append((source, board, limit))
        return {"tracks": rows}

    catalog = {"A": [{"lists": [{"id": "history", "title": "年度热歌榜"},
                                 {"id": "chart", "title": "流行榜"}]}]}
    music = SimpleNamespace(get_top_lists=lambda source: catalog, get_top_list_detail=detail)
    assert read_chart(music, "A", lambda: False) == [] and not calls
    entries = read_chart(music, "A", lambda: True)
    assert calls == [("A", "chart", 12)]
    assert [e.rank for e in entries] == list(range(2, 13))
    assert [e.track for e in entries] == rows[1:12]
