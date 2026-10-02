"""
按 chat 维护 TTS 队列：每条 assistant 正文异步入队合成，缓存后供前端按 seq 顺序播放。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..logger import get_logger
from .client import DoubaoTTSClient

logger = get_logger(__name__)


def should_skip_tts_content(content: str) -> bool:
    t = (content or "").strip()
    if not t:
        return True
    if t == "HEARTBEAT_OK":
        return True
    if len(t) < 40 and "HEARTBEAT_OK" in t:
        return True
    return False


@dataclass
class TTSQueueItem:
    seq: int
    chat_id: str
    content_hash: str
    content: str
    status: str = "pending"  # pending | ready | failed
    audio: Optional[bytes] = None
    error: Optional[str] = None
    delivered: bool = False
    created_at: float = field(default_factory=time.time)


class TTSQueueManager:
    """单例：每个 chat_id 单调 seq，同正文 hash 去重。"""

    def __init__(self) -> None:
        self._items: Dict[str, List[TTSQueueItem]] = {}
        self._seq: Dict[str, int] = {}
        self._hash_keys: Dict[str, int] = {}  # f"{chat_id}:{digest}" -> seq
        self._locks: Dict[str, asyncio.Lock] = {}
        self._client: Optional[DoubaoTTSClient] = None
        self._webchat_tts_enabled: bool = True

    def is_tts_enabled(self, chat_id: str) -> bool:
        """WebChat 会话受页面「回复语音」开关控制；其它 channel 默认允许。"""
        if chat_id.startswith("webchat:"):
            return self._webchat_tts_enabled
        return True

    def set_webchat_tts_enabled(self, enabled: bool) -> None:
        self._webchat_tts_enabled = enabled
        if not enabled:
            self.purge_webchat_queues()
        logger.info("WebChat TTS enabled=%s", enabled)

    def purge_webchat_queues(self) -> None:
        """关闭语音时清空 WebChat 队列，避免重新勾选后播放旧缓存。"""
        for key in list(self._hash_keys.keys()):
            if key.startswith("webchat:"):
                del self._hash_keys[key]
        for chat_id in list(self._items.keys()):
            if chat_id.startswith("webchat:"):
                del self._items[chat_id]
                self._seq.pop(chat_id, None)

    def _tts_available(self) -> bool:
        import os
        from pathlib import Path
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        return bool(os.environ.get("CLAWSPEAKER_TTS_API_KEY", "").strip())

    def _get_client(self) -> DoubaoTTSClient:
        if self._client is None:
            self._client = DoubaoTTSClient()
        return self._client

    def _lock_for(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    async def enqueue(self, chat_id: str, content: str) -> Optional[int]:
        """异步入队；已存在同 hash 则返回已有 seq。"""
        content = (content or "").strip()
        if should_skip_tts_content(content) or not self._tts_available():
            return None

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        dedup_key = f"{chat_id}:{digest}"
        if dedup_key in self._hash_keys:
            return self._hash_keys[dedup_key]

        async with self._lock_for(chat_id):
            if dedup_key in self._hash_keys:
                return self._hash_keys[dedup_key]

            seq = self._seq.get(chat_id, 0) + 1
            self._seq[chat_id] = seq
            item = TTSQueueItem(
                seq=seq,
                chat_id=chat_id,
                content_hash=digest,
                content=content,
            )
            self._items.setdefault(chat_id, []).append(item)
            self._hash_keys[dedup_key] = seq

        asyncio.create_task(self._synthesize_item(item))
        logger.info(
            "TTS queued chat_id=%s seq=%d content_len=%d",
            chat_id,
            seq,
            len(content),
        )
        return seq

    async def _synthesize_item(self, item: TTSQueueItem) -> None:
        from .section import get_or_create_section_id

        try:
            client = self._get_client()
            section_id = None
            if client.config.use_section_id:
                sm = getattr(self, "session_manager", None)
                if sm is not None:
                    section_id = await get_or_create_section_id(
                        sm, item.chat_id, channel="webchat", account_id="local"
                    )
            audio = await client.synthesize_async(
                item.content, section_id=section_id
            )
            item.audio = audio
            item.status = "ready"
            logger.info(
                "TTS ready chat_id=%s seq=%d bytes=%d",
                item.chat_id,
                item.seq,
                len(audio),
            )
        except Exception as exc:
            item.status = "failed"
            item.error = str(exc)
            logger.warning(
                "TTS failed chat_id=%s seq=%d: %s",
                item.chat_id,
                item.seq,
                exc,
            )

    async def sync_assistant_messages_from_session(
        self, chat_id: str, session_manager
    ) -> None:
        """把会话里已有 assistant 正文补进队列（仅缺 hash 的）。"""
        if not self._tts_available():
            return
        session = await session_manager.resolve_session(chat_id)
        if not session:
            return
        for m in session.messages:
            if m.role == "assistant" and (m.content or "").strip():
                await self.enqueue(chat_id, m.content)

    def get_pending_items(
        self, chat_id: str, since_seq: int = 0, *, include_pending: bool = False
    ) -> List[TTSQueueItem]:
        items = self._items.get(chat_id, [])
        out: List[TTSQueueItem] = []
        for it in sorted(items, key=lambda x: x.seq):
            if it.seq <= since_seq or it.delivered:
                continue
            if it.status == "ready" and it.audio:
                out.append(it)
            elif include_pending and it.status == "pending":
                out.append(it)
        return out

    def max_seq(self, chat_id: str) -> int:
        return self._seq.get(chat_id, 0)

    def ack_through(self, chat_id: str, through_seq: int) -> None:
        for it in self._items.get(chat_id, []):
            if it.seq <= through_seq:
                it.delivered = True

    def bind_session_manager(self, session_manager) -> None:
        self.session_manager = session_manager


_manager: Optional[TTSQueueManager] = None


def get_tts_queue() -> TTSQueueManager:
    global _manager
    if _manager is None:
        _manager = TTSQueueManager()
    return _manager


def enqueue_assistant_tts(
    chat_id: str, content: str, *, enabled: bool | None = None
) -> None:
    """Agent 写入 assistant 后调用（不阻塞主流程）。"""
    if should_skip_tts_content(content):
        return
    q = get_tts_queue()
    if enabled is None:
        enabled = q.is_tts_enabled(chat_id)
    if not enabled:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(q.enqueue(chat_id, content))
