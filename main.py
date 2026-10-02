"""

main.py — 完整版启动入口（所有阶段集成）

"""

import asyncio

import signal

import sys

from pathlib import Path



from clawspeaker.config import config, WORKSPACE_DIR

from clawspeaker.logger import get_logger

from clawspeaker.llm.router import LLMRouter

from clawspeaker.memory.session import SessionManager

from clawspeaker.agent import Agent

from clawspeaker.heartbeat import HeartbeatLoop

from clawspeaker.cron_scheduler import CronScheduler, CronJob

from clawspeaker.subagent import SubAgentManager

from clawspeaker.gateway.server import GatewayServer

from clawspeaker.channels.webchat import WebChatAdapter

from clawspeaker.tools import ensure_tools_loaded

from clawspeaker.tts.queue import get_tts_queue



ensure_tools_loaded()



logger = get_logger("clawspeaker")





async def main():

    logger.info("=== ClawSpeaker Starting ===")

    

    # --- 核心组件初始化 ---

    router = LLMRouter(config.llm, config.llm.fallback_providers)

    session_mgr = SessionManager(WORKSPACE_DIR / "sessions")

    get_tts_queue().bind_session_manager(session_mgr)

    agent = Agent(router, session_mgr)

    subagent_mgr = SubAgentManager(agent, session_mgr)

    

    # --- Gateway (WebSocket Server) ---

    gateway = GatewayServer(agent)

    gateway_task = asyncio.create_task(gateway.start())

    

    # --- Channel Adapters ---

    adapters = []

    

    # WebChat (always on)

    if config.channels.enabled.get("webchat", True):

        webchat = WebChatAdapter(host="127.0.0.1", port=8000, session_manager=session_mgr)

        webchat.set_message_handler(agent.process_message)

        adapters.append(webchat)

    

    # Telegram (if enabled)

    if config.channels.enabled.get("telegram", False):

        tg_token = config.channels.settings.get("telegram", {}).get("bot_token", "")

        if tg_token:

            from clawspeaker.channels.telegram import TelegramAdapter

            tg = TelegramAdapter(tg_token)

            tg.set_message_handler(agent.process_message)

            adapters.append(tg)

    

    # 启动所有 channel

    channel_tasks = [asyncio.create_task(a.start()) for a in adapters]

    

    # --- Heartbeat ---

    heartbeat = HeartbeatLoop(agent)

    heartbeat_task = asyncio.create_task(heartbeat.run())

    

    # --- Cron Scheduler ---

    # 示例 cron 任务：每天早上 8 点推送摘要

    cron = CronScheduler(agent)

    cron.add_job(CronJob(

        name="morning_summary",

        cron_expr="0 8 * * *",

        task_prompt="Check today's calendar, weather, and any important emails. "

                     "Write a brief morning summary.",

        delivery_channel="webchat",

        delivery_to="default",

    ))

    cron_task = asyncio.create_task(cron.run())

    

    # --- 优雅退出 ---

    stop_event = asyncio.Event()

    

    def shutdown(signum, frame):

        logger.info("Received signal %s, shutting down...", signum)

        stop_event.set()

    

    signal.signal(signal.SIGINT, shutdown)

    signal.signal(signal.SIGTERM, shutdown)

    

    # 打印启动信息

    logger.info(f"Gateway: ws://{config.gateway.host}:{config.gateway.port}")

    logger.info(f"WebChat: http://127.0.0.1:8000")

    logger.info(f"Heartbeat: every {config.agent.heartbeat_interval_min} min")

    logger.info(f"Cron jobs: {len(cron.jobs)} registered")

    logger.info("=== ClawSpeaker Ready ===")

    

    await stop_event.wait()

    logger.info("Shutting down services...")

    await heartbeat.stop()
    await cron.stop()
    await gateway.stop()
    for a in adapters:
        await a.stop()

    background_tasks = [gateway_task, *channel_tasks, heartbeat_task, cron_task]
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    await asyncio.sleep(0.25)

    logger.info("=== ClawSpeaker Stopped ===")





if __name__ == "__main__":

    asyncio.run(main())

