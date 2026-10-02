"""
豆包 / 火山 Seed-TTS 单向流式 HTTP 客户端。
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from typing import Union

import requests

from ..logger import get_logger
from .parser import (
    TTSSegment,
    build_payload,
    merge_adjacent_segments,
    parse_mood_script,
)
from .section import speaker_supports_section_id

logger = get_logger(__name__)

DEFAULT_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
SUCCESS_CODES = {0, 20000000}


@dataclass
class TTSConfig:
    api_key: str
    resource_id: str = "seed-tts-2.0"
    speaker: str = "zh_female_vv_uranus_bigtts"
    url: str = DEFAULT_URL
    uid: str = "clawspeaker"
    timeout: int = 60
    merge_same_mood: bool = True
    use_section_id: bool = True
    disable_markdown_filter: bool = True

    @classmethod
    def from_env(cls) -> TTSConfig:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        merge = os.environ.get("CLAWSPEAKER_TTS_MERGE_SAME_MOOD", "1").lower() in (
            "1",
            "true",
            "yes",
        )
        use_section = os.environ.get("CLAWSPEAKER_TTS_USE_SECTION_ID", "1").lower() in (
            "1",
            "true",
            "yes",
        )
        md_filter = os.environ.get(
            "CLAWSPEAKER_TTS_DISABLE_MARKDOWN_FILTER", "1"
        ).lower() in ("1", "true", "yes")
        return cls(
            api_key=os.environ.get("CLAWSPEAKER_TTS_API_KEY", ""),
            resource_id=os.environ.get("CLAWSPEAKER_TTS_RESOURCE_ID", "seed-tts-2.0"),
            speaker=os.environ.get("CLAWSPEAKER_TTS_SPEAKER", "zh_female_vv_uranus_bigtts"),
            url=os.environ.get("CLAWSPEAKER_TTS_URL", DEFAULT_URL),
            uid=os.environ.get("CLAWSPEAKER_TTS_UID", "clawspeaker"),
            merge_same_mood=merge,
            use_section_id=use_section,
            disable_markdown_filter=md_filter,
        )


class DoubaoTTSClient:
    """文字转语音：支持 <mood> 剧本，多段分别合成后拼接 MP3。"""

    def __init__(self, config: TTSConfig | None = None):
        self.config = config or TTSConfig.from_env()
        if not self.config.api_key:
            raise ValueError("CLAWSPEAKER_TTS_API_KEY is not set")

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.config.api_key,
            "X-Api-Resource-Id": self.config.resource_id,
        }

    def _resolve_section_id(self, section_id: str | None) -> str | None:
        if not self.config.use_section_id or not section_id:
            return None
        if not speaker_supports_section_id(self.config.speaker):
            return None
        return section_id

    def synthesize_segment(
        self,
        segment: TTSSegment,
        *,
        section_id: str | None = None,
    ) -> bytes:
        """合成单段（一段 mood + 正文，或无 mood 正文）。"""
        payload = build_payload(
            segment,
            speaker=self.config.speaker,
            uid=self.config.uid,
            section_id=self._resolve_section_id(section_id),
            disable_markdown_filter=self.config.disable_markdown_filter,
        )
        return self._request_audio(payload)

    def _prepare_segments(self, script: str) -> list[TTSSegment]:
        segments = parse_mood_script(script)
        if not segments:
            raise ValueError("no speakable segments in script")
        if self.config.merge_same_mood:
            before = len(segments)
            segments = merge_adjacent_segments(segments)
            if len(segments) < before:
                logger.info(
                    "TTS merged %d -> %d segments (same mood)",
                    before,
                    len(segments),
                )
        return segments

    def synthesize(self, script: str, *, section_id: str | None = None) -> bytes:
        """解析剧本并合成完整音频（多段 MP3 字节拼接）。"""
        segments = self._prepare_segments(script)
        sid = self._resolve_section_id(section_id)
        if sid:
            logger.info("TTS section_id=%s", sid)

        chunks: list[bytes] = []
        for i, seg in enumerate(segments):
            logger.info(
                "TTS segment %d/%d: mood=%s text_len=%d",
                i + 1,
                len(segments),
                (seg.mood[:20] + "…") if seg.mood and len(seg.mood) > 20 else seg.mood,
                len(seg.text),
            )
            chunks.append(self.synthesize_segment(seg, section_id=section_id))

        audio = b"".join(chunks)
        if not audio:
            raise RuntimeError("no audio data received")
        return audio

    async def synthesize_async(
        self,
        script: str,
        *,
        section_id: str | None = None,
    ) -> bytes:
        """在线程池中执行合成，避免阻塞 asyncio 事件循环。"""
        fn = functools.partial(self.synthesize, script, section_id=section_id)
        return await asyncio.to_thread(fn)

    def synthesize_to_file(
        self,
        script: str,
        output_path: Union[str, Path],
        *,
        section_id: str | None = None,
    ) -> Path:
        """合成并写入文件，返回输出路径。"""
        path = Path(output_path)
        path.write_bytes(self.synthesize(script, section_id=section_id))
        logger.info("TTS saved to %s (%d bytes)", path, path.stat().st_size)
        return path

    def preview_payloads(
        self,
        script: str,
        *,
        section_id: str | None = None,
    ) -> list[dict]:
        """仅解析剧本并生成请求 JSON 列表（不调用 API），便于调试。"""
        segments = self._prepare_segments(script)
        sid = self._resolve_section_id(section_id)
        return [
            build_payload(
                seg,
                speaker=self.config.speaker,
                uid=self.config.uid,
                section_id=sid,
                disable_markdown_filter=self.config.disable_markdown_filter,
            )
            for seg in segments
        ]

    def _request_audio(self, payload: dict) -> bytes:
        session = requests.Session()
        response = session.post(
            self.config.url,
            headers=self._headers(),
            json=payload,
            stream=True,
            timeout=self.config.timeout,
        )
        response.raise_for_status()

        audio_chunks: list[bytes] = []
        for line in response.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            code = event.get("code")
            if code not in SUCCESS_CODES:
                raise RuntimeError(
                    f"TTS failed: code={code}, message={event.get('message')}"
                )
            if event.get("data"):
                audio_chunks.append(base64.b64decode(event["data"]))

        audio = b"".join(audio_chunks)
        if not audio:
            raise RuntimeError("no audio data received for segment")
        return audio
