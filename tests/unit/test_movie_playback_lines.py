"""No filesystem/network/MPV; CMS line identity and original data preservation."""
from copy import deepcopy

import pytest

from app.movie.cms_parser import normalise_playback, split_play_url, first_play_url, episode_eps


@pytest.mark.parametrize('index,expected', [(None,'https://a.invalid/one'), (0,'https://a.invalid/one'),
                                          (1,'https://b.invalid/one')])
def test_separate_lines_preserve_cms_source_raw_data_and_episode_order(index, expected):
    raw = dict(vod_id=7, _source='非凡资源', _source_id='opaque-a', vod_play_from='main$$$backup',
               vod_play_url='一$https://a.invalid/one#二$https://a.invalid/two$$$一$https://b.invalid/one#二$https://b.invalid/two')
    before = deepcopy(raw)
    item = normalise_playback(raw, index)
    assert item['first_url'] == expected and '$$$' not in item['play_url']
    assert item['vod_play_url'] == raw['vod_play_url'] and raw == before
    assert item['_source_id'] == 'opaque-a' and item['_source'] == '非凡资源'
    assert len(item['_playback_lines']) == 2 and len(episode_eps(item)) == 2
    assert normalise_playback(item) == item


def test_legacy_split_never_treats_another_line_as_part_of_an_episode_url():
    raw = '正片$https://a.invalid/one$$$正片$https://b.invalid/one'
    assert split_play_url(raw) == [('正片','https://a.invalid/one')]
    assert first_play_url(raw) == ('正片','https://a.invalid/one')


@pytest.mark.parametrize('raw', [{'first_url': 'https://a.invalid/movie'},
                                {'local_file':True, 'first_url':'D:/isolated/generated.wav'}])
def test_existing_direct_play_payload_is_unchanged(raw):
    assert normalise_playback(raw) == raw


@pytest.mark.parametrize('index', [-1,2,99])
def test_invalid_line_rejected_without_switching_source(index):
    with pytest.raises(ValueError):
        normalise_playback(dict(_source='A',vod_play_url='一$https://a.invalid/1'), index)


def test_mpv_diagnostic_redacts_address_but_preserves_loadfile_call(monkeypatch, capsys):
    from unittest.mock import Mock
    from app.music.mpv import MpvController
    mpv = MpvController('unused')
    send = Mock(return_value={'error':'success'})
    monkeypatch.setattr(mpv, '_send_command', send)
    address = 'https://private.invalid/movie?token=private-test-token'
    assert mpv.loadfile(address) == {'error':'success'}
    send.assert_called_once_with('loadfile', [address,'replace'], timeout=5.0)
    output = capsys.readouterr().out
    assert 'media_tag=' in output and 'private-test-token' not in output and address not in output
