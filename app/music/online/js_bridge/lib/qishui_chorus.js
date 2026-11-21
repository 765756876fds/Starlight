// Observe only the two responses already read by the Qishui plugin for this call.
// Nothing here resolves a URL, signs a request or reads account settings.
const { AsyncLocalStorage } = require('node:async_hooks');
const active = new AsyncLocalStorage();

function observe(state, url, data) {
  if (url.origin === 'https://beta-luna.douyin.com' && url.pathname === '/luna/h5/seo_track') {
    const t = data?.seo_track?.track;
    state.track = null;
    state.urls = [];
    if (typeof t?.id !== 'string' || t.id !== state.id || !t.vid || typeof t.vid !== 'string') return;
    state.status = 'unconfirmed';
    const range = t.playable_range;
    if (!Number.isSafeInteger(t.duration) || t.duration <= 0 || !range) return;
    if (range.start !== 0 || range.duration !== t.duration) {
      state.status = 'limited_range';
      return;
    }
    if (t.vid === t.preview?.vid || t.vid === t.audition_info?.vid) return;
    const clip = t.chorus;
    if (!Number.isSafeInteger(clip?.start) || clip.start < 0 || clip.start >= t.duration ||
        !Number.isSafeInteger(clip.duration) || clip.duration < 0 ||
        clip.start + clip.duration > t.duration) return;
    state.track = {id:t.id, media_id:t.vid, start_ms:clip.start,
      end_ms:clip.duration ? clip.start + clip.duration : null, duration_ms:t.duration};
  } else if (url.origin === 'https://vod-luna.douyin.com' && state.track &&
             url.searchParams.get('video_id') === state.track.media_id) {
    const info = data?.Result?.Data;
    if (info?.VideoID !== state.track.media_id || !Array.isArray(info.PlayInfoList)) return;
    state.urls = info.PlayInfoList.slice(0,32).map(row=>row?.MainPlayUrl)
      .filter(value=>typeof value === 'string' && value.startsWith('https://'));
  }
}

function scopedAxios(axios) {
  // Keep all methods and defaults of the installed dependency. Only GET responses
  // inside this plugin's getMediaSource promise have a collection context.
  const proxy = new Proxy(axios, {get(target,key) {
    if (key === 'default') return proxy;
    if (key !== 'get') return Reflect.get(target,key);
    return async (...args) => {
      const response = await target.get(...args);
      const state = active.getStore();
      if (state && response.status === 200) {
        try {
          const url = new URL(args[0]);
          const final = response.request?.res?.responseUrl;
          if (final && new URL(final).href === url.href)
            observe(state,url,response.data);
        } catch { /* Invalid metadata must never change normal playback. */ }
      }
      return response;
    };
  }});
  return proxy;
}

async function resolve(plugin,item,quality) {
  const state = {id:item.id, track:null, urls:[], status:'unconfirmed'};
  const result = await active.run(state,()=>plugin.getMediaSource(item,quality));
  if (!result?.url) return result;
  const recording = state.urls.includes(result.url) ? state.track : null;
  return {...result, playback_metadata: {branch:'qishui.play', status:recording?'matched':state.status,
    ...(recording || {})}};
}
module.exports = {scopedAxios, resolve};
