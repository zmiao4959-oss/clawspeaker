import asyncio
import base64
import json
import pyaudio
from concurrent.futures import ThreadPoolExecutor
import websockets

import logging
logger = logging.getLogger('my_logger')


async def send_text(client):
    text = ["你好", ",今天", "天气", "怎", "么", "样？"]
    for t in text:
        event = {
            "type": "input_text.append",
            "delta": t
        }
        await client.send(json.dumps(event))
    event = {
        "type": "input_text.done"
    }
    await client.send(json.dumps(event))


def write_audio_data(stream, data):
    stream.write(data)


async def write_audio_data_async(stream, data):
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        await loop.run_in_executor(pool, write_audio_data, stream, data)

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000

# init pyaudio
audio = pyaudio.PyAudio()

stream = audio.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    output=True)

async def receive_messages(client):
    while not client.closed:
        message = await client.recv()
        event = json.loads(message)
        message_type = event.get("type")
        if message_type == "response.audio.delta":
            audio_bytes = base64.b64decode(event["delta"])
            event['delta'] = "[MASKED]"
            print(json.dumps(event))
            await write_audio_data_async(stream, audio_bytes)
            continue
        else:
            print(message)

        if message_type == 'response.audio.done':
            break

        continue


def get_session_update_msg():
    config = {
        "voice": "zh_female_kailangjiejie_moon_bigtts",
        "output_audio_format": "pcm",
        "output_audio_sample_rate": RATE,
    }
    event = {
        "type": "tts_session.update",
        "session": config
    }
    return json.dumps(event)

async def with_openai():
    key = "your_api_key"
    ws_url = "wss://ai-gateway.vei.volces.com/v1/realtime?model=doubao-tts"

    headers = {
        "Authorization": f"Bearer {key}",
    }
    async with websockets.connect(ws_url, ping_interval=None, logger=logger, extra_headers=headers) as client:
        session_msg = get_session_update_msg()
        await client.send(session_msg)
        await asyncio.gather(send_text(client), receive_messages(client))



if __name__ == "__main__":
    asyncio.run(with_openai())