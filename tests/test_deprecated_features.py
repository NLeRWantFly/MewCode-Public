"""Guard removed features while preserving their historical conversation records."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from uuid import uuid4

import pytest

from mewcode.agent import Agent, ErrorEvent, StreamText
from mewcode.client import LLMClient
from mewcode.conversation import (
    ConversationManager, Message, ToolResultBlock, ToolUseBlock,
)
from mewcode.memory.session import SessionRecord, records_to_messages
from mewcode.prompts import build_system_prompt
from mewcode.serialization import (
    build_anthropic_messages, build_chat_completion_messages, build_openai_input,
)
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta

ROOT = Path(__file__).resolve().parents[1]
REMOVED = re.compile(r'TopicSpace|topic-space|topics_api|topic_context|TopicStore|议题与行动|当前对话中的议题和行动')


def test_removed_feature_has_no_code_or_test_references():
    matches = []
    for directory in ('frontend', 'mewcode', 'tests'):
        for path in (ROOT / directory).rglob('*'):
            if path == Path(__file__) or path.suffix not in {'.py', '.js', '.cjs', '.css', '.html'}:
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
                if REMOVED.search(line):
                    matches.append(f'{path.relative_to(ROOT)}:{number}: {line}')
    assert not matches, '\n'.join(matches)


def test_removed_tool_prompt_routes_and_archived_docs(tmp_path):
    from frontend import server

    assert create_default_registry().get('TopicSpace') is None
    assert 'TopicSpace' not in build_system_prompt(work_dir=str(tmp_path))
    assert not any(str(getattr(route, 'path', '')).startswith('/api/topics') for route in server.app.routes)
    assert '## 已废弃功能' in (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
    names = ('topic-space-ui-notes.md', 'topic-conversation-implementation-2026-09-09.md',
             'topic-chat-integration-contract-2026-09-09.md', 'conversation-first-topics-guide-2026-09-09.md')
    marker = '> 已废弃（2026-10-04）：议题与行动功能已移除，本文仅作历史记录。'
    for name in names:
        assert not (ROOT / 'docs' / name).exists()
        assert (ROOT / 'docs/archive/topics' / name).read_text(encoding='utf-8').startswith(marker)


def _legacy_messages():
    return [Message(role='user', content='保存对话要点'),
            Message(role='assistant', content='已记录旧要点', tool_uses=[
                ToolUseBlock(tool_use_id='legacy-call', tool_name='TopicSpace', arguments={'operation': 'list'})]),
            Message(role='user', content='', tool_results=[ToolResultBlock(tool_use_id='legacy-call', content='历史记录内容')]),
            Message(role='assistant', content='旧记录仍可查看')]


@pytest.mark.asyncio
async def test_removed_historical_tool_loads_displays_and_continues_without_reexecution(tmp_path):
    from frontend.server import messages_to_display

    legacy = tmp_path / 'legacy.jsonl'
    records = [record for message in _legacy_messages() for record in SessionRecord.from_message(message)]
    legacy.write_text('\n'.join(record.to_jsonl() for record in records) + '\n', encoding='utf-8')
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    loaded = [SessionRecord.from_jsonl(line) for line in legacy.read_text(encoding='utf-8').splitlines()]
    assert all(record is not None for record in loaded)
    messages = records_to_messages(loaded)
    rendered = messages_to_display(messages)
    assert rendered[1]['tools'] == [{'name': 'TopicSpace', 'args': {'operation': 'list'},
                                     'result': '历史记录内容', 'is_error': False}]
    for serializer in (build_anthropic_messages, build_chat_completion_messages, build_openai_input):
        request = json.dumps(serializer(messages), ensure_ascii=False)
        assert 'TopicSpace' in request and 'legacy-call' in request and '历史记录内容' in request

    class ContinuingProvider(LLMClient):
        calls = 0

        async def stream(self, conversation, system='', tools=None):
            self.calls += 1
            history = json.dumps(build_anthropic_messages(conversation.get_messages()), ensure_ascii=False)
            assert 'TopicSpace' in history and '历史记录内容' in history
            assert 'TopicSpace' not in json.dumps(tools)
            yield TextDelta('已继续处理新的对话')
            yield StreamEnd('end_turn', input_tokens=20, output_tokens=10)

    client = ContinuingProvider()
    conversation = ConversationManager()
    conversation.restore_history(messages)
    conversation.add_user_message('继续新的对话')
    agent = Agent(client, create_default_registry(), 'anthropic', work_dir=str(tmp_path))
    events = [event async for event in agent.run(conversation)]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert ''.join(event.text for event in events if isinstance(event, StreamText)) == '已继续处理新的对话'
    assert client.calls == 1
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_removed_entry_absent_and_legacy_history_renders_in_real_browser(tmp_path, monkeypatch):
    import uvicorn

    electron = ROOT / 'client/node_modules/electron/dist/electron.exe'
    if not electron.is_file():
        pytest.skip('Optional desktop renderer unavailable')
    monkeypatch.syspath_prepend(str(ROOT / 'tests'))
    from chat_queue_render_server import fixture_app
    from frontend.server import messages_to_display

    fixture_root = ROOT / ('.tmp-chat-queue-integration-deprecated-' + uuid4().hex)
    app = fixture_app(fixture_root, initial_history=messages_to_display(_legacy_messages()))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started
    renderer = tmp_path / 'removed-feature.cjs'
    renderer.write_text(r'''
const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),path=require('node:path');
const output=process.argv[2],base=process.argv[3],errors=[];
app.setPath('userData',path.join(output,'profile'));app.disableHardwareAcceleration();
app.whenReady().then(async()=>{
  const win=new BrowserWindow({show:false,width:1280,height:900,webPreferences:{backgroundThrottling:false,contextIsolation:true}});
  win.webContents.on('console-message',(_event,level,message)=>{if(level===3)errors.push(message);});
  try{
    await win.loadURL(base+'/desktop-boot?session=chat-queue-fixture-only&next=/');
    const report=await win.webContents.executeJavaScript(`new Promise((resolve,reject)=>{
      let n=0;const timer=setInterval(async()=>{
        if(window.MewConversationContext?.getContext()?.available && !document.getElementById('btn-send').disabled && document.getElementById('messages').textContent.includes('历史记录内容')){
          clearInterval(timer);
          if(document.querySelector('[data-open-topic-space],#topic-space,#topic-nav'))return reject(new Error('Removed entry returned'));
          if(!document.getElementById('messages').textContent.includes('TopicSpace'))return reject(new Error('Historical tool card missing'));
          resolve({removedEntries:0,historicalToolRendered:true});
        }else if(++n>400){clearInterval(timer);reject(new Error('Legacy UI timeout: '+document.body.innerText.slice(-1500)));}
      },30);
    })`);
    if(errors.length)throw new Error(errors.join('\n'));
    fs.writeFileSync(path.join(output,'removed-feature-report.json'),JSON.stringify({...report,consoleErrors:errors}));
    fs.writeFileSync(path.join(output,'removed-feature.png'),(await win.webContents.capturePage()).toPNG());
    win.destroy();app.exit(0);
  }catch(error){console.error(error);win.destroy();app.exit(1);}
});
''', encoding='utf-8')
    env = dict(os.environ)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    try:
        result = subprocess.run([str(electron), str(renderer), str(tmp_path), f'http://127.0.0.1:{port}'],
                                env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads((tmp_path / 'removed-feature-report.json').read_text(encoding='utf-8'))
        assert report == {'removedEntries': 0, 'historicalToolRendered': True, 'consoleErrors': []}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
