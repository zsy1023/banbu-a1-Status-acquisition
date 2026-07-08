"""
Agent TCP 服务 — 供外部程序/agent通信

协议: JSON over TCP, 换行符分隔
端口: 19555 (可配置)
认证: API key

命令:
  get_status     — 获取当前打印机状态
  subscribe      — 订阅事件（保持长连接，接收推送）
  unsubscribe    — 取消订阅
  ping           — 心跳保活

主动推送事件:
  layer / complete / error / paused / printing_started
"""
import asyncio
import json
import logging
import os
import time

logger = logging.getLogger("printer.agent")


class AgentConnection:
    """单个agent连接"""

    def __init__(self, reader, writer, apikey, addr):
        self.reader = reader
        self.writer = writer
        self.apikey = apikey
        self.addr = addr
        self.subscriptions = []
        self.last_ping = time.time()
        self._closed = False

    async def send(self, data):
        if self._closed:
            return False
        try:
            line = json.dumps(data, ensure_ascii=False) + "\n"
            self.writer.write(line.encode("utf-8"))
            await self.writer.drain()
            return True
        except (ConnectionError, BrokenPipeError):
            self._closed = True
            return False

    async def send_push(self, event_type, event_data):
        for sub in self.subscriptions:
            if sub["type"] == event_type:
                if "value" in sub:
                    val = event_data.get("layer", event_data.get("value", 0))
                    if val < sub["value"]:
                        continue
                await self.send({
                    "ok": True,
                    "type": "push",
                    "event": event_type,
                    "data": event_data,
                })
                return

    def matches_event(self, event_type):
        return any(s["type"] == event_type for s in self.subscriptions)

    def close(self):
        self._closed = True
        try:
            self.writer.close()
        except Exception:
            pass

    @property
    def is_closed(self):
        return self._closed


class AgentServer:
    """Agent TCP 服务器"""

    def __init__(self, config, get_status_fn, control_request_fn=None, control_confirm_fn=None):
        self.config = config
        self.get_status_fn = get_status_fn
        self.control_request_fn = control_request_fn
        self.control_confirm_fn = control_confirm_fn
        self.port = config.get("agent_port", 19555)
        self.api_keys = config.get("api_keys", [])
        self.connections = []
        self._server = None

    def verify_apikey(self, apikey):
        return apikey in self.api_keys

    async def _handle_client(self, reader, writer):
        addr = writer.get_extra_info("peername")
        conn = AgentConnection(reader, writer, "", addr)
        logger.info("Agent新连接: %s", addr)

        buffer = ""

        try:
            while not conn.is_closed:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=60)
                except asyncio.TimeoutError:
                    if time.time() - conn.last_ping > 90:
                        logger.info("Agent连接超时断开: %s", addr)
                        break
                    continue

                if not chunk:
                    logger.info("Agent断开连接: %s", addr)
                    break

                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    await self._handle_message(conn, line)

        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.error("Agent连接异常 %s: %s", addr, e)
        finally:
            conn.close()
            self.connections = [c for c in self.connections if c is not conn]
            logger.info("Agent连接已清理: %s", addr)

    async def _handle_message(self, conn, line):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            await conn.send({"ok": False, "error": "JSON解析失败"})
            return

        cmd = msg.get("cmd", "")
        apikey = msg.get("apikey", "")

        if not self.verify_apikey(apikey):
            await conn.send({"ok": False, "error": "无效的API key"})
            return

        conn.apikey = apikey

        if cmd == "get_status":
            status = self.get_status_fn()
            await conn.send({"ok": True, "type": "response", "data": status})

        elif cmd == "subscribe":
            events = msg.get("events", [])
            if not isinstance(events, list):
                await conn.send({"ok": False, "error": "events必须是数组"})
                return

            for evt in events:
                if isinstance(evt, dict) and "type" in evt:
                    conn.subscriptions = [
                        s for s in conn.subscriptions
                        if not (s["type"] == evt["type"]
                                and s.get("value") == evt.get("value")
                                and s.get("component") == evt.get("component"))
                    ]
                    conn.subscriptions.append(evt)

            if conn not in self.connections:
                self.connections.append(conn)

            logger.info("Agent订阅事件: %s | %s", conn.addr, events)
            await conn.send({
                "ok": True, "type": "response",
                "data": {"subscribed": len(events), "total": len(conn.subscriptions)}
            })

        elif cmd == "unsubscribe":
            event_types = msg.get("events", [])
            if event_types:
                conn.subscriptions = [
                    s for s in conn.subscriptions if s["type"] not in event_types
                ]
            else:
                conn.subscriptions = []

            await conn.send({
                "ok": True, "type": "response",
                "data": {"remaining": len(conn.subscriptions)}
            })

        elif cmd == "control":
            action = msg.get("action", "")
            confirm_id = msg.get("confirm_id", "")
            if not action:
                await conn.send({"ok": False, "error": "缺少 action"})
            elif confirm_id:
                if self.control_confirm_fn:
                    result = self.control_confirm_fn(confirm_id)
                    await conn.send({"ok": True, "type": "response", "data": result})
                else:
                    await conn.send({"ok": False, "error": "控制功能未初始化"})
            else:
                if self.control_request_fn:
                    result = self.control_request_fn(action)
                    await conn.send({"ok": True, "type": "response", "data": result})
                else:
                    await conn.send({"ok": False, "error": "控制功能未初始化"})

        elif cmd == "ping":
            conn.last_ping = time.time()
            await conn.send({"ok": True, "type": "pong"})

        else:
            await conn.send({"ok": False, "error": f"未知命令: {cmd}"})

    async def push_events(self, events):
        if not events:
            logger.debug("push_events: 事件列表为空，跳过")
            return

        before = len(self.connections)
        self.connections = [c for c in self.connections if not c.is_closed]
        after = len(self.connections)

        await self._push_webhooks(events)

        if after == 0:
            logger.info("push_events: 无agent连接，已通过webhook推送 %d 个事件", len(events))
            return

        logger.info("push_events: %d个agent在线, %d个事件待处理", after, len(events))

        for event in events:
            evt_type = event["type"]
            evt_data = event["data"]

            tasks = []
            matched = 0
            for conn in self.connections:
                if conn.matches_event(evt_type):
                    matched += 1
                    tasks.append(conn.send_push(evt_type, evt_data))
                else:
                    logger.debug("  agent %s 未订阅事件 %s", conn.addr, evt_type)

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info("事件推送: %s → %d/%d个agent (已订阅)", evt_type, matched, after)
            else:
                logger.info("事件 %s: 无agent订阅此类型", evt_type)

        self.connections = [c for c in self.connections if not c.is_closed]

    async def _push_webhooks(self, events):
        try:
            import requests as req
            wh_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'webhooks.json')
            if not os.path.exists(wh_file):
                return
            with open(wh_file, 'r') as f:
                hooks = json.loads(f.read() or '[]')
            for hook in hooks:
                hook_events = hook.get('events', [])
                url = hook.get('url', '')
                if not url:
                    continue
                for event in events:
                    if not hook_events or event['type'] in hook_events:
                        try:
                            req.post(url, json=event, timeout=5,
                                     headers={'Content-Type': 'application/json'})
                            logger.info('Webhook推送: %s -> %s', event['type'], url)
                        except Exception as e:
                            logger.debug('Webhook推送失败 %s: %s', url, e)
        except Exception as e:
            logger.debug('Webhook推送异常: %s', e)

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client,
            host="0.0.0.0",
            port=self.port,
        )
        logger.info("Agent TCP服务已启动: :%d", self.port)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for conn in self.connections:
            conn.close()
        self.connections.clear()
        logger.info("Agent TCP服务已停止")