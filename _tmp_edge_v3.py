import asyncio, tempfile, os, sys

async def main():
    import edge_tts
    out = os.path.join(tempfile.gettempdir(), "test_edge.mp3")
    try:
        c = edge_tts.Communicate("测试语音播报", voice="zh-CN-XiaoxiaoNeural")
        await c.save(out)
        sys.stdout.write("SUCCESS size=%d\n" % os.path.getsize(out))
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write("FAIL %s %s\n" % (type(e).__name__, str(e)[:300]))
        sys.stdout.flush()

asyncio.run(main())
