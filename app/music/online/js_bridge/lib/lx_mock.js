// Mock globalThis.lx for LX Music source plugins.
function createLxMock() {
  const handlers = new Map(); // eventName -> [handlers]
  const axios = require('axios');

  const lx = {
    on(eventName, handler) {
      if (!handlers.has(eventName)) handlers.set(eventName, []);
      handlers.get(eventName).push(handler);
    },
    off(eventName, handler) {
      const arr = handlers.get(eventName) || [];
      const i = arr.indexOf(handler);
      if (i >= 0) arr.splice(i, 1);
    },
    async send(eventName, data) {
      const arr = handlers.get(eventName) || [];
      return Promise.all(arr.map((h) => new Promise((resolve, reject) => {
        try {
          const r = h(data, resolve);
          if (r && typeof r.then === 'function') r.then(resolve, reject);
        } catch (e) { reject(e); }
      })));
    },
    request(url, options = {}, callback) {
      return (async () => {
        try {
          const resp = await axios({
            url,
            method: (options.method || 'GET').toUpperCase(),
            headers: options.headers || {},
            data: options.body || options.data,
            timeout: 8000,
            validateStatus: () => true,
            responseType: 'json',
          });
          const result = {
            statusCode: resp.status,
            headers: resp.headers,
            body: resp.data,
          };
          if (callback) callback(null, result);
          return result;
        } catch (e) {
          if (callback) callback(e, null);
          throw e;
        }
      })();
    },
    utils: {
      buffer: {
        from: (...a) => Buffer.from(...a),
        bufToString: (b) => Buffer.isBuffer(b) ? b.toString() : String(b),
      },
      crypto: require('crypto'),
      // LX provides a script loader for remote plugins; stub it.
      rawScript: async (src) => { throw new Error('rawScript not supported in bridge'); },
      script: async (src) => { throw new Error('script not supported in bridge'); },
      request: (...args) => lx.request(...args),
      string: { parseJson: (s) => JSON.parse(s), stringify: (o) => JSON.stringify(o) },
    },
    env: { getUserVariables: () => ({}), platform: 'pc' },
    version: '2.0.0',
    rawScript: async (src) => { throw new Error('rawScript not supported'); },
    script: async (src) => { throw new Error('script not supported'); },
    EVENT_NAMES: {
      request: 'request',
      inited: 'inited',
      updateAlert: 'updateAlert',
    },
    // Some LX sources call globalThis.lx.bridge
    bridge: {
      env: { getUserVariables: () => ({}) },
      version: '2.0.0',
    },
  };
  return lx;
}

module.exports = { createLxMock };
