"""
channels/webchat.py — 轻量 Web 聊天界面
基于 FastAPI + Server-Sent Events (SSE) 实现流式输出；支持会话列表与刷新后恢复历史。
回复正文（assistant content）流式展示结束后，可选豆包 TTS 合成并通过 SSE 推送音频。
"""
import asyncio
import base64
import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

from ..agent import AgentContext
from ..logger import get_logger
from .base import BaseChannelAdapter

if TYPE_CHECKING:
    from ..memory.session import SessionManager

logger = get_logger(__name__)

# 前端：左侧会话栏 + 主区；localStorage 保存当前 chat_id
WEBCHAT_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><title>ClawSpeaker</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; font-family: system-ui, sans-serif; background: #1a1a2e; color: #eaeaea; }
  #app { display: flex; height: 100vh; max-width: 1200px; margin: 0 auto; }
  #sidebar {
    width: 260px; min-width: 220px; border-right: 1px solid #333; display: flex; flex-direction: column;
    background: #12121f; padding: 12px; gap: 8px;
  }
  #sidebar h3 { margin: 0 0 8px 0; font-size: 1rem; color: #888; }
  #new-chat {
    padding: 10px; border-radius: 8px; border: none; background: #00d4ff; color: #1a1a2e;
    font-weight: bold; cursor: pointer;
  }
  #new-chat:hover { background: #00b8e6; }
  #conv-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
  .conv-item {
    text-align: left; padding: 10px; border-radius: 8px; border: 1px solid #333; background: #16213e;
    color: #ccc; cursor: pointer; font-size: 13px;
  }
  .conv-item:hover { border-color: #555; }
  .conv-item.active { border-color: #00d4ff; background: #1a2744; color: #fff; }
  .conv-item .preview { opacity: 0.75; font-size: 11px; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #main { flex: 1; display: flex; flex-direction: column; padding: 16px; min-width: 0; }
  #main h2 { margin: 0 0 12px 0; }
  #messages {
    flex: 1; border: 1px solid #333; border-radius: 8px; padding: 16px; overflow-y: auto;
    margin-bottom: 12px; background: #16213e;
  }
  .user { color: #00d4ff; margin: 8px 0; white-space: pre-wrap; }
  .agent { color: #ffd700; margin: 8px 0; }
  .agent-body { white-space: pre-wrap; }
  .system { color: #888; font-size: 0.85em; margin: 6px 0; white-space: pre-wrap; }
  #input-area { display: flex; gap: 8px; }
  #input { flex: 1; padding: 12px; border-radius: 8px; border: 1px solid #333; background: #16213e; color: #eaeaea; font-size: 15px; }
  button.send { padding: 12px 20px; border-radius: 8px; border: none; background: #00d4ff; color: #1a1a2e; cursor: pointer; font-weight: bold; }
  button.send:hover { background: #00b8e6; }
  .tts-hint { color: #888; font-size: 12px; margin-top: 4px; }
  #toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }
  #toolbar label { display: flex; align-items: center; gap: 6px; font-size: 14px; color: #aaa; cursor: pointer; user-select: none; }
  #toolbar input[type="checkbox"] { width: 16px; height: 16px; accent-color: #00d4ff; cursor: pointer; }
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <h3>历史对话</h3>
    <button type="button" id="new-chat">+ 新对话</button>
    <div id="conv-list"></div>
  </aside>
  <main id="main">
    <h2>ClawSpeaker WebChat</h2>
    <div id="toolbar">
      <label><input type="checkbox" id="tts-enable"> 回复语音（每条 assistant 正文排队合成，按顺序播放）</label>
    </div>
    <div id="messages"></div>
    <div id="input-area">
      <input id="input" type="text" placeholder="输入消息..." autofocus>
      <button type="button" class="send" id="btn-send">发送</button>
    </div>
  </main>
</div>
<script>
(function () {
  const STORAGE_KEY = 'clawspeaker_webchat_chat_id';
  const TTS_ENABLE_KEY = 'clawspeaker_webchat_tts_enable';
  const msgs = document.getElementById('messages');
  const convList = document.getElementById('conv-list');
  const inp = document.getElementById('input');
  const ttsEnable = document.getElementById('tts-enable');
  let currentChatId = localStorage.getItem(STORAGE_KEY) || 'webchat:default';
  let lastDeliveredSeqByChat = {};
  let queuedSeqByChat = {};
  let sendInProgress = false;
  let pollInProgress = false;
  const audioQueue = [];
  let drainingAudio = false;
  let audioPlayGen = 0;
  const POLL_MS = 4000;
  const TTS_WAIT_MS = 90000;
  const TTS_POLL_INTERVAL_MS = 600;

  var ttsStored = localStorage.getItem(TTS_ENABLE_KEY);
  ttsEnable.checked = ttsStored === null ? true : ttsStored === '1';

  function stopActiveAudio() {
    audioPlayGen += 1;
    drainingAudio = false;
    audioQueue.length = 0;
    if (currentAudio) {
      currentAudio.pause();
      currentAudio.removeAttribute('src');
      currentAudio.load();
      currentAudio = null;
    }
  }

  function resetTtsPlaybackState(chatId) {
    if (chatId) {
      delete lastDeliveredSeqByChat[chatId];
      delete queuedSeqByChat[chatId];
    } else {
      lastDeliveredSeqByChat = {};
      queuedSeqByChat = {};
    }
  }

  async function ackTtsThrough(chatId, throughSeq) {
    if (!throughSeq) return;
    await fetch('/api/tts/ack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, through_seq: throughSeq })
    });
  }

  async function syncTtsSettings() {
    await fetch('/api/tts/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: ttsEnable.checked })
    });
    stopActiveAudio();
    if (!ttsEnable.checked) {
      resetTtsPlaybackState(null);
    } else {
      resetTtsPlaybackState(currentChatId);
    }
  }

  ttsEnable.addEventListener('change', function () {
    localStorage.setItem(TTS_ENABLE_KEY, ttsEnable.checked ? '1' : '0');
    syncTtsSettings();
  });
  syncTtsSettings();

  let currentAudio = null;
  let audioUnlocked = false;

  document.body.addEventListener('click', function () {
    audioUnlocked = true;
  }, { once: false, capture: true });

  const SCROLL_NEAR_PX = 80;

  function isNearBottom() {
    return msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight <= SCROLL_NEAR_PX;
  }

  function scrollToBottomIfNear(force) {
    if (force === true || (force !== false && isNearBottom())) {
      msgs.scrollTop = msgs.scrollHeight;
    }
  }

  function appendMsg(role, text) {
    const d = document.createElement('div');
    d.className = role;
    d.textContent = text;
    msgs.appendChild(d);
    return d;
  }

  function addMsg(role, text) {
    appendMsg(role, text);
    scrollToBottomIfNear(true);
  }

  function showTtsToast(msg) {
    var el = document.getElementById('tts-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'tts-toast';
      el.style.cssText = 'position:fixed;bottom:16px;right:16px;background:#333;color:#ffd700;padding:10px 14px;border-radius:8px;font-size:13px;z-index:9999;max-width:280px;';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.display = 'block';
    setTimeout(function () { el.style.display = 'none'; }, 6000);
  }

  function playAudioAndWait(b64) {
    const gen = audioPlayGen;
    return new Promise(function (resolve, reject) {
      const audio = new Audio('data:audio/mpeg;base64,' + b64);
      currentAudio = audio;
      function finish(ok) {
        if (gen !== audioPlayGen) {
          resolve();
          return;
        }
        currentAudio = null;
        if (ok) resolve();
        else reject(new Error('audio error'));
      }
      audio.onended = function () { finish(true); };
      audio.onerror = function () { finish(false); };
      const p = audio.play();
      if (p && typeof p.then === 'function') {
        p.catch(function (e) {
          showTtsToast('语音无法自动播放，请先点击页面任意处');
          finish(false);
        });
      }
    });
  }

  function enqueueAudioEntry(chatId, seq, b64) {
    audioQueue.push({ chatId: chatId, seq: seq, b64: b64 });
    drainAudioQueue();
  }

  async function drainAudioQueue() {
    if (drainingAudio || !audioQueue.length || !ttsEnable.checked) return;
    drainingAudio = true;
    while (audioQueue.length && ttsEnable.checked) {
      const entry = audioQueue.shift();
      try {
        await playAudioAndWait(entry.b64);
      } catch (e) {
        console.warn('TTS play failed', e);
      }
    }
    drainingAudio = false;
    var toast = document.getElementById('tts-toast');
    if (toast) toast.style.display = 'none';
  }

  function getDeliveredSeq(chatId) {
    return lastDeliveredSeqByChat[chatId] || 0;
  }

  function setDeliveredSeq(chatId, seq) {
    lastDeliveredSeqByChat[chatId] = Math.max(getDeliveredSeq(chatId), seq);
  }

  async function initTtsCursor(chatId) {
    if (!ttsEnable.checked) return;
    const r = await fetch('/api/tts/queue?chat_id=' + encodeURIComponent(chatId) + '&init=1');
    const data = await r.json();
    const maxSeq = data.max_seq || 0;
    setDeliveredSeq(chatId, maxSeq);
    queuedSeqByChat[chatId] = {};
    await ackTtsThrough(chatId, maxSeq);
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  async function pollTtsQueue(chatId) {
    if (!ttsEnable.checked) return false;
    const since = getDeliveredSeq(chatId);
    const r = await fetch('/api/tts/queue?chat_id=' + encodeURIComponent(chatId) + '&since_seq=' + since);
    const data = await r.json();
    const items = data.items || [];
    if (!items.length) return false;
    var queued = queuedSeqByChat[chatId];
    if (!queued) queued = queuedSeqByChat[chatId] = {};
    var maxSeq = since;
    var enqueued = false;
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (!it.audio_b64 || queued[it.seq]) continue;
      queued[it.seq] = true;
      maxSeq = Math.max(maxSeq, it.seq);
      if (chatId === currentChatId) {
        enqueueAudioEntry(chatId, it.seq, it.audio_b64);
        enqueued = true;
      }
    }
    if (maxSeq > since) {
      setDeliveredSeq(chatId, maxSeq);
      await ackTtsThrough(chatId, maxSeq);
    }
    return enqueued;
  }

  async function waitForTtsPlayback(chatId) {
    if (!ttsEnable.checked) return;
    const startSeq = getDeliveredSeq(chatId);
    const started = Date.now();
    const deadline = started + TTS_WAIT_MS;
    while (Date.now() < deadline) {
      await pollTtsQueue(chatId);
      if (getDeliveredSeq(chatId) > startSeq) return;
      const r = await fetch(
        '/api/tts/queue?chat_id=' + encodeURIComponent(chatId) +
        '&since_seq=' + startSeq + '&pending=1'
      );
      const data = await r.json();
      if (data.pending_count > 0 || data.max_seq > startSeq) {
        await sleep(TTS_POLL_INTERVAL_MS);
        continue;
      }
      if (Date.now() - started < 8000) {
        await sleep(TTS_POLL_INTERVAL_MS);
        continue;
      }
      return;
    }
    showTtsToast('语音合成超时，请稍后刷新或重试');
  }

  function setActiveInSidebar() {
    convList.querySelectorAll('.conv-item').forEach(function (el) {
      el.classList.toggle('active', el.dataset.chatId === currentChatId);
    });
  }

  async function loadSidebar() {
    const r = await fetch('/api/conversations');
    const data = await r.json();
    const list = data.conversations || [];
    convList.innerHTML = '';
    list.forEach(function (row) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'conv-item';
      btn.dataset.chatId = row.chat_id;
      const shortId = row.chat_id.replace(/^webchat:/, '');
      const head = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = shortId;
      head.appendChild(strong);
      const pv = document.createElement('div');
      pv.className = 'preview';
      pv.textContent = row.preview || '';
      btn.appendChild(head);
      btn.appendChild(pv);
      btn.addEventListener('click', function () { selectChat(row.chat_id); });
      convList.appendChild(btn);
    });
    setActiveInSidebar();
    return list;
  }

  function renderMessages(messageList, pinBottom) {
    var stick = pinBottom === true || (pinBottom !== false && isNearBottom());
    var savedTop = msgs.scrollTop;
    msgs.innerHTML = '';
    messageList.forEach(function (m) {
      if (m.role === 'system') return;
      if (m.role === 'tool') { appendMsg('system', m.content || ''); return; }
      if (m.role === 'user') appendMsg('user', m.content || '');
      else if (m.role === 'assistant') appendMsg('agent', m.content || '');
    });
    if (stick) {
      msgs.scrollTop = msgs.scrollHeight;
    } else {
      var maxTop = Math.max(0, msgs.scrollHeight - msgs.clientHeight);
      msgs.scrollTop = Math.min(savedTop, maxTop);
    }
  }

  async function fetchHistory(chatId) {
    const url = '/api/history?chat_id=' + encodeURIComponent(chatId);
    const r = await fetch(url);
    const data = await r.json();
    return data.messages || [];
  }

  async function loadHistory() {
    const messageList = await fetchHistory(currentChatId);
    renderMessages(messageList, true);
    await initTtsCursor(currentChatId);
  }

  async function pollUpdates() {
    if (pollInProgress) return;
    pollInProgress = true;
    try {
      const list = await loadSidebar();
      const ids = list.map(function (x) { return x.chat_id; });
      if (ids.indexOf(currentChatId) < 0) ids.push(currentChatId);
      for (let i = 0; i < ids.length; i++) {
        const chatId = ids[i];
        if (chatId === currentChatId && !sendInProgress) {
          const messageList = await fetchHistory(chatId);
          renderMessages(messageList);
        }
        await pollTtsQueue(chatId);
      }
    } finally {
      pollInProgress = false;
    }
  }

  async function selectChat(chatId) {
    currentChatId = chatId;
    localStorage.setItem(STORAGE_KEY, currentChatId);
    setActiveInSidebar();
    await loadHistory();
    inp.focus();
  }

  async function createNewChat() {
    const r = await fetch('/api/conversations', { method: 'POST' });
    const data = await r.json();
    if (data.chat_id) await selectChat(data.chat_id);
    await loadSidebar();
  }

  async function init() {
    const list = await loadSidebar();
    const ids = list.map(function (x) { return x.chat_id; });
    if (!ids.length) {
      currentChatId = 'webchat:default';
      localStorage.setItem(STORAGE_KEY, currentChatId);
    } else if (ids.indexOf(currentChatId) < 0) {
      currentChatId = list[0].chat_id;
      localStorage.setItem(STORAGE_KEY, currentChatId);
    }
    setActiveInSidebar();
    await loadHistory();
  }

  async function send() {
    const text = inp.value.trim();
    if (!text) return;
    addMsg('user', text);
    inp.value = '';
    const agDiv = document.createElement('div');
    agDiv.className = 'agent';
    const agBody = document.createElement('div');
    agBody.className = 'agent-body';
    agDiv.appendChild(agBody);
    msgs.appendChild(agDiv);
    let replyText = '';
    sendInProgress = true;
    if (ttsEnable.checked) showTtsToast('🔊 语音排队合成中…');
    try {
      const resp = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          chat_id: currentChatId,
          enable_tts: ttsEnable.checked
        })
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const x = await reader.read();
        if (x.done) break;
        buffer += decoder.decode(x.value, { stream: true });
        const parts = buffer.split('\\n');
        buffer = parts.pop() || '';
        for (let i = 0; i < parts.length; i++) {
          const line = parts[i];
          if (line.startsWith('data:')) {
            try {
              const payload = JSON.parse(line.slice(5).trim());
              if (payload.delta) {
                replyText += payload.delta;
                agBody.textContent = replyText;
                scrollToBottomIfNear();
              }
              if (payload.full) {
                replyText = payload.full;
                agBody.textContent = replyText;
                scrollToBottomIfNear();
              }
              if (payload.done) scrollToBottomIfNear();
            } catch (e) {}
          }
        }
      }
      if (buffer.trim()) {
        const line = buffer.trim();
        if (line.startsWith('data:')) {
          try {
            const payload = JSON.parse(line.slice(5).trim());
            if (payload.delta) {
              replyText += payload.delta;
              agBody.textContent = replyText;
            }
            if (payload.full) {
              replyText = payload.full;
              agBody.textContent = replyText;
            }
          } catch (e) {}
        }
      }
    } catch (e) {
      agBody.textContent = 'Error: ' + e.message;
    } finally {
      sendInProgress = false;
      const messageList = await fetchHistory(currentChatId);
      renderMessages(messageList, false);
      await waitForTtsPlayback(currentChatId);
    }
    await loadSidebar();
    setActiveInSidebar();
  }

  document.getElementById('new-chat').addEventListener('click', createNewChat);
  document.getElementById('btn-send').addEventListener('click', send);
  inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
  init().then(function () {
    setInterval(pollUpdates, POLL_MS);
  });
})();
</script>
</body>
</html>"""


class WebChatAdapter(BaseChannelAdapter):
    def __init__(
        self,
        #host: str = "0.0.0.0",
        host: str = "127.0.0.1",
        port: int = 8000,
        session_manager: Optional["SessionManager"] = None,
        tts_enabled: Optional[bool] = None,
    ):
        super().__init__("webchat")
        self.host = host
        self.port = port
        self.session_manager = session_manager
        if tts_enabled is None:
            tts_enabled = os.environ.get("CLAWSPEAKER_WEBCHAT_TTS", "1").lower() in (
                "1",
                "true",
                "yes",
            )
        self.tts_enabled = tts_enabled
        self._tts_client = None
        self._env_loaded = False
        self._tts_locks: dict[str, asyncio.Lock] = {}
        self._tts_cache: dict[str, tuple[str, float, bytes]] = {}
        self._uvicorn_server = None

    def _load_env(self) -> None:
        if not self._env_loaded:
            load_dotenv(Path(__file__).resolve().parent.parent / ".env")
            self._env_loaded = True

    def _tts_available(self) -> bool:
        if not self.tts_enabled:
            return False
        self._load_env()
        return bool(os.environ.get("CLAWSPEAKER_TTS_API_KEY", "").strip())

    def _get_tts_client(self):
        self._load_env()
        if self._tts_client is None:
            from ..tts import DoubaoTTSClient

            self._tts_client = DoubaoTTSClient()
        return self._tts_client

    async def _resolve_tts_section_id(self, chat_id: str) -> str | None:
        from ..tts.section import get_or_create_section_id, speaker_supports_section_id

        client = self._get_tts_client()
        if not speaker_supports_section_id(client.config.speaker):
            return None
        return await get_or_create_section_id(self.session_manager, chat_id)

    def _tts_lock_for(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._tts_locks:
            self._tts_locks[chat_id] = asyncio.Lock()
        return self._tts_locks[chat_id]

    async def _synthesize_reply_audio(self, content: str, chat_id: str) -> bytes:
        """仅对 assistant 最终回复正文（content）做 TTS；同 chat 同正文 120s 内去重。"""
        content = content.strip()
        if not content:
            raise ValueError("empty content")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        async with self._tts_lock_for(chat_id):
            cached = self._tts_cache.get(chat_id)
            if cached and cached[0] == digest and time.time() - cached[1] < 120:
                logger.info("TTS dedup reuse chat_id=%s", chat_id)
                return cached[2]
            client = self._get_tts_client()
            section_id = await self._resolve_tts_section_id(chat_id)
            audio = await client.synthesize_async(content, section_id=section_id)
            self._tts_cache[chat_id] = (digest, time.time(), audio)
            return audio

    async def start(self):
        try:
            from fastapi import FastAPI, Request
            from fastapi.responses import HTMLResponse, StreamingResponse
            import uvicorn
        except ImportError:
            logger.error("fastapi/uvicorn not installed")
            return

        app = FastAPI(title="ClawSpeaker")
        adapter = self

        from ..tts.queue import get_tts_queue

        if adapter.session_manager:
            get_tts_queue().bind_session_manager(adapter.session_manager)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return WEBCHAT_HTML

        @app.get("/api/conversations")
        async def list_conversations():
            if not adapter.session_manager:
                return {"conversations": []}
            rows = adapter.session_manager.list_conversations_by_channel("webchat")
            return {"conversations": rows}

        @app.post("/api/conversations")
        async def new_conversation():
            if not adapter.session_manager:
                chat_id = f"webchat:{uuid.uuid4().hex[:10]}"
                return {"chat_id": chat_id}
            chat_id = f"webchat:{uuid.uuid4().hex[:10]}"
            session = adapter.session_manager.get_or_create(chat_id, "webchat", "local")
            from ..tts.section import reset_section_id

            await reset_section_id(adapter.session_manager, chat_id)
            return {"chat_id": chat_id}

        async def _messages_for_chat(chat_id: str) -> list:
            if not adapter.session_manager:
                return []
            session = await adapter.session_manager.resolve_session(chat_id)
            if not session:
                return []
            out = []
            for m in session.messages:
                if m.role == "system":
                    continue
                if m.role == "tool":
                    name = m.name or "tool"
                    snippet = (m.content or "")[:600]
                    out.append({"role": "tool", "content": f"[{name}] {snippet}"})
                    continue
                out.append({"role": m.role, "content": m.content or ""})
            return out

        @app.get("/api/history")
        async def history(chat_id: str):
            return {
                "messages": await _messages_for_chat(chat_id),
                "chat_id": chat_id,
            }

        @app.get("/api/tts/queue")
        async def tts_queue_api(
            chat_id: str, since_seq: int = 0, init: int = 0, pending: int = 0
        ):
            """返回 since_seq 之后已合成、未 ack 的语音条目（按 seq 升序）。"""
            q = get_tts_queue()
            items_out = []
            for it in q.get_pending_items(chat_id, since_seq):
                preview = (it.content or "")[:80]
                items_out.append(
                    {
                        "seq": it.seq,
                        "audio_b64": base64.b64encode(it.audio).decode("ascii"),
                        "preview": preview,
                        "status": it.status,
                    }
                )
            pending_count = 0
            if pending:
                for it in q.get_pending_items(
                    chat_id, since_seq, include_pending=True
                ):
                    if it.status == "pending":
                        pending_count += 1
            if items_out:
                logger.debug(
                    "TTS queue deliver chat_id=%s since=%d count=%d",
                    chat_id,
                    since_seq,
                    len(items_out),
                )
            return {
                "items": items_out,
                "max_seq": q.max_seq(chat_id),
                "chat_id": chat_id,
                "pending_count": pending_count,
                "init": bool(init),
            }

        @app.post("/api/tts/settings")
        async def tts_settings(request: Request):
            """同步 WebChat「回复语音」开关；关闭时不合成并清空队列。"""
            body = await request.json()
            enabled = bool(body.get("enabled"))
            get_tts_queue().set_webchat_tts_enabled(enabled)
            return {"enabled": enabled}

        @app.post("/api/tts/ack")
        async def tts_ack(request: Request):
            body = await request.json()
            chat_id = body.get("chat_id") or "webchat:default"
            through_seq = int(body.get("through_seq") or 0)
            get_tts_queue().ack_through(chat_id, through_seq)
            return {"ok": True, "through_seq": through_seq}

        @app.post("/api/chat")
        async def chat(request: Request):
            body = await request.json()
            user_message = body.get("message", "")
            chat_id = body.get("chat_id") or "webchat:default"
            enable_tts = bool(body.get("enable_tts"))
            get_tts_queue().set_webchat_tts_enabled(enable_tts)

            async def event_stream():
                import json

                ctx = AgentContext(
                    chat_id=chat_id,
                    channel="webchat",
                    account_id="local",
                    user_message=user_message,
                    enable_tts=enable_tts,
                )
                out_q: asyncio.Queue = asyncio.Queue()
                result: dict = {"response": None}

                async def collect(chunk: str) -> None:
                    await out_q.put(chunk)

                async def run_agent() -> None:
                    try:
                        if adapter._message_handler:
                            result["response"] = await adapter._message_handler(
                                ctx, on_stream_chunk=collect
                            )
                    finally:
                        await out_q.put(None)

                task = asyncio.create_task(run_agent())
                try:
                    while True:
                        chunk = await out_q.get()
                        if chunk is None:
                            break
                        yield f"data: {json.dumps({'delta': chunk})}\n\n"
                finally:
                    await task

                reply_content = (result["response"] or "").strip()
                yield f"data: {json.dumps({'done': True, 'full': reply_content}, ensure_ascii=False)}\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self._uvicorn_server = uvicorn.Server(config)
        logger.info(f"WebChat at http://{self.host}:{self.port}")
        await self._uvicorn_server.serve()

    async def stop(self):
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

    async def send_message(self, chat_id: str, text: str, **kwargs):
        pass
