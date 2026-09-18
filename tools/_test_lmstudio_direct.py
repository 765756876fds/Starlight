"""Direct Qwen3-4B API test via LM Studio on 1234, using production prompt format."""
import json, time
from openai import OpenAI

out = open('tools/_lmstudio_result.txt', 'w', encoding='utf-8')
def p(*args):
    out.write(' '.join(str(a) for a in args) + '\n')
    out.flush()

c = OpenAI(base_url='http://127.0.0.1:1234/v1', api_key='lm-studio', timeout=60)

SUPPORTED = {
    'music.next': '下一首/换一首',
    'music.previous': '上一首',
    'music.pause': '暂停播放',
    'music.resume': '继续播放',
    'music.volume_set': '设置音量，参数 volume (0-100)',
    'music.volume_up': '音量增加',
    'music.volume_down': '音量减少',
    'music.search_play': '搜索并播放，参数 query',
    'music.highlight': '跳到高潮部分',
    'alarm.flash': '记录闪现，参数 champion',
    'alarm.spell': '记录技能，参数 champion, spell, cooldown',
    'alarm.ward': '记录眼位，参数 location',
    'alarm.custom': '自定义计时器，参数 duration(秒), title',
    'voice.disable': '关闭语音识别',
    'tts.disable': '关闭语音播报',
    'system.unknown': '无法识别的意图',
}

cases = [
    '放首歌放松一下',
    '来点有节奏的音乐',
    '下一首',
    '想听点安静一点的',
]

for text in cases:
    intent_list = '\n'.join(f'- {k}: {v}' for k, v in SUPPORTED.items())
    sys_prompt = '/no_think\n你是一个专业的意图识别助手，只输出 JSON。'
    prompt = f'''将用户的自然语言转换为结构化意图。

支持的意图类型：
{intent_list}

用户输入："{text}"

请输出 JSON 格式，包含以下字段：
- intent: 意图类型（必须是上面列出的类型之一）
- confidence: 置信度 (0.0-1.0)
- params: 参数对象

只输出 JSON，不要输出其他内容。'''

    try:
        t0 = time.time()
        r = c.chat.completions.create(
            model='qwen3/qwen3-4b-k_m',
            messages=[
                {'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.1,
            max_tokens=256,
        )
        dt = time.time() - t0
        content = r.choices[0].message.content.strip()
        reasoning = getattr(r.choices[0].message, 'reasoning_content', None)
        usage = r.usage

        p(f'INPUT: {text}')
        p(f'  content: {content}')
        if reasoning:
            p(f'  reasoning: {reasoning[:200]}...')
        else:
            p(f'  reasoning: (none)')
        p(f'  latency: {dt:.2f}s, completion_tokens: {usage.completion_tokens}')
        # Validate JSON
        try:
            parsed = json.loads(content)
            intent = parsed.get('intent', '')
            whitelist_ok = intent in SUPPORTED
            p(f'  JSON valid: YES, intent={intent}, whitelist_ok={whitelist_ok}')
        except json.JSONDecodeError as e:
            p(f'  JSON valid: NO ({e})')
        p()
    except Exception as e:
        p(f'INPUT: {text} -> ERROR: {type(e).__name__}: {e}')
        p()

out.close()
