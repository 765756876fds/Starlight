"""Test OpenAI Python client against llama-server on 8081."""
import sys
from openai import OpenAI
import time

out = open('tools/_openai_result.txt', 'w', encoding='utf-8')
def p(*args):
    out.write(' '.join(str(a) for a in args) + '\n')
    out.flush()

c = OpenAI(base_url='http://127.0.0.1:8081/v1', api_key='lm-studio')
models = c.models.list()
p('models:', [m.id for m in models.data])

SUPPORTED = {
    'music.next': '下一首/换一首',
    'music.pause': '暂停播放',
    'music.search_play': '搜索并播放，参数 query',
    'music.highlight': '跳到高潮部分',
    'system.unknown': '无法识别的意图',
}

for text in ['放首歌放松一下', '下一首']:
    intent_list = '\n'.join(f'- {k}: {v}' for k, v in SUPPORTED.items())
    sys_prompt = '/no_think\n你是一个专业的意图识别助手，只输出 JSON。'
    prompt = f'''将用户的自然语言转换为结构化意图。

支持的意图类型：
{intent_list}

用户输入："{text}"

请输出 JSON 格式，包含：intent, confidence, params。
只输出 JSON，不要输出其他内容。'''

    t0 = time.time()
    r = c.chat.completions.create(
        model='default',
        messages=[
            {'role': 'system', 'content': sys_prompt},
            {'role': 'user', 'content': prompt},
        ],
        temperature=0.1,
        max_tokens=256,
    )
    dt = time.time() - t0
    p(f'INPUT: {text}')
    p(f'  -> {r.choices[0].message.content}  ({dt:.2f}s, tokens={r.usage.completion_tokens})')
    p()

out.close()
