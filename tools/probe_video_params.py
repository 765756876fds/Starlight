"""探针：打印运行中电影 mpv 的 video-params 完整 dict（w/h/aspect/par/dar 等）。

用法（软件播放电影时运行）：
    python tools/probe_video_params.py            # 自动扫 \\\\.\\pipe\\ 下含 movie 的 pipe
    python tools/probe_video_params.py <pipe名>   # 手动指定（如 \\\\.\\pipe\\lol_movie_mpv_1234）

连上的 pipe 会发 get_property video-params，打印返回 JSON。
也可加参数 --all 同时查 video-format / aspect 等附加属性。
"""
import json
import os
import sys
import time


def _query(pipe: str, props):
    try:
        f = open(pipe, "r+", buffering=1, encoding="utf-8")
    except Exception as e:
        return None, "open 失败: %s" % e
    try:
        out = {}
        for i, prop in enumerate(props):
            req = {"command": ["get_property", prop], "request_id": i + 1}
            f.write(json.dumps(req) + "\n")
            f.flush()
            deadline = time.time() + 3
            data = ""
            while time.time() < deadline:
                line = f.readline()
                if line:
                    data += line
                    if '"request_id":%d' % (i + 1) in data:
                        break
            out[prop] = data.strip()
        return out, None
    finally:
        f.close()


def main():
    props = ["video-params"]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--all" in sys.argv[1:]:
        props += ["video-format", "video-codec", "width", "height",
                  "video-aspect", "video-params/aspect", "video-params/par"]

    pipes = args if args else []
    if not pipes:
        try:
            names = os.listdir(r"\\.\pipe\\")
        except Exception as e:
            names = []
            print("枚举 pipe 失败: %s（请手动传 pipe 名）" % e)
        pipes = [r"\\.\pipe\\" + n for n in names
                 if "movie" in n.lower() or "mpv" in n.lower()]

    if not pipes:
        print("没找到 pipe。先播放一部电影，然后从日志里抄 pipe 名手动传入。")
        return

    for p in pipes:
        resp, err = _query(p, props)
        print("== %s ==" % p)
        if err:
            print("  (%s)" % err)
            continue
        for prop in props:
            print("  %s: %s" % (prop, resp.get(prop) or "(无响应)"))
        print()


if __name__ == "__main__":
    main()
