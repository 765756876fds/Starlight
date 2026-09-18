"""Test LM Studio on 1234 with the project's actual config."""
import sys
from openai import OpenAI

out = open('tools/_lmstudio_result.txt', 'w', encoding='utf-8')
def p(*args):
    out.write(' '.join(str(a) for a in args) + '\n')
    out.flush()

# Try the exact config the project uses
c = OpenAI(base_url='http://127.0.0.1:1234/v1', api_key='lm-studio', timeout=15)

try:
    models = c.models.list()
    p('models:', [m.id for m in models.data])
except Exception as e:
    p('models list ERROR:', e)
    # Try without api_key
    try:
        c2 = OpenAI(base_url='http://127.0.0.1:1234/v1', api_key='not-needed', timeout=15)
        models = c2.models.list()
        p('models (no-key):', [m.id for m in models.data])
    except Exception as e2:
        p('models list ERROR2:', e2)

# Try chat
try:
    r = c.chat.completions.create(
        model='qwen3/qwen3-4b-k_m',
        messages=[{'role': 'user', 'content': 'hi'}],
        max_tokens=10,
    )
    p('chat OK:', r.choices[0].message.content)
except Exception as e:
    p('chat ERROR:', type(e).__name__, str(e)[:300])

out.close()
