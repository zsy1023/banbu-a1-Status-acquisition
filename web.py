"""
Web服务器 — HTTPS + 仪表盘 + REST API

端口: 配置里的 web_port（默认 18443），被占用或被系统保留时自动顺延并发布实际端口
证书: 自签名 cert/server.crt + server.key
"""
import os
import json
import logging
import socket
from datetime import datetime

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from cert_util import CERT_FILE, KEY_FILE
from core import NotifyRuleManager
from webhook import load_webhooks, save_webhooks, push_events as push_webhook_events
import uuid

logger = logging.getLogger("printer.web")


class ChineseAccessLogger(AbstractAccessLogger):
    """中文简化的 HTTP 访问日志

    aiohttp 默认的 AccessLogger 输出 Apache combined 风格英文日志
    （IP、时间、请求行、状态码、字节数、Referer、User-Agent），
    这里替换为一行中文摘要：IP、方法、路径、状态中文名、响应大小、耗时。

    浏览器自动发起的 /favicon.ico 请求会被静默（服务未提供该文件，只会刷 404）。
    """

    STATUS_TEXT = {
        200: "成功", 201: "已创建", 204: "无内容", 206: "部分内容",
        301: "永久重定向", 302: "临时重定向", 304: "未修改",
        400: "请求有误", 401: "需要登录", 403: "无权限", 404: "未找到",
        405: "方法不允许", 429: "请求过多",
        500: "服务器错误", 502: "网关错误", 503: "服务不可用",
    }
    SILENT_PATHS = frozenset({"/favicon.ico"})

    @staticmethod
    def _size_text(body_length):
        if not body_length:
            return "-"
        if body_length < 1024:
            return f"{body_length}B"
        return f"{body_length / 1024:.1f}KB"

    def log(self, request, response, time):
        path = request.path
        if path in self.SILENT_PATHS:
            return

        status = response.status
        label = self.STATUS_TEXT.get(status, "")
        status_text = f"{status} {label}".strip()
        ms = time * 1000
        cost = "<1ms" if ms < 1 else f"{ms:.0f}ms"

        self.logger.info(
            "%s %s %s → %s（%s，%s）",
            request.remote or "-",
            request.method,
            path,
            status_text,
            self._size_text(response.body_length),
            cost,
        )


# 从外部文件加载仪表盘HTML
HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_FILE = os.path.join(HERE, "templates", "dashboard.html")


def load_dashboard_html():
    """加载仪表盘HTML"""
    if os.path.exists(DASHBOARD_FILE):
        with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>仪表盘模板缺失</h1>"


# ── Webhook 存储已移至 webhook.py ────────────────


def create_app(core):
    """创建aiohttp web应用并注册路由

    Args:
        core: PrinterCore 实例
    """
    async def handle_test_event(request):
        """POST /api/test-event — 推送测试事件到已注册的 webhook"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        evt_type = body.get("event", "paused")
        evt_data = body.get("data", {"percent": 55, "layer": 110})
        if not load_webhooks():
            return web.json_response(
                {"ok": False, "error": "没有已注册的 webhook，无需推送"}, status=400
            )
        await push_webhook_events([{"type": evt_type, "data": evt_data}])
        return web.json_response({"ok": True, "pushed": evt_type})

    app = web.Application()

    # ── 路由 ──────────────────────────────────────────

    async def handle_index(request):
        """GET / — HTML仪表盘

        禁用缓存：仪表盘是单文件前端，改动后必须立即生效，
        否则浏览器会用缓存的旧页面（曾导致「发了新代码但界面行为没变」）。
        """
        html = load_dashboard_html()
        return web.Response(
            text=html, content_type="text/html", charset="utf-8",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    async def handle_api_status(request):
        """GET /api/status — 打印机状态JSON"""
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        status = core.get_formatted_status()
        status["cached_ttl"] = 15
        return web.json_response(status)

    async def handle_api_refresh(request):
        """POST /api/refresh — 强制刷新"""
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, core.do_refresh)
        return web.json_response(result)

    async def handle_send_code(request):
        """POST /api/send-code — 发送验证码"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        # 允许请求中指定手机号（覆盖配置），前端登录框可修改
        phone = (body.get("phone") or "").strip()
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, core.do_send_sms, phone or None)
        return web.json_response(result)

    async def handle_login(request):
        """POST /api/login — 验证码登录"""
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

        # 登录后立即刷新一次
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, core.do_refresh)

        return web.json_response(result)

    async def handle_api_config(request):
        """GET /api/config — 查看当前配置（脱敏）"""
        c = core.config
        safe = {
            "phone": c["phone"][:3] + "****" + c["phone"][-4:],
            "serial": c["serial"],
            "web_port": request.app.get("web_port", c.get("web_port", 18443)),
            "refresh_interval": c.get("refresh_interval", 30),
            "login_status": "已登录" if not core.login_required else "未登录",
        }
        return web.json_response(safe)

    async def handle_control(request):
        """POST /api/control — 控制指令

        一键直达（前端按钮用，无二次确认）:
            {"action": "light_on", "direct": true}
            → {"ok":true, "status":"executed", "result":{"light_mode":"on"}}

        两步确认（保留兼容）:
            {"action": "light_toggle"}                  → 返回 confirm_id
            {"action": "light_toggle", "confirm_id":"x"} → 执行
        """
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

        import asyncio
        loop = asyncio.get_event_loop()

        if confirm_id:
            # Step 2: 确认执行
            result = await loop.run_in_executor(None, core.control_confirm, confirm_id)
            return web.json_response(result)

        if body.get("direct"):
            # 一键直达：直接执行，不生成 confirm_id
            result = await loop.run_in_executor(
                None, lambda: core.control_request(action, None, True))
            return web.json_response(result)

        # Step 1: 创建待确认
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
            hooks = load_webhooks()
            hooks.append({'id': hook_id, 'url': url, 'events': events})
            save_webhooks(hooks)
            logger.info('Webhook注册: %s -> %s (%d events)', hook_id, url, len(events))
            return web.json_response({'ok': True, 'id': hook_id})
        elif action == 'unregister':
            hook_id = body.get('id', '')
            hooks = load_webhooks()
            hooks = [h for h in hooks if h['id'] != hook_id]
            save_webhooks(hooks)
            return web.json_response({'ok': True})
        return web.json_response({'ok': False, 'error': '未知action'}, status=400)

    async def handle_temp_history(request):
        """GET /api/temp-history — 温度历史（默认最近60分钟）"""
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        minutes = int(request.query.get("minutes", 60))
        data = core.get_temp_history(minutes=minutes)
        return web.json_response({"ok": True, "data": data})

    async def handle_print_history(request):
        """GET /api/print-history — 打印历史"""
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)
        limit = int(request.query.get("limit", 50))
        data = core.get_print_history(limit=limit)
        return web.json_response({"ok": True, "data": data})

    async def handle_retry_rules(request):
        """GET/POST /api/retry-rules — 暂停自动重试配置

        GET  → {"ok":true, "config":{...}, "options":[...], "status":{...}}
        POST → body {"config": {"enabled":bool, "max_retries":int,
                                "retry_delay":int, "reasons":[str]}}
        """
        if core.login_required:
            return web.json_response({"ok": False, "error": "需要先登录"}, status=401)

        if request.method == "GET":
            return web.json_response({"ok": True, **core.get_retry_rules()})

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "需要JSON body"}, status=400)

        cfg = body.get("config", body)
        saved = core.set_retry_rules(cfg)
        return web.json_response({
            "ok": True,
            "config": saved,
            "status": core.retry_manager.status(),
        })

    async def handle_notify_rules(request):
        """GET/POST /api/notify-rules — 通知规则"""
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

    # 注册
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
        ("GET", "/api/retry-rules", handle_retry_rules),
        ("POST", "/api/retry-rules", handle_retry_rules),
    ]:
        app.router.add_route(method, path, handler)

    return app


# 配置端口不可用时的备用端口候选顺序
FALLBACK_PORTS = (19443, 9443, 8443, 29443, 30443)
PORT_SCAN_SPAN = 50


def candidate_ports(preferred, fallbacks=FALLBACK_PORTS, scan=PORT_SCAN_SPAN):
    """按优先级产出候选端口

    先试配置端口，再试几个固定备用端口，最后在配置端口之后做有限顺序扫描。
    末尾的顺序扫描能把整段被系统保留的端口区（如 18354-18453）直接跳过去。
    """
    seen = set()
    for p in (preferred, *fallbacks):
        if p not in seen:
            seen.add(p)
            yield p
    for p in range(preferred + 1, preferred + 1 + scan):
        if p not in seen:
            seen.add(p)
            yield p


def describe_bind_error(err):
    """把绑定失败的 OSError 翻译成可操作的说明"""
    if err is None:
        return "未知错误"
    winerror = getattr(err, "winerror", None)
    if winerror == 10013:
        return ("权限被拒 (WinError 10013)，通常是该端口被 Windows/WinNAT 的保留端口区间独占，"
                "常见于开了 Hyper-V / WSL2 / Docker 的机器")
    if winerror == 10048 or err.errno == 98:
        return "端口已被其他程序占用"
    return f"WinError {winerror}" if winerror else str(err)


def _port_available(port):
    """探测端口能否独占绑定

    不设置 SO_REUSEADDR —— Windows 上它会允许绑定到已被监听的端口，导致探测结果失真。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        return True, None
    except OSError as exc:
        return False, exc
    finally:
        sock.close()


RUNTIME_FILE = os.path.join(HERE, "data", "runtime.json")


def publish_runtime_port(port):
    """把实际监听端口写到 data/runtime.json，供 status.py 等外部工具读取

    配置端口可能被系统保留区占掉而自动回退，外部工具靠这个文件才能拿到真实端口。
    """
    payload = {
        "web_port": port,
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        os.makedirs(os.path.dirname(RUNTIME_FILE), exist_ok=True)
        with open(RUNTIME_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError as e:
        logger.warning("写入运行时端口文件失败 %s: %s", RUNTIME_FILE, e)
        return
    logger.info("实际端口已发布: %s (web_port=%d)", RUNTIME_FILE, port)


async def start_web(app, port):
    """启动HTTPS Web服务器

    返回 (runner, 实际监听端口)。配置端口无法绑定时自动改用候选端口，
    避免整个服务因为一个端口被系统保留而启动失败。
    """
    ssl_context = None
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        import ssl as ssl_mod
        ssl_context = ssl_mod.create_default_context(ssl_mod.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
        logger.info("HTTPS证书已加载: %s", CERT_FILE)
    else:
        logger.warning("未找到证书文件，以HTTP模式运行")

    first_error = None
    actual_port = None
    for candidate in candidate_ports(port):
        ok, err = _port_available(candidate)
        if ok:
            actual_port = candidate
            break
        if first_error is None:
            first_error = err
        logger.warning("端口 %d 不可用（%s），尝试下一个端口", candidate, describe_bind_error(err))

    if actual_port is None:
        raise RuntimeError(
            f"配置端口 {port} 及其候选端口均无法绑定。原始错误: {describe_bind_error(first_error)}。"
            "可用 'netsh int ipv4 show excludedportrange protocol=tcp' 查看系统保留端口区间，"
            "或修改 config.json 里的 web_port。"
        )

    runner = web.AppRunner(app, access_log_class=ChineseAccessLogger)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", actual_port, ssl_context=ssl_context)
        await site.start()
    except OSError:
        await runner.cleanup()
        raise

    if actual_port != port:
        logger.warning("配置端口 %d 不可用，已自动改用 %d（仪表盘地址随之变化）", port, actual_port)

    app["web_port"] = actual_port
    publish_runtime_port(actual_port)

    proto = "https" if ssl_context else "http"
    logger.info("Web服务已启动: %s://0.0.0.0:%d", proto, actual_port)
    return runner, actual_port
