"""
拓竹打印机状态服务 — 主入口

启动所有组件:
  1. 加载配置
  2. 生成证书（如需）
  3. 自动登录（用缓存的token）
  4. 启动 Agent TCP 服务
  5. 启动 HTTPS Web 服务（仪表盘 + REST API）
  6. 启动后台状态刷新循环
"""
import asyncio
import json
import logging
import os
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("printer")

CONFIG_FILE = os.path.join(HERE, "config.json")
TOKEN_FILE = os.path.join(HERE, "token.txt")


def banner():
    logger.info("=" * 50)
    logger.info("  拓竹打印机状态服务 v2")
    logger.info("=" * 50)


def load_or_exit():
    if not os.path.exists(CONFIG_FILE):
        logger.error("config.json 不存在！请先运行 start.bat 完成配置")
        sys.exit(1)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    for key in ["phone", "serial", "api_keys"]:
        if key not in config:
            logger.error("config.json 缺少必要字段: %s", key)
            sys.exit(1)

    return config


async def periodic_refresh(core):
    while True:
        try:
            if not core.login_required:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, core.do_refresh)
        except Exception as e:
            logger.error("后台刷新异常: %s", e)
        await asyncio.sleep(core.refresh_interval)


async def main():
    banner()

    config = load_or_exit()

    from cert_util import generate_certificate
    generate_certificate()

    from core import PrinterCore
    core = PrinterCore(config, TOKEN_FILE)
    core._loop = asyncio.get_event_loop()

    from agent_tcp import AgentServer
    agent_server = AgentServer(
        config, core.get_formatted_status,
        control_request_fn=core.control_request,
        control_confirm_fn=core.control_confirm,
    )

    async def on_event(events):
        await agent_server.push_events(events)
    core.on_event_callback = on_event

    if core.try_auto_login():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, core.do_refresh)

    from web import create_app, start_web
    web_app = create_app(core, agent_server)
    web_port = config.get("web_port", 18443)

    logger.info("─" * 50)
    logger.info("  服务已启动！")
    proto = "https"
    logger.info("  🌐 仪表盘:  %s://localhost:%d", proto, web_port)
    logger.info("  🔧 Agent:   tcp://localhost:%d", config.get("agent_port", 19555))
    logger.info("  📡 状态API: %s://localhost:%d/api/status", proto, web_port)
    logger.info("  ⏰ 刷新间隔: %d秒", core.refresh_interval)
    logger.info("─" * 50)

    await asyncio.gather(
        agent_server.start(),
        start_web(web_app, web_port),
        periodic_refresh(core),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("服务已停止")
    except Exception as e:
        logger.error("服务异常退出: %s", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)