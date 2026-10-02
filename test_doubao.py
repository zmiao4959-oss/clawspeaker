"""
豆包 TTS 测试：使用 clawspeaker.tts 解析 <mood> 剧本并合成。

环境变量：
  CLAWSPEAKER_TTS_API_KEY
  CLAWSPEAKER_TTS_RESOURCE_ID  （默认 seed-tts-2.0）
  CLAWSPEAKER_TTS_SPEAKER
  CLAWSPEAKER_TTS_USE_SECTION_ID （默认 1，TTS2.0 预设音色会带 section_id）
"""
import json
import uuid
from pathlib import Path

from clawspeaker.tts import DoubaoTTSClient

SCRIPT = """
    <mood>用带着关心的语气，轻声地说</mood>主人，你终于来啦~
    <mood>用带着关心的语气，轻声地说</mood>今天有什么想做的吗？我已经准备好啦～
"""

OUTPUT_PATH = Path("output.mp3")


def main() -> None:
    client = DoubaoTTSClient()
    # 模拟同一对话内多次 TTS，固定 section_id 便于对比稳定性
    section_id = str(uuid.uuid4())
    print(f"=== 测试用 section_id: {section_id} ===\n")

    from clawspeaker.tts import parse_mood_script, merge_adjacent_segments

    raw = parse_mood_script(SCRIPT)
    merged = merge_adjacent_segments(raw)
    print(f"=== 分段: 解析 {len(raw)} 段 → 合并后 {len(merged)} 段（相同 mood 会合并为一次 API）===")

    print("\n=== 实际调用的 API 请求体 ===")
    for i, payload in enumerate(
        client.preview_payloads(SCRIPT, section_id=section_id), 1
    ):
        print(f"\n--- 第 {i} 段 ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    client.synthesize_to_file(SCRIPT, OUTPUT_PATH, section_id=section_id)
    print(f"\n已保存: {OUTPUT_PATH.resolve()} ({OUTPUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
