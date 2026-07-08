"""AstrBot Plugin — 3D Printer + Dryer Control (Webhook Edition)

Receive printer events via Webhook without long connections.
"""
import asyncio
import json
import logging
import socket

from astrbot.api.all import *

logger = logging.getLogger("astrbot_plugin_3d")

WEBHOOK_PORT = 19855


class DryerClient:
    """Connect to Dryer TCP Service (default 192.168.50.109:8080)"""

    def __init__(self, host="192.168.50.109", port=8080, timeout=5):
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
    """3D Printer + Dryer Control Plugin"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config)
        self.config = config
        self.printer_host = "127.0.0.1"
        self.printer_port = 19555
        self.apikey = "your-api-key-here"
        self.server_host = "127.0.0.1"
        self.server_port = 18443
        self._webhook_id = None
        self._sub_events = []
        self._sub_target = None
        self._wh_server = None

        self.dryer_host = "192.168.50.109"
        self.dryer_port = 8080
        self._dryer = None

    async def initialize(self):
        self._dryer = DryerClient(self.dryer_host, self.dryer_port)
        logger.info("3D Plugin loaded")

    async def terminate(self):
        await self._unregister_webhook()
        if self._wh_server:
            await self._wh_server.stop()

    async def _start_webhook_server(self):
        from aiohttp import web

        async def handle_callback(request):
            try:
                data = await request.json()
            except Exception:
                return web.Response(status=400)
            evt_type = data.get("type", "")
            evt_data = data.get("data", {})
            logger.info("Webhook event received: %s", evt_type)
            await self._send_notification(evt_type, evt_data)
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_post("/", handle_callback)
        runner = web.AppRunner(app)
        await runner.setup()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        self._wh_server = runner
        self.callback_port = port
        logger.info("Webhook callback server started: 127.0.0.1:%d", port)

    async def _register_webhook(self, events):
        if not self._wh_server:
            await self._start_webhook_server()
        if self._webhook_id:
            return

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
                        logger.info("Webhook registered: id=%s", self._webhook_id)
                        return True
                    logger.error("Webhook registration failed: %s", data)
        except Exception as e:
            logger.error("Webhook registration error: %s", e)
        return False

    async def _unregister_webhook(self):
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

    def _fmt_status(self, d: dict) -> str:
        if not d.get("connected"):
            return "❌ Printer not connected"
        sm = {"idle": "Idle", "printing": "Printing", "paused": "Paused",
              "finished": "Finished", "failed": "Failed"}
        st = sm.get(d.get("state", ""), d.get("state_raw", "Unknown"))
        pct = d.get("progress", {}).get("percent", 0)
        lyr = d.get("progress", {}).get("layer", 0)
        tot = d.get("progress", {}).get("total_layers", 0)
        t = d.get("temperature", {})
        n, b = t.get("nozzle", {}), t.get("bed", {})
        lines = [f"🖨️ Printer: {st}", f"📊 Progress: {pct}%"]
        if tot: lines.append(f"🔢 Layer: {lyr}/{tot}")
        lines.append(f"🌡️ Nozzle: {n.get('current','?')}°C → {n.get('target',0)}°C")
        lines.append(f"   Bed: {b.get('current','?')}°C → {b.get('target',0)}°C")
        if t.get('chamber'): lines.append(f"   Chamber: {t['chamber']}°C")
        lines.append(f"🚀 Speed: {d.get('speed',100)}%")
        tn = d.get('task',{}).get('name','')
        if tn: lines.append(f"📄 Task: {tn}")
        li = d.get('hardware',{}).get('light_mode','')
        if li: lines.append(f"💡 Light: {li}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_push(et: str, ed: dict) -> str:
        tpl = {
            "printing_started": "🖨️ Starting: {task_name}",
            "complete": "✅ Completed: {task_name}",
            "error": "❌ Printer error, please check",
            "paused": "⏸️ Printing paused",
            "layer": "📈 Layer {layer} of {total_layers}",
            "bed_temp_above": "🌡️ Bed temp above threshold: {message}",
            "bed_temp_below": "🌡️ Bed temp below threshold: {message}",
            "nozzle_temp_above": "🌡️ Nozzle temp above threshold: {message}",
            "nozzle_temp_below": "🌡️ Nozzle temp below threshold: {message}",
            "print_remaining": "⏱️ {message}",
            "print_duration": "⏱️ {message}",
            "temp_in_range": "🌡️ {message}",
        }
        t = tpl.get(et, ed.get("message", f"📢 Printer event: {et}"))
        try: return t.format(**ed)
        except KeyError:
            return ed.get("message", f"📢 [{et}]: {json.dumps(ed, ensure_ascii=False)}")

    async def _send_notification(self, evt_type: str, evt_data: dict):
        if not self._sub_target:
            logger.debug("No subscription target, skip")
            return
        text = self._fmt_push(evt_type, evt_data)
        try:
            from astrbot.api.message_components import Plain
            await self._sub_target.send([Plain(text)])
            logger.info("Notification sent: %s", text[:50])
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

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
            return {"ok": False, "error": "Request timeout"}
        except ConnectionRefusedError:
            return {"ok": False, "error": "Printer service not running"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _dryer_cmd(self, cmd: str) -> str:
        return await asyncio.to_thread(self._dryer._cmd, cmd)

    async def _dryer_status(self) -> dict:
        return await asyncio.to_thread(self._dryer.status)

    def _fmt_dryer(self, d: dict) -> str:
        if "error" in d:
            return f"❌ Dryer: {d['error']}"
        return (
            f"🔥 Dryer Status\nState: {d.get('state','?')}\n"
            f"Temp: {d.get('heaterTemp','?')}°C\n"
            f"Target: {d.get('targetTemp','?')}°C\n"
            f"Time: {d.get('remainTime','?')}min\n"
            f"Heater: {'ON' if d.get('heater') else 'OFF'}\n"
            f"Fan: {'ON' if d.get('fan') else 'OFF'}"
        )

    @command("printer")
    async def printer(self, event: AstrMessageEvent, args: str = ""):
        """
        /printer                    — Check printer status
        /printer subscribe          — Subscribe to all events
        /printer subscribe layer    — Subscribe specific event
        /printer unsubscribe        — Unsubscribe
        /printer subscribe status   — Check current subscription
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
                    f"✅ Subscribed {n} events\nEvents: {evt_list}\n"
                    f"Pushed via HTTP callback"
                )
            else:
                yield event.plain_result("❌ Subscribe failed, server unavailable")

        elif a == "unsubscribe":
            await self._unregister_webhook()
            self._sub_target = None
            yield event.plain_result("✅ Unsubscribed")

        elif a == "subscribe status":
            if self._sub_events:
                n = len(self._sub_events)
                evts = "\\n".join("  \\u2022 " + e for e in self._sub_events)
                yield event.plain_result(
                    f"📡 Current subscription ({n}):\n{evts}\n"
                    f"Webhook ID: {self._webhook_id or 'None'}"
                )
            else:
                yield event.plain_result("ℹ️ Not subscribed")

        else:
            if not self.apikey:
                yield event.plain_result("❌ Missing API Key")
                return
            r = await self._tcp_request({"cmd": "get_status"})
            if not r.get("ok"):
                yield event.plain_result(f"❌ {r.get('error','Failed')}")
                return
            yield event.plain_result(self._fmt_status(r.get("data", {})))

    @command("dryer")
    async def dryer(self, event: AstrMessageEvent, args: str = ""):
        """
        /dryer               — Check dryer status
        /dryer start         — Start drying
        /dryer stop          — Stop
        /dryer temp 50       — Set temperature
        /dryer time 120      — Set time
        /dryer heater on/off — Toggle heater
        /dryer fan on/off    — Toggle fan
        """
        a = args.strip().split()
        if not a:
            d = await self._dryer_status()
            yield event.plain_result(self._fmt_dryer(d))
            return
        cmd, *rest = a
        if cmd == "start":
            r = await self._dryer_cmd("START")
            yield event.plain_result("✅ Started" if r == "OK" else f"❌ Failed: {r}")
        elif cmd == "stop":
            r = await self._dryer_cmd("STOP")
            yield event.plain_result("✅ Stopped" if r == "OK" else f"❌ Failed: {r}")
        elif cmd == "temp":
            if not rest:
                yield event.plain_result("Usage: /dryer temp 50"); return
            try:
                t = int(rest[0])
                if not 0 <= t <= 100:
                    yield event.plain_result("❌ Range 0~100°C"); return
                r = await self._dryer_cmd(f"SETTEMP {t}")
                yield event.plain_result(f"✅ Temp set to {t}°C" if r == "OK" else f"❌ Failed: {r}")
            except ValueError:
                yield event.plain_result("❌ Enter a number")
        elif cmd == "time":
            if not rest:
                yield event.plain_result("Usage: /dryer time 120"); return
            try:
                m = int(rest[0])
                if not 1 <= m <= 999:
                    yield event.plain_result("❌ Range 1~999 minutes"); return
                r = await self._dryer_cmd(f"SETTIME {m}")
                yield event.plain_result(f"✅ Time set to {m}min" if r == "OK" else f"❌ Failed: {r}")
            except ValueError:
                yield event.plain_result("❌ Enter a number")
        elif cmd == "heater":
            if not rest or rest[0] not in ("on", "off", "开", "关"):
                yield event.plain_result("Usage: /dryer heater on/off"); return
            on = rest[0] in ("on", "开")
            r = await self._dryer_cmd(f"HEATER {'ON' if on else 'OFF'}")
            yield event.plain_result(f"✅ Heater {'on' if on else 'off'}" if r == "OK" else f"❌ Failed: {r}")
        elif cmd == "fan":
            if not rest or rest[0] not in ("on", "off", "开", "关"):
                yield event.plain_result("Usage: /dryer fan on/off"); return
            on = rest[0] in ("on", "开")
            r = await self._dryer_cmd(f"FAN {'ON' if on else 'OFF'}")
            yield event.plain_result(f"✅ Fan {'on' if on else 'off'}" if r == "OK" else f"❌ Failed: {r}")
        else:
            d = await self._dryer_status()
            yield event.plain_result(self._fmt_dryer(d))