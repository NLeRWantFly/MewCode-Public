"""Isolated visual fixture serving the real conversation statistics component."""
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "frontend/static"), name="static")


@app.get("/")
def page():
    html = (ROOT / "frontend/static/index.html").read_text(encoding="utf-8")
    html = re.sub(r'<script src="/static/app\.js\?[^"<>]+"></script>', '''<script>
      document.documentElement.dataset.theme = 'light';
      document.getElementById('messages').innerHTML = `<div style="padding:48px;max-width:700px;margin:auto"><h2>Jev 决策与模型用量</h2><p>此页为隔离验收数据，不连接真实模型或业务会话。</p><p>城市候选已完成结构化判断，主模型正在继续制定旅行计划。</p><button id="switch-fixture" onclick="window.dispatchEvent(new CustomEvent('mewcode:conversation-context',{detail:{conversationId:'empty',busy:false}}))">切换空对话</button></div>`;
      window.dispatchEvent(new CustomEvent('mewcode:conversation-context', {detail:{conversationId:'demo',busy:false}}));
    </script>''', html)
    return HTMLResponse(html)


requests = 0


@app.get("/api/chat/usage")
def usage(conversation_id: str):
    global requests
    requests += 1
    from mewcode.usage.models import TokenUsage
    records = [("Grok", "grok-4.7-build", 12 + requests, TokenUsage(34800, 16000, 0, 4200 + requests * 100, 1200)),
               ("TypeSafe", "jev-1.13.0", 8, TokenUsage(4800, 0, 0, 240)),
               ("Grok", "grok-4.6", 3, TokenUsage(10500, 2400, 0, 1800))]
    if conversation_id == "empty":
        records = []
    total = TokenUsage()
    models = []
    for provider, model, count, tokens in records:
        total = total.add(tokens)
        models.append({"provider": provider, "model": model, "request_count": count, "tokens": tokens.to_dict()})
    return {"conversation_id": conversation_id, "request_count": sum(r[2] for r in records),
            "tokens": total.to_dict(), "models": models, "unreported_token_requests": 0}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=50739)
