"""
解析带 <mood> 标签的剧本文本，生成豆包 TTS API 请求体。

输入示例::

    <mood>用带着害羞又故作凶巴巴的语气，快速说</mood>哼！谁让你是我的主人呢。

    <mood>声音渐渐变小，带着一丝不舍</mood>不过……我这么宠你。

    所以！五分钟到了就起来，好不好？😤

每段 mood 后的正文单独一次 API 调用；无 mood 的尾部段落不带 context_texts。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MOOD_TAG_RE = re.compile(r"<mood>(.*?)</mood>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class TTSSegment:
    """一段待合成的语音：正文 + 可选的语气/情绪说明（来自 <mood>）。"""

    text: str
    mood: str | None = None


def parse_mood_script(script: str) -> list[TTSSegment]:
    """
    将剧本文本拆成若干 TTSSegment。

    - ``<mood>说明</mood>`` 后面直到下一个 ``<mood>`` 或文末为朗读正文
    - 第一个 ``<mood>`` 之前、以及各 mood 块之间的纯文本为无 mood 段落
    """
    if not script or not script.strip():
        return []

    segments: list[TTSSegment] = []
    last_end = 0

    for match in MOOD_TAG_RE.finditer(script):
        prefix = script[last_end : match.start()].strip()
        if prefix:
            segments.append(TTSSegment(text=prefix, mood=None))

        mood = match.group(1).strip()
        content_start = match.end()
        next_match = MOOD_TAG_RE.search(script, content_start)
        hard_end = next_match.start() if next_match else len(script)
        region = script[content_start:hard_end]

        # mood 只绑定紧跟的一段正文；遇到空行后的内容视为无 mood 段落
        para_break = region.find("\n\n")
        if para_break != -1:
            speech = region[:para_break].strip()
            last_end = content_start + para_break + 2
        else:
            speech = region.strip()
            last_end = hard_end

        if speech:
            segments.append(TTSSegment(text=speech, mood=mood or None))

    suffix = script[last_end:].strip()
    if suffix:
        segments.append(TTSSegment(text=suffix, mood=None))

    return segments


def _join_segment_text(left: str, right: str) -> str:
    """合并相邻段正文，避免多余空格被 TTS 读出来。"""
    left, right = left.strip(), right.strip()
    if not left:
        return right
    if not right:
        return left
    if left[-1] in "。！？.!?…~～":
        return left + right
    if right[0] in "，。！？、":
        return left + right
    return left + right


def merge_adjacent_segments(segments: list[TTSSegment]) -> list[TTSSegment]:
    """
    合并相邻且 mood 相同的段，减少多次 API 调用带来的音色漂移。

    仅当需要「同一段落、同一种语气」连续说时才合并；
    mood 不同（如害羞 → 不舍）仍保持分段，以便 context_texts 生效。
    """
    if not segments:
        return []

    merged: list[TTSSegment] = []
    for seg in segments:
        if not merged:
            merged.append(seg)
            continue
        prev = merged[-1]
        if prev.mood == seg.mood:
            merged[-1] = TTSSegment(
                text=_join_segment_text(prev.text, seg.text),
                mood=prev.mood,
            )
        else:
            merged.append(seg)
    return merged


def build_payload(
    segment: TTSSegment,
    *,
    speaker: str,
    uid: str = "clawspeaker",
    audio_format: str = "mp3",
    sample_rate: int = 24000,
    section_id: str | None = None,
    disable_markdown_filter: bool = True,
) -> dict[str, Any]:
    """
    为单段文本构造豆包 unidirectional TTS 请求 JSON（可直接 POST）。

    ``additions`` 为 JSON 字符串，可含：
    - ``context_texts``（仅 mood 段）
    - ``section_id``（TTS 2.0 预设音色，同对话多次合成共用）
    - ``model_type: 4``（speaker 以 ``S_`` 开头的复刻音色）
    - ``disable_markdown_filter``（true 时过滤 **粗体** 等 markdown，避免读星号）
    """
    text = segment.text.strip()
    if not text:
        raise ValueError("segment text is empty")

    req_params: dict[str, Any] = {
        "text": text,
        "speaker": speaker,
        "audio_params": {
            "format": audio_format,
            "sample_rate": sample_rate,
        },
    }

    additions: dict[str, Any] = {}
    if section_id and not speaker.startswith("S_"):
        additions["section_id"] = section_id
    if segment.mood:
        additions["context_texts"] = [segment.mood]
    if speaker.startswith("S_"):
        additions["model_type"] = 4
    if disable_markdown_filter:
        additions["disable_markdown_filter"] = True
    if additions:
        req_params["additions"] = json.dumps(additions, ensure_ascii=False)

    return {"user": {"uid": uid}, "req_params": req_params}


def build_payloads_from_script(
    script: str,
    *,
    speaker: str,
    uid: str = "clawspeaker",
    audio_format: str = "mp3",
    sample_rate: int = 24000,
    section_id: str | None = None,
    disable_markdown_filter: bool = True,
) -> list[dict[str, Any]]:
    """解析整段剧本，返回每段对应的 API 请求体列表。"""
    segments = parse_mood_script(script)
    if not segments:
        raise ValueError("no speakable segments in script")
    return [
        build_payload(
            seg,
            speaker=speaker,
            uid=uid,
            audio_format=audio_format,
            sample_rate=sample_rate,
            section_id=section_id,
            disable_markdown_filter=disable_markdown_filter,
        )
        for seg in segments
    ]
