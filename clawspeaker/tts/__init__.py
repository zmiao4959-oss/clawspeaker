from .client import DoubaoTTSClient, TTSConfig
from .queue import TTSQueueManager, enqueue_assistant_tts, get_tts_queue
from .parser import (
    TTSSegment,
    build_payload,
    build_payloads_from_script,
    merge_adjacent_segments,
    parse_mood_script,
)
from .section import (
    get_or_create_section_id,
    reset_section_id,
    speaker_supports_section_id,
)

__all__ = [
    "DoubaoTTSClient",
    "TTSConfig",
    "TTSSegment",
    "parse_mood_script",
    "merge_adjacent_segments",
    "build_payload",
    "build_payloads_from_script",
    "get_or_create_section_id",
    "reset_section_id",
    "speaker_supports_section_id",
    "TTSQueueManager",
    "get_tts_queue",
    "enqueue_assistant_tts",
]
