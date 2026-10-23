"""Webhook 事件推送

把事件 POST 到 data/webhooks.json 里注册的地址。
原先这段逻辑挂在 Agent TCP 服务里，agent 功能移除后独立成模块保留。
"""
import asyncio
import json
import logging
import os

logger = logging.getLogger("printer.webhook")

HERE = os.path.dirname(os.path.abspath(__file__))
WEBHOOK_FILE = os.path.join(HERE, "data", "webhooks.json")


def load_webhooks():
    """读取已注册的 webhook 列表"""
    if not os.path.exists(WEBHOOK_FILE):
        return []
    try:
        with open(WEBHOOK_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except (OSError, ValueError) as e:
        logger.warning("读取 webhooks.json 失败: %s", e)
        return []


def save_webhooks(hooks):
    """覆盖写入 webhook 列表"""
    os.makedirs(os.path.dirname(WEBHOOK_FILE), exist_ok=True)
    with open(WEBHOOK_FILE, "w", encoding="utf-8") as f:
        json.dump(hooks, f, ensure_ascii=False, indent=2)


async def push_events(events):
    """把事件推给所有匹配的 webhook

    requests 是阻塞的，放到线程池里跑，避免卡住事件循环（刷新循环和 web 请求共用）。
    """
    if not events:
        return
    hooks = load_webhooks()
    if not hooks:
        return

    def _deliver():
        import requests

        for hook in hooks:
            url = hook.get("url", "")
            if not url:
                continue
            hook_events = hook.get("events", [])
            for event in events:
                if hook_events and event.get("type") not in hook_events:
                    continue
                try:
                    requests.post(
                        url, json=event, timeout=5,
                        headers={"Content-Type": "application/json"},
                    )
                    logger.info("Webhook推送: %s -> %s", event.get("type"), url)
                except Exception as e:
                    logger.warning("Webhook推送失败 %s: %s", url, e)

    await asyncio.get_event_loop().run_in_executor(None, _deliver)
