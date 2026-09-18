# JS Music Bridge

Node 子进程，加载 MusicFree / LX Music 音源 JS，通过 stdio NDJSON 与 Python 通信。

协议：每行一个 JSON。

请求：
```
{"id":"1","method":"search","params":{"source":"kg","keyword":"周杰伦","limit":20}}
{"id":"2","method":"getUrl","params":{"source":"kg","quality":"320k","raw_item":{...}}}
{"id":"3","method":"getLyric","params":{"source":"kg","raw_item":{...}}}
{"id":"4","method":"listSources","params":{}}
{"id":"5","method":"ping","params":{}}
```

响应：
```
{"id":"1","ok":true,"result":[...]}
{"id":"1","ok":false,"error":"..."}
```

启动时先发一行 `{"id":"_ready","ok":true,"sources":[...]}`。
