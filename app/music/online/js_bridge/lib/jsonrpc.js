// NDJSON stdio RPC: reads lines, dispatches to handler, writes response.
function startRpc(handler) {
  const readline = require('readline');
  const rl = readline.createInterface({ input: process.stdin, terminal: false });

  function send(msg) {
    process.stdout.write(JSON.stringify(msg) + '\n');
  }

  rl.on('line', async (line) => {
    line = line.trim();
    if (!line) return;
    let req;
    try { req = JSON.parse(line); }
    catch (e) { return; }
    if (!req || typeof req !== 'object' || Array.isArray(req) || typeof req.id !== 'string') return;
    const id = req.id;
    try {
      const result = await handler(req.method, req.params || {});
      send({ id, ok: true, result });
    } catch (e) {
      send({ id, ok: false, error: (e && e.message) || String(e) });
    }
  });
}

module.exports = { startRpc };
