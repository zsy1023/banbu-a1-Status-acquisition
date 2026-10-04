"""AstrBot 插件 — 3D 打印机 + 烘干机控制（Webhook 版）

通过 Webhook 接收打印机事件推送，不需要长连接。
"""
import asyncio
import json
import logging
import socket

from astrbot.api.all import *

logger = logging.getLogger("astrbot_plugin_3d")

WEBHOOK_PORT = 19855  # webhook 回调服务器端口


class DryerClient:
    """连接烘干机 TCP 服务 (默认 192.168.1.100:8080)"""

    def __init__(self, host="192.168.1.100", port=8080, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _cmd(self, cmd: str) -> str:
        import socket as sock_mod
        import time
        s = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect((self.host, self.port))
            time.sleep(0.2)
            s.sendall((cmd + "\n").encode())
            time.sleep(0.5)
            return s.recv(4096).decode().strip()
        finally:
            s.close()

    def status(self) -> dict:
        r = self._cmd("STATUS")
        return json.loads(r) if r.startswith("{") else {"error": r}
    def start(self) -> str: return self._cmd("START")
    def stop(self) -> str: return self._cmd("STOP")
    def set_temp(self, t: int) -> str: return self._cmd(f"SETTEMP {t}")
    def set_time(self, m: int) -> str: return self._cmd(f"SETTIME {m}")
    def heater(self, on: bool) -> str: return self._cmd(f"HEATER {'ON' if on else 'OFF'}")
    def fan(self, on: bool) -> str: return self._cmd(f"FAN {'ON' if on else 'OFF'}")


class PrinterBridge(Star):
    """3D 打印机 + 烘干机控制插件"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config)
        self.config = config
        # 打印机
        self.printer_host = "127.0.0.1"
        self.printer_port = 19555
        self.apikey = "sk-your-agent-api-key"
        self.server_host = "127.0.0.1"
        self.server_port = 18443
        self._webhook_id = None  # 注册后返回的ID
        self._sub_events = []
        self._sub_target = None
        self._wh_server = None

        # 烘干机
        self.dryer_host = "192.168.1.100"
        self.dryer_port = 8080
        self._dryer = None

    async def initialize(self):
        self._dryer = DryerClient(self.dryer_host, self.dryer_port)
        logger.info("3D 插件已加载")

    async def terminate(self):
        await self._unregister_webhook()
        if self._wh_server:
            await self._wh_server.stop()

    # ── Webhook 回调服务器 ─────────────────────

    async def _start_webhook_server(self):
        """启动本地 HTTP 服务器接收 webhook 回调"""
        from aiohttp import web

        async def handle_callback(request):
            try:
                data = await request.json()
            except Exception:
                return web.Response(status=400)
            evt_type = data.get("type", "")
            evt_data = data.get("data", {})
            logger.info("收到webhook事件: %s", evt_type)
            await self._send_notification(evt_type, evt_data)
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_post("/", handle_callback)
        runner = web.AppRunner(app)
        await runner.setup()

        # 找一个可用端口
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        self._wh_server = runner
        self.callback_port = port
        logger.info("Webhook回调服务器已启动: 127.0.0.1:%d", port)

    async def _register_webhook(self, events):
        """向服务端注册 webhook"""
        if not self._wh_server:
            await self._start_webhook_server()
        if self._webhook_id:
            return  # 已注册

        import aiohttp
        url = f"http://127.0.0.1:{self.callback_port}/"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://{self.server_host}:{self.server_port}/api/webhook",
                    json={"action": "register", "url": url, "events": events},
                    ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        self._webhook_id = data["id"]
                        self._sub_events = events
                        logger.info("Webhook注册成功: id=%s", self._webhook_id)
                        return True
                    logger.error("Webhook注册失败: %s", data)
        except Exception as e:
            logger.error("Webhook注册异常: %s", e)
        return False

    async def _unregister_webhook(self):
        """注销 webhook"""
        if not self._webhook_id:
            return
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"https://{self.server_host}:{self.server_port}/api/webhook",
                    json={"action": "unregister", "id": self._webhook_id},
                    ssl=False,
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception:
            pass
        self._webhook_id = None
        self._sub_events = []

    # ── 事件格式化 ─────────────────────────────

    def _fmt_status(self, d: dict) -> str:
        if not d.get("connected"):
            return "❌ 打印机未连接"
        sm = {"idle": "待机", "printing": "打印中", "paused": "暂停",
              "finished": "已完成", "failed": "失败"}
        st = sm.get(d.get("state", ""), d.get("state_raw", "未知"))
        pct = d.get("progress", {}).get("percent", 0)
        lyr = d.get("progress", {}).get("layer", 0)
        tot = d.get("progress", {}).get("total_layers", 0)
        t = d.get("temperature", {})
        n, b = t.get("nozzle", {}), t.get("bed", {})
        lines = [f"🖨️ 打印机状态: {st}", f"📊 进度: {pct}%"]
        if tot: lines.append(f"🔢 层数: {lyr}/{tot}")
        lines.append(f"🌡️ 喷头: {n.get('current','?')}°C → {n.get('target',0)}°C")
        lines.append(f"   热床: {b.get('current','?')}°C → {b.get('target',0)}°C")
        if t.get('chamber'): lines.append(f"   腔体: {t['chamber']}°C")
        lines.append(f"🚀 速度: {d.get('speed',100)}%")
        tn = d.get('task',{}).get('name','')
        if tn: lines.append(f"📄 任务: {tn}")
        li = d.get('hardware',{}).get('light_mode','')
        if li: lines.append(f"💡 灯光: {li}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_push(et: str, ed: dict) -> str:
        tpl = {
            "printing_started": "🖨️ 开始打印: {task_name}",
            "complete": "✅ 打印完成: {task_name}",
            "error": "❌ 打印出错，请检查打印机",
            "paused": "⏸️ 打印已暂停",
            "layer": "📈 打印到第 {layer} 层（共 {total_layers} 层）",
            "bed_temp_above": "🌡️ 热床温度超过阈值: {message}",
            "bed_temp_below": "🌡️ 热床温度低于阈值: {message}",
            "nozzle_temp_above": "🌡️ 喷头温度超过阈值: {message}",
            "nozzle_temp_below": "🌡️ 喷头温度低于阈值: {message}",
            "print_remaining": "⏱️ {message}",
            "print_duration": "⏱️ {message}",
            "temp_in_range": "🌡️ {message}",
        }
        t = tpl.get(et, ed.get("message", f"📢 打印机事件: {et}"))
        try: return t.format(**ed)
        except KeyError:
            return ed.get("message", f"📢 [{et}]: {json.dumps(ed, ensure_ascii=False)}")

    async def _send_notification(self, evt_type: str, evt_data: dict):
        if not self._sub_target:
            logger.debug("无订阅目标，跳过推送")
            return
        text = self._fmt_push(evt_type, evt_data)
        try:
            from astrbot.api.message_components import Plain
            await self._sub_target.send([Plain(text)])
            logger.info("推送已发送: %s", text[:50])
        except Exception as e:
            logger.error("推送发送失败: %s", e)

    # ── TCP 通信 ───────────────────────────────

    async def _tcp_request(self, cmd: dict) -> dict:
        cmd["apikey"] = self.apikey
        try:
            r, w = await asyncio.open_connection(self.printer_host, self.printer_port)
            w.write((json.dumps(cmd) + "\n").encode())
            await w.drain()
            data = await asyncio.wait_for(r.read(65536), timeout=15)
            w.close()
            return json.loads(data.decode())
        except asyncio.TimeoutError:
            return {"ok": False, "error": "请求超时"}
        except ConnectionRefusedError:
            return {"ok": False, "error": "打印机服务未运行"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 烘干机 ─────────────────────────────────

    async def _dryer_cmd(self, cmd: str) -> str:
        return await asyncio.to_thread(self._dryer._cmd, cmd)

    async def _dryer_status(self) -> dict:
        return await asyncio.to_thread(self._dryer.status)

    def _fmt_dryer(self, d: dict) -> str:
        if "error" in d:
            return f"❌ 烘干机: {d['error']}"
        return (
            f"🔥 烘干机状态\n状态: {d.get('state','?')}\n"
            f"温度: {d.get('heaterTemp','?')}°C\n"
            f"目标: {d.get('targetTemp','?')}°C\n"
            f"时间: {d.get('remainTime','?')}分钟\n"
            f"加热: {'ON' if d.get('heater') else 'OFF'}\n"
            f"风扇: {'ON' if d.get('fan') else 'OFF'}"
        )

    # ── 命令处理器 ────────────────────────────

    @command("printer")
    async def printer(self, event: AstrMessageEvent, args: str = ""):
        """
        /printer                    — 查看打印机状态
        /printer subscribe          — 订阅推送（所有事件）
        /printer subscribe layer    — 只订阅指定事件
        /printer unsubscribe        — 取消订阅
        /printer subscribe status   — 查看当前订阅
        """
        a = args.strip()

        if a == "subscribe" or a.startswith("subscribe "):
            parts = a.split()
            if len(parts) > 1:
                self._sub_events = parts[1:]
            else:
                self._sub_events = [
                    "layer", "complete", "error", "paused",
                    "printing_started", "bed_temp_above", "bed_temp_below",
                    "nozzle_temp_above", "nozzle_temp_below",
                    "print_remaining", "print_duration", "temp_in_range",
                ]
            self._sub_target = event
            ok = await self._register_webhook(self._sub_events)
            if ok:
                n = len(self._sub_events)
                evt_list = ", ".join(self._sub_events[:5])
                if n > 5: evt_list += "..."
                yield event.plain_result(
                    f"✅ 已订阅 {n} 个事件\\n事件: {evt_list}\\n"
                    f"通过 HTTP 回调推送，稳定可靠"
                )
            else:
                yield event.plain_result("❌ 订阅失败，服务端不可用")

        elif a == "unsubscribe":
            await self._unregister_webhook()
            self._sub_target = None
            yield event.plain_result("✅ 已取消订阅")

        elif a == "subscribe status":
            if self._sub_events:
                n = len(self._sub_events)
                evts = "\\n".join("  \\u2022 " + e for e in self._sub_events)
                yield event.plain_result(
                    f"📡 当前订阅 ({n} 个):\\n{evts}\\n"
                    f"Webhook ID: {self._webhook_id or '无'}"
                )
            else:
                yield event.plain_result("ℹ️ 当前未订阅")

        else:
            if not self.apikey:
                yield event.plain_result("❌ 缺少 API Key")
                return
            r = await self._tcp_request({"cmd": "get_status"})
            if not r.get("ok"):
                yield event.plain_result(f"❌ {r.get('error','失败')}")
                return
            yield event.plain_result(self._fmt_status(r.get("data", {})))

    @command("dryer")
    async def dryer(self, event: AstrMessageEvent, args: str = ""):
        """
        /dryer               — 查看烘干机状态
        /dryer start         — 开始烘干
        /dryer stop          — 停止
        /dryer temp 50       — 设温度
        /dryer time 120      — 设时间
        /dryer heater on/off — 开关加热
        /dryer fan on/off    — 开关风扇
        """
        a = args.strip().split()
        if not a:
            d = await self._dryer_status()
            yield event.plain_result(self._fmt_dryer(d))
            return
        cmd, *rest = a
        if cmd == "start":
            r = await self._dryer_cmd("START")
            yield event.plain_result("✅ 已开始烘干" if r == "OK" else f"❌ 失败: {r}")
        elif cmd == "stop":
            r = await self._dryer_cmd("STOP")
            yield event.plain_result("✅ 已停止" if r == "OK" else f"❌ 失败: {r}")
        elif cmd == "temp":
            if not rest:
                yield event.plain_result("用法: /dryer temp 50"); return
            try:
                t = int(rest[0])
                if not 0 <= t <= 100:
                    yield event.plain_result("❌ 范围 0~100°C"); return
                r = await self._dryer_cmd(f"SETTEMP {t}")
                yield event.plain_result(f"✅ 温度设为 {t}°C" if r == "OK" else f"❌ 失败: {r}")
            except ValueError:
                yield event.plain_result("❌ 请输入数字")
        elif cmd == "time":
            if not rest:
                yield event.plain_result("用法: /dryer time 120"); return
            try:
                m = int(rest[0])
                if not 1 <= m <= 999:
                    yield event.plain_result("❌ 范围 1~999 分钟"); return
                r = await self._dryer_cmd(f"SETTIME {m}")
                yield event.plain_result(f"✅ 时间设为 {m}分钟" if r == "OK" else f"❌ 失败: {r}")
            except ValueError:
                yield event.plain_result("❌ 请输入数字")
        elif cmd == "heater":
            if not rest or rest[0] not in ("on", "off", "开", "关"):
                yield event.plain_result("用法: /dryer heater on/off"); return
            on = rest[0] in ("on", "开")
            r = await self._dryer_cmd(f"HEATER {'ON' if on else 'OFF'}")
            yield event.plain_result(f"✅ 加热已{'开' if on else '关'}" if r == "OK" else f"❌ 失败: {r}")
        elif cmd == "fan":
            if not rest or rest[0] not in ("on", "off", "开", "关"):
                yield event.plain_result("用法: /dryer fan on/off"); return
            on = rest[0] in ("on", "开")
            r = await self._dryer_cmd(f"FAN {'ON' if on else 'OFF'}")
            yield event.plain_result(f"✅ 风扇已{'开' if on else '关'}" if r == "OK" else f"❌ 失败: {r}")
        else:
            d = await self._dryer_status()
            yield event.plain_result(self._fmt_dryer(d))
