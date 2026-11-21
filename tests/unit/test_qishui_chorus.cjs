const {test} = require('node:test');
const assert = require('node:assert/strict');
const q = require('../../app/music/online/js_bridge/lib/qishui_chorus');
const origin = 'https://beta-luna.douyin.com/luna/h5/seo_track';
const movie = 'https://vod-luna.douyin.com/?video_id=recording1';
const media = 'https://media.invalid/music-A';
const item = {id:'7602088592302901291'};

function fixture(overrides = {}, echo = 'recording1', returned = media, final = true) {
  const track = {id:item.id,vid:'recording1', duration:256027, playable_range:{start:0,duration:256027},
    audition_info:{vid:'preview'},chorus:{start:50688,duration:0}, ...overrides};
  const http = q.scopedAxios({get:async url=>({status:200,request:{res:{responseUrl:final?url:'https://other.invalid/'}},
    data:url===origin ? {seo_track:{track}} : {Result:{Data:{VideoID:echo,PlayInfoList:[{MainPlayUrl:media}]}}}})});
  return {getMediaSource:async()=>{await http.get(origin);await http.get(movie);return {url:returned}}};
}
test('whole recording, point bound to the literal URL selected by plugin', async()=>{
  const r = await q.resolve(fixture(),item,'high');
  assert.equal(r.url,media);
  assert.deepEqual(r.playback_metadata,{branch:'qishui.play',status:'matched',id:item.id,media_id:'recording1',
    start_ms:50688,end_ms:null,duration_ms:256027});
});
test('partial range, redirect, mismatched track, audition video, different URL or echoed video',async()=>{
  for(const plug of [fixture({playable_range:{start:138624,duration:35136}}), fixture({id:'bad'}),
    fixture({vid:'preview'}),fixture({},'other'),fixture({},'recording1',media+'B'),fixture({},'recording1',media,false)]){
    const r=await q.resolve(plug,item,'standard');
    assert.notEqual(r.playback_metadata.status,'matched');
    assert.equal(r.playback_metadata.start_ms,undefined);
  }
});
test('overlapping calls do not combine two partial responses',async()=>{
  let signal;
  const wait=new Promise(done=>{signal=done});
  const http=q.scopedAxios({get:async url=>({status:200,request:{res:{responseUrl:url}},
    data:url===origin?{seo_track:{track:{id:item.id,vid:'recording1',duration:10000,playable_range:{start:0,duration:10000},chorus:{start:1500,duration:0}}}}:
    {Result:{Data:{VideoID:'recording1',PlayInfoList:[{MainPlayUrl:media}]}}}})});
  const first=q.resolve({getMediaSource:async()=>{await http.get(origin);await wait;return {url:media}}},item,'high');
  const second=q.resolve({getMediaSource:async()=>{await http.get(movie);signal();return {url:media}}},item,'high');
  for(const r of await Promise.all([first,second])) assert.equal(r.playback_metadata.status,'unconfirmed');
});
