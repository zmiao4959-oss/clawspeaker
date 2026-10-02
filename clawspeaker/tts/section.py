"""
豆包 TTS 2.0 多轮语音上下文：section_id 与 chat 绑定并持久化。
"""
from __future__ import annotations

import uuid
from typing import Optional

from ..logger import get_logger
from ..memory.session import SessionManager

logger = get_logger(__name__)


def speaker_supports_section_id(speaker: str) -> bool:
    """仅「豆包语音合成模型 2.0」预设音色支持；S_ 复刻音色不支持。"""
    return not speaker.startswith("S_")


async def get_or_create_section_id(
    session_manager: Optional[SessionManager],
    chat_id: str,
    *,
    channel: str = "webchat",
    account_id: str = "local",
) -> Optional[str]:
    """
    为同一对话返回稳定的 section_id（UUID），写入 session.metadata 并落盘。

    无 SessionManager 时返回一次性 UUID（不跨请求保持）。
    """
    if session_manager is None:
        return str(uuid.uuid4())

    session = session_manager.get_session_by_chat_id(chat_id)
    if session is None:
        session = await session_manager.load_latest_session_for_chat(chat_id)
    if session is None:
        session = session_manager.get_or_create(chat_id, channel, account_id)

    section_id = session.metadata.get("tts_section_id")
    if not section_id:
        section_id = str(uuid.uuid4())
        session.metadata["tts_section_id"] = section_id
        await session_manager.save(session)
        logger.info(
            "TTS section_id created chat_id=%s section_id=%s",
            chat_id,
            section_id,
        )
    return section_id


async def reset_section_id(
    session_manager: Optional[SessionManager],
    chat_id: str,
    *,
    channel: str = "webchat",
    account_id: str = "local",
) -> str:
    """新对话时生成新的 section_id（与旧语音上下文隔离）。"""
    new_id = str(uuid.uuid4())
    if session_manager is None:
        return new_id

    session = session_manager.get_session_by_chat_id(chat_id)
    if session is None:
        session = session_manager.get_or_create(chat_id, channel, account_id)
    session.metadata["tts_section_id"] = new_id
    await session_manager.save(session)
    logger.info(
        "TTS section_id reset chat_id=%s section_id=%s",
        chat_id,
        new_id,
    )
    return new_id
