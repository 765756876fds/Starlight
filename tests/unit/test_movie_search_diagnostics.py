"""Only the extracted source identifier is part of this search candidate."""
from app.movie.playback_diagnostics import source_tag

def test_search_source_tag_correlates_hosts_without_exposing_private_url_parts():
    first=source_tag('https://synthetic.invalid/a?token=private-marker')
    second=source_tag('https://synthetic.invalid/b?token=different')
    assert first==second and len(first)==12
    assert all(ch in '0123456789abcdef' for ch in first)
    assert 'synthetic' not in first and 'private-marker' not in first
    assert source_tag(None)==source_tag('')=='unknown'
