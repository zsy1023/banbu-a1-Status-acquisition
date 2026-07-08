"""
Web服务器 — HTTPS + 仪表盘 + REST API

端口: 18443 (可配置)
证书: 自签名 cert/server.crt + server.key
"""
import os
import json
import logging

from aiohttp import web

from cert_util import CERT_FILE, KEY_FILE
from core import NotifyRuleManager
import uuid

logger = logging.getLogger("printer.web")

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_FILE = os.path.join(HERE, "templates", "dashboard.html")


def load_dashboard_html():
    if os.path.exists(DASHBOARD_FILE):
        with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>仪表盘模板缺失</h1>"


_webhook_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'webhooks.json')


def _load_webhooks():
    try:
        if os.path.exists(_webhook_file):
            with open(_webhook_file, 'r') as f:
                data = f.read()
                return json.loads(data) if data else []
    except:
        pass
    return []


def _save_webhooks(hooks):
    os.makedirs(os.path.dirname(_webhook_file), exist_ok=True)
    with open(_webhook_file, 'w') as f:
        f.write(json.dumps(hooks, ensure_ascii=False, indent=2))


def create_app(core, agent_server):
    app = web.Application()

    async def handle_index(request):
        html = load_dashboard_html()
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def handle_api_status(request):
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        status = core.get_formatted_status()
        status["cached_ttl"] = 15
        return web.json_response(status)

    async def handle_api_refresh(request):
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, core.do_refresh)
        return web.json_response(result)

    async def handle_send_code(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        result = core.do_send_sms()
        return web.json_response(result)

    async def handle_login(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "需要JSON body: {code}"}, status=400)

        code = body.get("code", "")
        if not code:
            return web.json_response({"ok": False, "error": "缺少code"}, status=400)

        result = core.do_login(code)
        if not result.get("ok"):
            return web.json_response(result, status=400)

        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, core.do_refresh)

        return web.json_response(result)

    async def handle_api_config(request):
        c = core.config
        safe = {
            "phone": c["phone"][:3] + "****" + c["phone"][-4:] if c["phone"] else "",
            "serial": c["serial"],
            "api_key_count": len(c.get("api_keys", [])),
            "web_port": c.get("web_port", 18443),
            "agent_port": c.get("agent_port", 19555),
            "refresh_interval": c.get("refresh_interval", 30),
            "login_status": "已登录" if not core.login_required else "未登录",
        }
        return web.json_response(safe)

    async def handle_control(request):
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "需要JSON body"}, status=400)

        action = body.get("action", "")
        confirm_id = body.get("confirm_id", "")

        if not action:
            return web.json_response({"ok": False, "error": "缺少 action"}, status=400)

        if confirm_id:
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, core.control_confirm, confirm_id)
            return web.json_response(result)

        return web.json_response(core.control_request(action))

    async def handle_webhook(request):
        try:
            body = await request.json()
        except:
            return web.json_response({'ok': False, 'error': '需要JSON body'}, status=400)
        action = body.get('action', 'register')
        if action == 'register':
            url = body.get('url', '')
            if not url:
                return web.json_response({'ok': False, 'error': '缺少url'}, status=400)
            events = body.get('events', [])
            hook_id = str(uuid.uuid4())[:8]
            hooks = _load_webhooks()
            hooks.append({'id': hook_id, 'url': url, 'events': events})
            _save_webhooks(hooks)
            logger.info('Webhook注册: %s -> %s (%d events)', hook_id, url, len(events))
            return web.json_response({'ok': True, 'id': hook_id})
        elif action == 'unregister':
            hook_id = body.get('id', '')
            hooks = _load_webhooks()
            hooks = [h for h in hooks if h['id'] != hook_id]
            _save_webhooks(hooks)
            return web.json_response({'ok': True})
        return web.json_response({'ok': False, 'error': '未知action'}, status=400)

    async def handle_temp_history(request):
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        minutes = int(request.query.get("minutes", 60))
        data = core.get_temp_history(minutes=minutes)
        return web.json_response({"ok": True, "data": data})

    async def handle_print_history(request):
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        limit = int(request.query.get("limit", 50))
        data = core.get_print_history(limit=limit)
        return web.json_response({"ok": True, "data": data})

    async def handle_notify_rules(request):
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        if request.method == "GET":
            return web.json_response({"ok": True, "types": NotifyRuleManager.TRIGGER_TYPES, "data": core.get_notify_rules()})
        try:
            body = await request.json()
            rules = body.get("rules", [])
            core.set_notify_rules(rules)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_test_event(request):
        try:
            body = await request.json()
        except:
            body = {}
        evt_type = body.get("event", "paused")
        evt_data = body.get("data", {"percent": 55, "layer": 110})
        if agent_server:
            await agent_server.push_events([{"type": evt_type, "data": evt_data}])
            return web.json_response({"ok": True, "pushed": evt_type})
        return web.json_response({"ok": False, "error": "Agent未运行"}, status=500)

    for method, path, handler in [
        ("GET", "/", handle_index),
        ("GET", "/api/status", handle_api_status),
        ("POST", "/api/refresh", handle_api_refresh),
        ("POST", "/api/send-code", handle_send_code),
        ("POST", "/api/login", handle_login),
        ("GET", "/api/config", handle_api_config),
        ("POST", "/api/control", handle_control),
        ("POST", "/api/test-event", handle_test_event),
        ("GET", "/api/temp-history", handle_temp_history),
        ("POST", "/api/webhook", handle_webhook),
        ("GET", "/api/print-history", handle_print_history),
        ("GET", "/api/notify-rules", handle_notify_rules),
        ("POST", "/api/notify-rules", handle_notify_rules),
    ]:
        app.router.add_route(method, path, handler)

    return app


async def start_web(app, port):
    ssl_context = None
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        import ssl as ssl_mod
        ssl_context = ssl_mod.create_default_context(ssl_mod.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
        logger.info("HTTPS证书已加载: %s", CERT_FILE)
    else:
        logger.warning("未找到证书文件，以HTTP模式运行")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port, ssl_context=ssl_context)
    await site.start()

    proto = "https" if ssl_context else "http"
    logger.info("Web服务已启动: %s://0.0.0.0:%d", proto, port)
    return runner