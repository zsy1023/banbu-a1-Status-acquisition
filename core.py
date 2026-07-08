"""
核心引擎 — 状态管理 + 事件系统 + 推送调度

负责：
  1. 定时从MQTT拉取打印机状态
  2. 检测事件（层到达/错误/完成/温度阈值）
  3. 通过回调推送事件给agent_tcp
"""
import time
import logging
import secrets
import os
import json
from datetime import datetime

import requests

import mqtt_client
import auth
from notification import notify

_COLOR_NAMES = {
    '000000': '黑色', 'FFFFFF': '白色', 'FF0000': '红色', '00FF00': '绿色',
    '0000FF': '蓝色', 'FFFF00': '黄色', 'FF00FF': '紫色', '00FFFF': '青色',
    'FFA500': '橙色', 'FFC0CB': '粉色', '800080': '紫色', '808080': '灰色',
    'C0C0C0': '银色', 'A52A2A': '棕色', 'BCBCBC': '灰色', 'F5F5F5': '浅灰',
    'E8E8E8': '浅灰', 'D3D3D3': '浅灰', 'BEBEBE': '深灰', '696969': '深灰',
    'ADD8E6': '浅蓝', '87CEEB': '天蓝', '98FB98': '浅绿', 'FFB6C1': '浅粉',
    'FFD700': '金色', 'E6E6FA': '淡紫', 'F0E68C': '卡其',
}


def _color_name(hex_color):
    h = hex_color.lstrip('#').upper()[:6]
    return _COLOR_NAMES.get(h, f'#{h}')


def _fan_to_pct(v):
    """风扇原始值(0-14)转百分比"""
    try:
        n = int(v)
        return f"{n * 100 // 14}%"
    except (ValueError, TypeError):
        return str(v)


logger = logging.getLogger("printer.core")


def hex_to_rgb(hex_color):
    """HEX颜色转RGB元组"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) >= 6:
        return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))
    return (0, 0, 0)


class EventEngine:
    """事件检测引擎"""

    @staticmethod
    def detect(old, new):
        """对比新旧状态，检测触发的事件列表

        Returns:
            list[dict]: 每个事件 {"type": str, "data": dict}
        """
        events = []

        if not new:
            return events

        old_state = old.get("gcode_state", "") if old else ""
        new_state = new.get("gcode_state", "")

        new_layer = new.get("layer_num", 0)
        old_layer = old.get("layer_num", 0) if old else 0
        if new_layer != old_layer and new_layer > 0:
            events.append({
                "type": "layer",
                "data": {
                    "layer": new_layer,
                    "total_layers": new.get("total_layer_num", 0),
                    "percent": new.get("mc_percent", 0),
                }
            })

        if new_state == "FINISH" and old_state != "FINISH":
            events.append({
                "type": "complete",
                "data": {
                    "task_name": new.get("subtask_name", ""),
                    "total_time": new.get("mc_print_total_time", 0),
                    "percent": 100,
                }
            })

        if new_state == "FAILED" and old_state != "FAILED":
            events.append({
                "type": "error",
                "data": {
                    "state": "FAILED",
                }
            })

        if new_state == "PAUSE" and old_state != "PAUSE" and old_state == "RUNNING":
            events.append({
                "type": "paused",
                "data": {
                    "percent": new.get("mc_percent", 0),
                    "layer": new_layer,
                }
            })

        if new_state == "RUNNING" and old_state != "RUNNING":
            events.append({
                "type": "printing_started",
                "data": {
                    "task_name": new.get("subtask_name", ""),
                }
            })

        return events


class ConfirmManager:
    """两步确认管理器 — 控制指令先返回 confirm_id，确认后才执行"""
    CONFIRM_TIMEOUT = 30

    def __init__(self):
        self._pending = {}

    def create(self, action: str, params: dict = None) -> dict:
        confirm_id = secrets.token_hex(16)
        self._pending[confirm_id] = {
            "action": action,
            "params": params or {},
            "expires": time.time() + self.CONFIRM_TIMEOUT,
        }
        logger.info("确认已创建: %s → %s", confirm_id[:8], action)
        return {
            "ok": True,
            "confirm_id": confirm_id,
            "expires_in": self.CONFIRM_TIMEOUT,
        }

    def confirm(self, confirm_id: str) -> dict:
        entry = self._pending.pop(confirm_id, None)
        if not entry:
            return {"ok": False, "error": "确认ID无效"}
        if time.time() > entry["expires"]:
            return {"ok": False, "error": "确认已过期，请重新发起请求"}
        logger.info("确认已通过: %s → %s", confirm_id[:8], entry["action"])
        return {
            "ok": True,
            "action": entry["action"],
            "params": entry["params"],
        }

    def cleanup(self):
        now = time.time()
        expired = [k for k, v in self._pending.items() if v["expires"] < now]
        for k in expired:
            del self._pending[k]
        if expired:
            logger.info("已清理 %d 条过期确认", len(expired))


class NotifyRuleManager:
    """通知规则引擎 — 自定义触发条件"""
    TRIGGER_TYPES = [
        {"id": "print_duration", "label": "打印持续多久后提醒", "unit": "分钟", "placeholder": "60"},
        {"id": "layer_reached", "label": "打印到第几层时提醒", "unit": "层", "placeholder": "50"},
        {"id": "bed_temp_above", "label": "热床温度超过提醒", "unit": "°C", "placeholder": "60"},
        {"id": "bed_temp_below", "label": "热床温度低于提醒", "unit": "°C", "placeholder": "30"},
        {"id": "nozzle_temp_above", "label": "喷头温度超过提醒", "unit": "°C", "placeholder": "200"},
        {"id": "nozzle_temp_below", "label": "喷头温度低于提醒", "unit": "°C", "placeholder": "40"},
        {"id": "print_remaining", "label": "剩余多久打印完成时提醒", "unit": "分钟", "placeholder": "10"},
        {"id": "temp_in_range", "label": "温度进入指定区间提醒", "unit": "°C", "placeholder": "30-40"},
    ]
    BUILTIN = [
        {"id": "_print_complete", "type": "print_complete", "value": 0, "enabled": True, "label": "✅ 打印完成", "builtin": True},
        {"id": "_print_error", "type": "print_error", "value": 0, "enabled": True, "label": "❌ 打印出错", "builtin": True},
        {"id": "_print_paused", "type": "print_paused", "value": 0, "enabled": True, "label": "⏸️ 打印暂停", "builtin": True},
    ]

    def __init__(self, data_dir):
        self.file = os.path.join(data_dir, "notify_rules.json")
        self._rules = self._load()
        self._triggered = set()

    def _load(self):
        if os.path.exists(self.file):
            try:
                with open(self.file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return []

    def save(self):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(self._rules, f, ensure_ascii=False, indent=2)

    def get_all(self):
        return self.BUILTIN + self._rules

    def set_rules(self, rules):
        self._rules = [r for r in rules if not r.get("builtin")]
        self.save()

    def check(self, old_status, new_status, events):
        triggered = []
        for rule in self.get_all():
            if not rule.get("enabled", True):
                continue
            rid = rule["id"]
            if rid in self._triggered:
                continue
            if self._match(rule, old_status, new_status, events):
                self._triggered.add(rid)
                triggered.append(rule)
        return triggered

    def _match(self, rule, old, new, events):
        t = rule["type"]
        v = rule.get("value", 0)
        evt_types = [e["type"] for e in events]

        if t == "print_complete":
            return "complete" in evt_types
        if t == "print_error":
            return "error" in evt_types
        if t == "print_paused":
            return "paused" in evt_types
        if not old or not new:
            return False

        if t == "layer_reached":
            return old.get("layer_num", 0) < v <= new.get("layer_num", 0)
        if t == "bed_temp_above":
            return old.get("bed_temper", 0) <= v < new.get("bed_temper", 0)
        if t == "bed_temp_below":
            return old.get("bed_temper", 0) >= v > new.get("bed_temper", 0)
        if t == "nozzle_temp_above":
            return old.get("nozzle_temper", 0) <= v < new.get("nozzle_temper", 0)
        if t == "nozzle_temp_below":
            return old.get("nozzle_temper", 0) >= v > new.get("nozzle_temper", 0)
        if t == "temp_in_range":
            v2 = rule.get("value2", v + 10)
            old_t = old.get("bed_temper", old.get("nozzle_temper", 0))
            new_t = new.get("bed_temper", new.get("nozzle_temper", 0))
            outside_before = old_t < v or old_t > v2
            inside_now = v <= new_t <= v2
            return outside_before and inside_now
        if t == "print_remaining":
            old_r = old.get("mc_remaining_time", 9999)
            new_r = new.get("mc_remaining_time", 9999)
            return old_r > v >= new_r
        if t == "print_duration":
            old_e = old.get("mc_print_time", 0)
            new_e = new.get("mc_print_time", 0)
            return old_e < v <= new_e
        return False

    def reset_triggered(self):
        self._triggered.clear()


class PrinterCore:
    """打印机核心引擎"""

    def __init__(self, config, token_file, on_event_callback=None):
        self.config = config
        self.token_file = token_file
        self.on_event_callback = on_event_callback

        self.phone = config["phone"]
        self.serial = config["serial"]
        self.refresh_interval = config.get("refresh_interval", 30)
        self.mqtt_timeout = config.get("mqtt_timeout", 15)
        self.mqtt_retries = config.get("mqtt_retries", 2)

        self.token = None
        self.username = None
        self.login_required = True

        self.last_status = {}
        self.last_task = {}
        self.last_refresh = 0.0
        self.confirm_manager = ConfirmManager()
        self._loop = None

        self.dashboard_url = f"https://localhost:{config.get('web_port', 18443)}"

        self.data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        self.notify_rules = NotifyRuleManager(self.data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.temp_history_file = os.path.join(self.data_dir, "temp_history.json")
        self.print_history_file = os.path.join(self.data_dir, "print_history.json")
        self._temp_history = self._load_json(self.temp_history_file)
        self._print_history = self._load_json(self.print_history_file)
        self._current_print = None

    @staticmethod
    def _load_json(path):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return []
        return []

    @staticmethod
    def _save_json(path, data):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def is_logged_in(self):
        return not self.login_required

    def try_auto_login(self):
        token_str = auth.load_token(self.token_file)
        if not token_str:
            logger.warning("未找到缓存的token")
            return False

        if not auth.test_token(token_str):
            logger.warning("缓存的token已失效")
            return False

        username = auth.get_username(token_str)
        if not username:
            logger.warning("获取用户名失败")
            return False

        self.token = token_str
        self.username = username
        self.login_required = False
        logger.info("自动登录成功: %s", username)
        return True

    def do_login(self, code):
        result = auth.login_with_code(self.phone, code, self.token_file)
        if not result.get("ok"):
            return result

        token = result["token"]
        username = auth.get_username(token)
        if not username:
            return {"ok": False, "error": "获取用户名失败"}

        self.token = token
        self.username = username
        self.login_required = False
        return {"ok": True, "token": token, "username": username}

    def do_send_sms(self):
        return auth.send_sms(self.phone)

    def do_refresh(self):
        if self.login_required:
            return {"ok": False, "error": "需要先登录"}

        logger.info("正在从MQTT获取状态...")
        data = mqtt_client.fetch_printer_status(
            self.token, self.username, self.serial,
            timeout=self.mqtt_timeout,
            retries=self.mqtt_retries,
        )
        if not data:
            logger.warning("MQTT未返回数据")
            return {"ok": False, "error": "MQTT未返回数据"}

        old_status = self.last_status
        events = EventEngine.detect(old_status, data)

        task_id = data.get("task_id", "") or data.get("subtask_id", "")
        task_detail = self._get_task_detail(task_id) if task_id else {}

        self.last_status = data
        self.last_task = task_detail
        self.last_refresh = time.time()

        logger.info("状态更新成功 | 状态: %s | 进度: %d%%",
                    data.get("gcode_state", "?"), data.get("mc_percent", 0))

        self._record_temperature(data)

        for evt in events:
            self._handle_print_event(evt, data)
            if evt["type"] == "printing_started":
                self.notify_rules.reset_triggered()

        rule_hits = self.notify_rules.check(old_status, data, events)
        for rule in rule_hits:
            msg = rule["label"]
            val = rule.get("value", 0)
            if val:
                if rule["type"] == "layer_reached":
                    msg = f"层 {val}"
                elif "temp" in rule["type"]:
                    msg = f"{val}°C {'超过' if 'above' in rule['type'] else '低于'}阈值"
                elif rule["type"] == "temp_in_range":
                    v2 = rule.get("value2", val + 10)
                    msg = f"温度进入 {val}~{v2}°C 区间"
                elif "remaining" in rule["type"]:
                    msg = f"剩余 {val} 分钟"
                elif "duration" in rule["type"]:
                    msg = f"已打印 {val} 分钟"
            task_name = data.get("subtask_name", "") or self.last_task.get("name", "")
            notify("🔔 " + msg, task_name, on_click=self.dashboard_url)
            events.append({
                "type": rule["type"],
                "data": {"message": msg, "task_name": task_name, "value": val}
            })
            logger.info("通知规则触发: %s | 事件已加入推送队列", rule["type"])

        if events:
            logger.info("本轮共 %d 个事件待推送: %s", len(events), [e["type"] for e in events])
        if events and self.on_event_callback:
            logger.info("回调函数存在，准备推送到 Agent TCP...")
            import asyncio
            if self._loop and not self._loop.is_closed():
                logger.info("事件循环可用，提交推送任务...")
                asyncio.run_coroutine_threadsafe(
                    self._dispatch_events(events), self._loop
                )
            else:
                logger.warning("事件循环不可用，跳过推送")
        else:
            if not events:
                logger.debug("无事件，跳过推送")
            if not self.on_event_callback:
                logger.warning("on_event_callback 为空，推送不会执行")

        return {"ok": True}

    def _send_mqtt_command(self, payload: dict):
        return mqtt_client.send_command(
            self.token, self.username, self.serial, payload
        )

    def control_light(self, target_mode: str = None):
        if self.login_required:
            return {"ok": False, "error": "需要先登录"}

        lights = self.last_status.get("lights_report", [{}])
        current = lights[0].get("mode", "off") if lights else "off"
        expected = target_mode or ("on" if current == "off" else "off")

        payload = {
            "system": {
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": expected,
            }
        }
        ok = self._send_mqtt_command(payload)
        if not ok:
            return {"ok": False, "error": "MQTT发送失败"}

        logger.info("指令已发，等待打印机响应并确认状态...")
        for attempt in range(3):
            time.sleep(1.5)
            self.do_refresh()
            new_lights = self.last_status.get("lights_report", [{}])
            actual = new_lights[0].get("mode", "off") if new_lights else "off"
            if actual == expected:
                logger.info("灯光状态确认一致: %s", actual)
                return {"ok": True, "light_mode": actual}
            logger.info("状态尚未更新(当前=%s 期望=%s)，重试...", actual, expected)

        actual = self.last_status.get("lights_report", [{}])
        actual_mode = actual[0].get("mode", "off") if actual else "off"
        logger.warning("灯光状态最终: 期望=%s 实际=%s", expected, actual_mode)
        return {"ok": True, "light_mode": actual_mode}

    def control_request(self, action: str, params: dict = None):
        if action == "light_toggle":
            lights = self.last_status.get("lights_report", [{}])
            current = lights[0].get("mode", "off") if lights else "off"
            target = "on" if current == "off" else "off"
            preview = {"current_light": current, "will_change_to": target}
            message = f"确认{'打开' if target == 'on' else '关闭'}灯光？"

        else:
            return {"ok": False, "error": f"不支持的操作: {action}"}

        info = self.confirm_manager.create(action, params)
        info["message"] = message
        info["preview"] = preview
        info["status"] = "pending"
        return info

    def control_confirm(self, confirm_id: str):
        result = self.confirm_manager.confirm(confirm_id)
        if not result["ok"]:
            return result

        action = result["action"]
        params = result["params"] or {}

        if action == "light_toggle":
            r = self.control_light()
            if r["ok"]:
                return {"ok": True, "status": "executed", "result": {"light_mode": r["light_mode"]}}
            return r

        return {"ok": False, "error": f"未知操作: {action}"}

    def _record_temperature(self, data):
        now = datetime.now().isoformat()
        entry = {
            "time": now,
            "nozzle": round(data.get("nozzle_temper", 0), 2),
            "bed": round(data.get("bed_temper", 0), 2),
            "chamber": round(data.get("chamber_temper", 0), 2),
        }
        self._temp_history.append(entry)
        if len(self._temp_history) > 360:
            self._temp_history = self._temp_history[-360:]
        self._save_json(self.temp_history_file, self._temp_history)

    def get_temp_history(self, minutes=60):
        if not self._temp_history:
            return []
        cutoff = datetime.now().timestamp() - minutes * 60
        result = []
        for entry in self._temp_history:
            try:
                t = datetime.fromisoformat(entry["time"]).timestamp()
                if t >= cutoff:
                    result.append(entry)
            except (ValueError, TypeError):
                continue
        return result

    def _handle_print_event(self, event, data):
        evt_type = event["type"]
        evt_data = event["data"]

        if evt_type == "printing_started":
            self._current_print = {
                "task_name": evt_data.get("task_name", ""),
                "start_time": datetime.now().isoformat(),
            }
            logger.info("📝 打印开始: %s", self._current_print["task_name"])

        elif evt_type == "complete":
            if self._current_print:
                record = {
                    **self._current_print,
                    "end_time": datetime.now().isoformat(),
                    "status": "completed",
                    "total_time_min": evt_data.get("total_time", 0),
                }
                self._print_history.append(record)
                self._save_json(self.print_history_file, self._print_history)
                self._current_print = None
                logger.info("📝 打印完成: %s", record["task_name"])
            notify("🖨️ 打印完成", f"{evt_data.get('task_name','')} 已完成", on_click=self.dashboard_url)

        elif evt_type == "error":
            if self._current_print:
                record = {
                    **self._current_print,
                    "end_time": datetime.now().isoformat(),
                    "status": "failed",
                }
                self._print_history.append(record)
                self._save_json(self.print_history_file, self._print_history)
                self._current_print = None
            notify("❌ 打印失败", "打印机出现错误，请检查", on_click=self.dashboard_url)

        elif evt_type == "paused":
            name = data.get("subtask_name", "") or (self._current_print or {}).get("task_name", "")
            notify("⏸️ 打印暂停", name or "打印机已暂停", on_click=self.dashboard_url)

    def get_print_history(self, limit=50):
        return list(reversed(self._print_history[-limit:]))

    def get_notify_rules(self):
        return self.notify_rules.get_all()

    def set_notify_rules(self, rules):
        self.notify_rules.set_rules(rules)
        self.notify_rules.save()

    async def _dispatch_events(self, events):
        if self.on_event_callback:
            await self.on_event_callback(events)

    def _get_task_detail(self, task_id):
        if not task_id or not self.token:
            return {}
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            resp = requests.get(
                f"https://api.bambulab.cn/v1/user-service/my/tasks",
                headers=headers, timeout=10, verify=False
            )
            if resp.status_code == 200:
                tasks = resp.json().get("tasks", [])
                for task in tasks:
                    if task.get("id") == task_id or task.get("task_id") == task_id:
                        return {
                            "weight": task.get("weight", 0),
                            "total_time": task.get("total_time", 0),
                            "cover": task.get("cover", "") or task.get("cover_url", ""),
                            "name": task.get("name", ""),
                        }
        except Exception:
            pass
        return {}

    def get_formatted_status(self):
        data = self.last_status
        task = self.last_task

        if not data:
            return {"connected": False, "error": "无数据"}

        state_map = {
            "IDLE": "idle", "RUNNING": "printing", "PAUSE": "paused",
            "FINISH": "finished", "FAILED": "failed",
        }
        state = data.get("gcode_state", "UNKNOWN")

        vtray_data = data.get("vtray", {})
        vtray_info = None
        if vtray_data and vtray_data.get("tray", []):
            t = vtray_data["tray"][0]
            vc = t.get("tray_color", "000000FF")[:6]
            vtray_info = {
                "type": t.get("tray_type", "外挂"),
                "color": f"#{vc}",
                "color_name": _color_name(vc),
                "remain": t.get("tray_remain", 0),
            }

        ams_list = [{"id": "外挂", "humidity": "N/A", "trays": [vtray_info]}] if vtray_info else []
        ams = data.get("ams", {})
        if ams and "ams" in ams:
            for unit in ams["ams"]:
                trays = []
                for tray in unit.get("tray", []):
                    color_hex = tray.get("tray_color", "000000FF")[:6]
                    trays.append({
                        "id": tray.get("id"),
                        "type": tray.get("tray_type", "未知"),
                        "color": f"#{color_hex}",
                        "rgb": list(hex_to_rgb(color_hex)),
                        "remain": tray.get("tray_remain", 0),
                        "color_name": _color_name(color_hex),
                    })
                ams_list.append({
                    "id": unit.get("id"),
                    "humidity": unit.get("humidity", "N/A"),
                    "trays": trays,
                })

        lights = data.get("lights_report", [{}])
        return {
            "connected": True,
            "serial": self.serial,
            "state": state_map.get(state, state.lower()),
            "state_raw": state,
            "progress": {
                "percent": data.get("mc_percent", 0),
                "layer": data.get("layer_num", 0),
                "total_layers": data.get("total_layer_num", 0),
            },
            "time": {
                "remaining_min": data.get("mc_remaining_time", 0),
                "total_min": task.get("total_time", 0) or data.get("mc_print_total_time", 0),
                "elapsed_min": data.get("mc_print_time", 0),
            },
            "temperature": {
                "nozzle": {
                    "current": round(data.get("nozzle_temper", 0), 2),
                    "target": data.get("nozzle_target_temper", 0),
                },
                "bed": {
                    "current": round(data.get("bed_temper", 0), 2),
                    "target": data.get("bed_target_temper", 0),
                },
                "chamber": round(data.get("chamber_temper", 0), 2),
            },
            "speed": data.get("spd_mag", 100),
            "fan": {
                "cooling": _fan_to_pct(data.get("cooling_fan_speed", "N/A")),
                "heatbreak": _fan_to_pct(data.get("heatbreak_fan_speed", "N/A")),
                "big_fan1": _fan_to_pct(data.get("big_fan1_speed", "N/A")),
                "big_fan2": _fan_to_pct(data.get("big_fan2_speed", "N/A")),
            },
            "task": {
                "name": data.get("subtask_name", "") or task.get("name", ""),
                "weight_g": task.get("weight", 0) or data.get("weight", 0),
                "cover_url": task.get("cover", "") or data.get("cover_url", "") or data.get("preview_img", ""),
            },
            "ams": ams_list,
            "hardware": {
                "wifi": data.get("wifi_signal", "N/A"),
                "nozzle_diameter": data.get("nozzle_diameter", "N/A"),
                "nozzle_type": data.get("nozzle_type", "N/A"),
                "light_mode": lights[0].get("mode", "N/A") if lights else "N/A",
            },
            "last_refresh": datetime.fromtimestamp(self.last_refresh).isoformat() if self.last_refresh else None,
        }