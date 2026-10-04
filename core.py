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


# 事件订阅列表中，每个订阅项的格式：
# {"events": [{"type":"layer", "value":50}, {"type":"error"}, ...]}
# 实际由agent_tcp管理，core只负责检测和回调


class EventEngine:
    """事件检测引擎"""

    # ── 暂停原因 ──────────────────────────────────
    # 原因分类：reason_key → 中文名。
    # 网页端「暂停自动重试」按 reason_key 勾选哪些原因需要自动重试。
    PAUSE_REASON_LABELS = {
        "filament_runout": "耗材用尽（断料）",
        "manual_pause": "用户手动暂停",
        "door_open": "前门被打开",
        "nozzle_temp": "喷嘴温控异常",
        "bed_temp": "热床温控异常",
        "chamber_temp": "腔体温控异常",
        "fan_speed": "热端风扇转速过低",
        "ams_lost": "AMS 连接丢失",
        "clog": "喷嘴堵塞",
        "debris": "喷嘴检测到异物覆盖",
        "cutter": "切刀异常",
        "first_layer": "首层检测异常",
        "lost_step": "丢步保护暂停",
        "m400": "M400 暂停指令",
        "gcode_pause": "G-code 指令暂停",
        "hms": "设备告警（HMS）",
        "device_error": "设备故障",
        "unknown": "未知原因",
    }

    # print.stg_cur（当前阶段码）→ reason_key。
    # 映射表依据 Bambu Lab 固件阶段定义，与 ha-bambulab 的 CURRENT_STAGE_IDS 一致。
    STAGE_PAUSE_REASONS = {
        5: "m400",
        6: "filament_runout",
        16: "manual_pause",
        17: "door_open",
        20: "nozzle_temp",
        21: "bed_temp",
        23: "lost_step",
        26: "ams_lost",
        27: "fan_speed",
        28: "chamber_temp",
        30: "gcode_pause",
        32: "debris",
        33: "cutter",
        34: "first_layer",
        35: "clog",
        # 255 / -1 是 idle 占位值，不代表暂停原因，交给 HMS / print_error 兜底
    }

    # print.print_error ≠ 0 时，按 HMS 子模块给出兜底原因
    HMS_MODULE_NAMES = {
        0x03: "主控板",
        0x05: "AMS",
        0x07: "工具头",
        0x0B: "摄像头",
        0x10: "HMS 系统",
        0x12: "AMS",
    }

    @staticmethod
    def _hms_module(error_code):
        """从 print_error 取值中解析故障模块名"""
        try:
            module = (int(error_code) >> 24) & 0xFF
        except (TypeError, ValueError):
            return None
        return EventEngine.HMS_MODULE_NAMES.get(module)

    @staticmethod
    def _hms_codes(data):
        """收集状态里的 HMS 错误码字符串，形如 ['0700-2200-0002-0001']"""
        codes = []
        for item in data.get("hms") or []:
            if not isinstance(item, dict):
                continue
            attr, code = item.get("attr"), item.get("code")
            if attr is None or code is None:
                continue
            try:
                codes.append(f"{int(attr):08X}-{int(code):08X}")
            except (TypeError, ValueError):
                continue
        return codes

    @staticmethod
    def pause_reason(data):
        """提取暂停原因

        优先级：阶段码(stg_cur) > HMS 错误码 > print_error 故障模块，
        取不到任何信息时返回 None。

        Returns:
            (reason: str|None, detail: dict)
            detail 含 reason_key（稳定标识，供自动重试匹配原因）
            以及 stage / hms / print_error，供事件与日志使用
        """
        detail = {}

        # 1) 阶段码最直接，能区分“用户手动暂停”和“断料”等
        stage = data.get("stg_cur")
        if stage is not None:
            try:
                stage = int(stage)
            except (TypeError, ValueError):
                stage = None
            if stage is not None:
                detail["stage"] = stage
                key = EventEngine.STAGE_PAUSE_REASONS.get(stage)
                if key:
                    detail["reason_key"] = key
                    return EventEngine.PAUSE_REASON_LABELS[key], detail

        # 2) HMS 错误码
        codes = EventEngine._hms_codes(data)
        if codes:
            detail["hms"] = codes
            detail["reason_key"] = "hms"
            return f"设备告警 {codes[0]}", detail

        # 3) print_error 故障模块兜底
        print_error = data.get("print_error")
        try:
            print_error = int(print_error)
        except (TypeError, ValueError):
            print_error = 0
        if print_error:
            detail["print_error"] = print_error
            detail["reason_key"] = "device_error"
            module = EventEngine._hms_module(print_error)
            code_hex = f"0x{print_error:08X}"
            if module:
                return f"{module}故障 ({code_hex})", detail
            return f"设备故障 ({code_hex})", detail

        detail["reason_key"] = "unknown"
        return None, detail

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

        # 新层到达 — layer_num 变了就触发
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

        # 打印完成 — 切换到 FINISH 状态
        if new_state == "FINISH" and old_state != "FINISH":
            events.append({
                "type": "complete",
                "data": {
                    "task_name": new.get("subtask_name", ""),
                    "total_time": new.get("mc_print_total_time", 0),
                    "percent": 100,
                }
            })

        # 错误 — 切换到 FAILED 状态
        if new_state == "FAILED" and old_state != "FAILED":
            events.append({
                "type": "error",
                "data": {
                    "state": "FAILED",
                }
            })

        # 打印暂停
        if new_state == "PAUSE" and old_state != "PAUSE" and old_state == "RUNNING":
            reason, detail = EventEngine.pause_reason(new)
            events.append({
                "type": "paused",
                "data": {
                    "percent": new.get("mc_percent", 0),
                    "layer": new_layer,
                    "task_name": new.get("subtask_name", ""),
                    "reason": reason,
                    **detail,
                }
            })

        # 开始打印
        if new_state == "RUNNING" and old_state != "RUNNING":
            events.append({
                "type": "printing_started",
                "data": {
                    "task_name": new.get("subtask_name", ""),
                }
            })

        return events


class ConfirmManager:
    """两步确认管理器 — 控制指令先返回 confirm_id，确认后才执行

    用法:
        cm = ConfirmManager()
        info = cm.create("light_toggle", {"target":"on"})
        # info.confirm_id → 给客户端
        result = cm.confirm(confirm_id)
        # result.ok → True/False
    """
    CONFIRM_TIMEOUT = 30  # 秒

    def __init__(self):
        self._pending = {}  # confirm_id -> {"action":str, "params":dict, "expires":float}

    def create(self, action: str, params: dict = None) -> dict:
        """创建一条待确认操作

        Returns:
            {"ok": True, "confirm_id": str, "expires_in": int}
        """
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
        """确认执行（从pending中取出并校验）

        Returns:
            {"ok": True, "action": str, "params": dict}
            or {"ok": False, "error": str}
        """
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
        """清理过期条目"""
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
    # 始终触发的内置规则
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
            except: pass
        return []

    def save(self):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(self._rules, f, ensure_ascii=False, indent=2)

    def get_all(self):
        """返回内置规则 + 自定义规则"""
        return self.BUILTIN + self._rules

    def set_rules(self, rules):
        """替换自定义规则（不含内置）"""
        self._rules = [r for r in rules if not r.get("builtin")]
        self.save()

    def check(self, old_status, new_status, events):
        """检查哪些规则被触发，返回触发的规则列表"""
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

    # 内置规则的弹窗标题（与通知规则引擎统一出口，避免与事件处理重复弹窗）
    BUILTIN_TITLES = {
        "print_complete": "🖨️ 打印完成",
        "print_error": "❌ 打印失败",
        "print_paused": "⏸️ 打印暂停",
    }

    @staticmethod
    def format_message(rule, events):
        """生成弹窗标题与正文

        Returns:
            (title, message, task_name) — 标题为空表示不弹窗
        """
        rtype = rule["type"]
        evt_by_type = {e["type"]: e.get("data", {}) for e in events}

        if rtype in NotifyRuleManager.BUILTIN_TITLES:
            title = NotifyRuleManager.BUILTIN_TITLES[rtype]
            etype = {"print_complete": "complete", "print_error": "error",
                     "print_paused": "paused"}[rtype]
            edata = evt_by_type.get(etype, {})
            task_name = (edata.get("task_name") or edata.get("name") or "").strip()
            if rtype == "print_complete":
                return title, (f"{task_name} 已完成" if task_name else "打印已完成"), task_name
            if rtype == "print_error":
                return title, "打印机出现错误，请检查", task_name
            # 暂停：优先展示暂停原因，其次任务名
            reason = (edata.get("reason") or "").strip()
            hint = ""
            ar = edata.get("auto_retry")
            if ar:
                hint = f" · {ar['delay']}秒后自动重试(第{ar['index']}/{ar['max']}次)"
            if reason:
                # 附带进度信息，便于判断停在哪儿
                pct = edata.get("percent")
                layer = edata.get("layer")
                extra = []
                if pct:
                    extra.append(f"{pct}%")
                if layer:
                    extra.append(f"第 {layer} 层")
                suffix = f"（{'，'.join(extra)}）" if extra else ""
                return title, f"{reason}{suffix}{hint}", task_name or reason
            return title, (task_name or "打印机已暂停") + hint, task_name

        # 自定义规则
        val = rule.get("value", 0)
        msg = rule["label"]
        if val:
            if rtype == "layer_reached":
                msg = f"层 {val}"
            elif "temp" in rtype:
                msg = f"{val}°C {'超过' if 'above' in rtype else '低于'}阈值"
            elif rtype == "temp_in_range":
                v2 = rule.get("value2", val + 10)
                msg = f"温度进入 {val}~{v2}°C 区间"
            elif "remaining" in rtype:
                msg = f"剩余 {val} 分钟"
            elif "duration" in rtype:
                msg = f"已打印 {val} 分钟"
        return "🔔 " + msg, "", ""

    def _match(self, rule, old, new, events):
        t = rule["type"]
        v = rule.get("value", 0)
        evt_types = [e["type"] for e in events]

        if t == "print_complete": return "complete" in evt_types
        if t == "print_error": return "error" in evt_types
        if t == "print_paused": return "paused" in evt_types
        if not old or not new: return False

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
            # 从 value2 读取区间上限
            v2 = rule.get("value2", v + 10)
            old_t = old.get("bed_temper", old.get("nozzle_temper", 0))
            new_t = new.get("bed_temper", new.get("nozzle_temper", 0))
            # 进入区间: 之前在外面，现在在里面
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


class RetryManager:
    """暂停自动重试 — 检测到暂停后按配置自动发送 resume 指令

    配置持久化在 data/retry_rules.json：
        enabled      是否启用自动重试（默认关闭）
        max_retries  同一打印任务最多自动重试次数
        retry_delay  每次重试前等待秒数（留出处理时间，如换料、关门）
        reasons      触发自动重试的暂停原因 key 列表（见 EventEngine.PAUSE_REASON_LABELS）

    默认关闭：自动恢复是对打印机的物理动作，需用户在网页端显式开启。
    """

    DEFAULT_CONFIG = {
        "enabled": False,
        "max_retries": 3,
        "retry_delay": 60,
        # 默认可自动恢复的原因：多为“处理完即可继续”的场景，不含用户手动暂停
        "reasons": ["filament_runout", "door_open", "ams_lost", "m400",
                    "gcode_pause", "lost_step"],
    }
    MAX_RETRY_LIMIT = 10          # 次数上限，避免误填导致长时间反复自动恢复
    MIN_DELAY, MAX_DELAY = 10, 1800

    def __init__(self, data_dir):
        self.file = os.path.join(data_dir, "retry_rules.json")
        self._config = self._load()
        self.reset_session()

    # ── 配置 ─────────────────────────────────────

    def _load(self):
        cfg = dict(self.DEFAULT_CONFIG)
        if os.path.exists(self.file):
            try:
                with open(self.file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
            except (json.JSONDecodeError, OSError):
                logger.warning("retry_rules.json 读取失败，使用默认配置")
        return self.normalize(cfg)

    @classmethod
    def normalize(cls, cfg):
        """规范化配置，容忍网页端传来的脏数据"""
        out = dict(cls.DEFAULT_CONFIG)
        v = cfg.get("enabled", False)
        if isinstance(v, str):
            # 手工编辑过的配置文件可能写成 "false"/"0"
            out["enabled"] = v.strip().lower() in ("1", "true", "yes", "on")
        else:
            out["enabled"] = bool(v)
        try:
            n = int(cfg.get("max_retries", cls.DEFAULT_CONFIG["max_retries"]))
            out["max_retries"] = max(1, min(cls.MAX_RETRY_LIMIT, n))
        except (TypeError, ValueError):
            out["max_retries"] = cls.DEFAULT_CONFIG["max_retries"]
        try:
            d = int(cfg.get("retry_delay", cls.DEFAULT_CONFIG["retry_delay"]))
            out["retry_delay"] = max(cls.MIN_DELAY, min(cls.MAX_DELAY, d))
        except (TypeError, ValueError):
            out["retry_delay"] = cls.DEFAULT_CONFIG["retry_delay"]

        reasons = cfg.get("reasons")
        if not isinstance(reasons, list):
            reasons = cls.DEFAULT_CONFIG["reasons"]
        out["reasons"] = [r for r in reasons if r in EventEngine.PAUSE_REASON_LABELS]
        return out

    def save(self):
        # enabled 属于用户显式开关，不在 normalize 里改；只落盘
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)

    def get_config(self):
        return dict(self._config)

    def set_config(self, cfg):
        self._config = self.normalize(cfg or {})
        self.save()
        logger.info("暂停重试配置已更新: 启用=%s 次数=%d 间隔=%ds 原因=%s",
                    self._config["enabled"], self._config["max_retries"],
                    self._config["retry_delay"], self._config["reasons"])
        return self.get_config()

    @staticmethod
    def reason_options():
        """供网页端勾选的原因列表"""
        return [{"id": k, "label": v}
                for k, v in EventEngine.PAUSE_REASON_LABELS.items()]

    # ── 会话状态 ─────────────────────────────────

    def reset_session(self):
        """重置会话状态（新打印任务开始时调用）"""
        self._clear_session_counters()
        self._task_name = None    # 当前任务名，用于识别任务切换

    def on_print_started(self, task_name=None, prev_state=None):
        """判断是否属于“新任务”，是则重置重试计数

        只有真正的新任务才清零计数。关键区分：
          - PAUSE → RUNNING 是自动重试成功恢复，属于同一任务，不能清零，
            否则「最多重试 N 次」会失效（每次恢复都能再重试 N 次）
          - IDLE/FINISH/FAILED → RUNNING 是新任务，清零
          - 任务名变化也是新任务，清零
        """
        is_new = False
        if prev_state in ("IDLE", "FINISH", "FAILED", ""):
            is_new = True
        if task_name and self._task_name and task_name != self._task_name:
            is_new = True

        if is_new:
            if self.count or self._given_up:
                logger.info("新打印任务(%s，上一状态 %s)，自动重试计数已重置",
                            task_name or "未命名", prev_state or "未知")
            self._clear_session_counters()
            self._task_name = task_name or ""
            return True

        # 同一任务内回到 RUNNING：保留计数
        if not self._task_name:
            self._task_name = task_name or ""
        return False

    def _clear_session_counters(self):
        self.count = 0
        self._waiting = False
        self._timer_start = 0.0
        self._reason_key = None
        self._reason_text = None
        self._given_up = False
        self._last_ok = None

    def is_waiting(self):
        return self._waiting

    def register_pause(self, reason_key, reason_text):
        """登记一次暂停，判断是否安排自动重试

        Returns:
            {"scheduled": bool, "index": int|None, "max": int, "delay": int,
             "reason": str, "skip": str|None}
            skip 为不安排重试的原因（用于日志）
        """
        cfg = self._config
        self._reason_key = reason_key
        self._reason_text = reason_text
        info = {"scheduled": False, "index": None, "max": cfg["max_retries"],
                "delay": cfg["retry_delay"], "reason": reason_text, "skip": None}

        if not cfg["enabled"]:
            info["skip"] = "自动重试未启用"
            return info
        if self.count >= cfg["max_retries"]:
            self._given_up = True
            info["skip"] = f"已达最大重试次数({cfg['max_retries']})"
            return info
        if reason_key not in cfg["reasons"]:
            info["skip"] = f"暂停原因“{reason_text}”未勾选自动重试"
            return info

        if not self._waiting:
            self._waiting = True
            self._timer_start = time.time()
        info["scheduled"] = True
        info["index"] = self.count + 1
        return info

    def due(self, now=None):
        """是否已到发送 resume 的时刻"""
        if not self._waiting:
            return False
        now = time.time() if now is None else now
        return (now - self._timer_start) >= self._config["retry_delay"]

    def remaining(self, now=None):
        """距离下次重试还剩多少秒"""
        if not self._waiting:
            return 0
        now = time.time() if now is None else now
        return max(0, int(self._config["retry_delay"] - (now - self._timer_start)))

    def mark_retry_sent(self, ok):
        """记录一次重试已发送

        发送失败同样计数（MQTT 不通时避免每轮刷新都重发）。
        """
        self.count += 1
        self._last_ok = bool(ok)
        self._timer_start = time.time()   # 重新计时：未恢复则等待后再试
        exhausted = self.count >= self._config["max_retries"]
        if exhausted:
            self._given_up = True
            # 关键：停止等待，否则每轮刷新都会再次发送 resume
            self._waiting = False
        return {"index": self.count, "max": self._config["max_retries"],
                "ok": bool(ok), "exhausted": exhausted}

    def clear(self, why=""):
        """打印已不处于暂停状态时清理等待态（计数保留，避免反复暂停-恢复循环）"""
        if self._waiting:
            logger.info("自动重试等待结束: %s", why or "打印已恢复")
        self._waiting = False
        self._timer_start = 0.0

    def status(self):
        """运行时状态，供网页端显示"""
        return {
            "enabled": self._config["enabled"],
            "count": self.count,
            "max": self._config["max_retries"],
            "waiting": self._waiting,
            "remaining": self.remaining(),
            "reason": self._reason_text,
            "reason_key": self._reason_key,
            "given_up": self._given_up,
            "last_ok": self._last_ok,
        }


class PrinterCore:
    """打印机核心引擎"""

    def __init__(self, config, token_file, on_event_callback=None):
        """
        Args:
            config: config.json 内容
            token_file: token.txt 路径
            on_event_callback: async function(event, event_filters)
                当事件触发时调用，用于向agent推送
        """
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
        self.login_url = self.dashboard_url + "/?login=1"
        # 自动登录检测结果: unknown / ok / expired / missing / error
        self.login_state = "unknown"

        # ── 数据存储 ──
        self.data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        self.notify_rules = NotifyRuleManager(self.data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.retry_manager = RetryManager(self.data_dir)
        self.temp_history_file = os.path.join(self.data_dir, "temp_history.json")
        self.print_history_file = os.path.join(self.data_dir, "print_history.json")
        self._temp_history = self._load_json(self.temp_history_file)
        self._print_history = self._load_json(self.print_history_file)
        self._current_print = None  # 当前打印会话

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
        """尝试用缓存的token自动登录

        失败时会把原因写入 self.login_state（missing/expired/error），
        供启动流程判断是否需要弹窗提醒更新 token。
        """
        token_str = auth.load_token(self.token_file)
        if not token_str:
            logger.warning("未找到缓存的token")
            self.login_state = "missing"
            return False

        if not auth.test_token(token_str):
            logger.warning("缓存的token已失效")
            self.login_state = "expired"
            return False

        username = auth.get_username(token_str)
        if not username:
            logger.warning("获取用户名失败")
            self.login_state = "error"
            return False

        self.token = token_str
        self.username = username
        self.login_required = False
        self.login_state = "ok"
        logger.info("自动登录成功: %s", username)
        return True

    def do_login(self, code):
        """用验证码登录"""
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

    def do_send_sms(self, phone=None):
        """发送验证码

        Args:
            phone: 可选，覆盖配置里的手机号（前端登录框可修改）
                   传空或传脱敏号（含 *）时，一律用 config.json 里的真实号码
        """
        p = (phone or "").strip()
        if not p or "*" in p:
            # 前端 /api/config 拿到的是脱敏号，不能拿去发短信
            p = self.phone
        return auth.send_sms(p)

    def do_refresh(self):
        """执行一次状态刷新（同步，应在executor中运行）"""
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

        # 检测事件
        old_status = self.last_status
        # 首次拉取只是建立基线：不能把“当前已处于 FINISH/FAILED/PAUSE”当成刚发生的事件，
        # 否则服务启动时会误报一次“打印完成”。但首轮若已在 RUNNING，仍要记录任务开始。
        first_poll = not old_status
        events = EventEngine.detect(old_status, data)
        if first_poll:
            events = [e for e in events
                      if e["type"] in ("printing_started", "layer")]

        # 获取任务详情
        task_id = data.get("task_id", "") or data.get("subtask_id", "")
        task_detail = self._get_task_detail(task_id) if task_id else {}

        # 更新缓存
        self.last_status = data
        self.last_task = task_detail
        self.last_refresh = time.time()

        logger.info("状态更新成功 | 状态: %s | 进度: %d%%",
                    data.get("gcode_state", "?"), data.get("mc_percent", 0))

        # ── 记录温度 ──
        self._record_temperature(data)

        # ── 处理打印历史 + 系统通知 ──
        for evt in events:
            self._handle_print_event(evt, data)
            if evt["type"] == "printing_started":
                self.notify_rules.reset_triggered()
                self.retry_manager.on_print_started(
                    evt["data"].get("task_name"), old_status.get("gcode_state", ""))

        # ── 暂停自动重试（在通知规则之前，便于把重试安排写进暂停弹窗）──
        events.extend(self._handle_auto_retry(events, data, first_poll))

        # ── 检查通知规则（内置 + 自定义）──
        # 这里是所有系统弹窗的唯一出口，_handle_print_event 不再单独 notify
        rule_hits = self.notify_rules.check(old_status, data, events)
        for rule in rule_hits:
            title, message, task_name = self.notify_rules.format_message(rule, events)
            if not task_name:
                task_name = data.get("subtask_name", "") or self.last_task.get("name", "")
            if message:
                # 内置规则：标题固定，正文为任务信息
                notify(title, message, on_click=self.dashboard_url)
            else:
                notify(title, task_name, on_click=self.dashboard_url)
            events.append({
                "type": rule["type"],
                "data": {"message": message or rule["label"],
                         "task_name": task_name,
                         "value": rule.get("value", 0)},
            })
            logger.info("通知规则触发: %s | 事件已加入推送队列", rule["type"])

        # 触发事件回调（推送到 Agent TCP）
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

    # ── 控制指令 ─────────────────────────────────

    def _send_mqtt_command(self, payload: dict):
        """底层：通过MQTT发指令"""
        return mqtt_client.send_command(
            self.token, self.username, self.serial, payload
        )

    def control_light(self, target_mode: str = None, wait: bool = True):
        """开关灯光

        Args:
            target_mode: "on" / "off"；None 则按当前状态自动取反
            wait: True  发指令后轮询确认真实状态（最多约 4.5 秒，原行为）
                  False 发完立即返回预期状态（前端按钮直连用，避免卡顿）
        Returns:
            {"ok": True, "light_mode": str}
            or {"ok": False, "error": str}
        """
        if self.login_required:
            return {"ok": False, "error": "需要先登录"}

        # 从当前缓存获取灯光状态来确定要切换的方向
        lights = self.last_status.get("lights_report", [{}])
        current = lights[0].get("mode", "off") if lights else "off"
        expected = target_mode or ("on" if current == "off" else "off")

        # 发MQTT指令
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

        if not wait:
            # 快速模式：不等打印机回报，先把预期状态同步到本地缓存，
            # 这样前端按钮能立刻切换到正确外观，下一次轮询会带回真实值。
            try:
                reports = self.last_status.setdefault("lights_report", [{}])
                if reports and isinstance(reports[0], dict):
                    reports[0]["mode"] = expected
                else:
                    self.last_status["lights_report"] = [{"mode": expected}]
            except Exception:
                pass
            logger.info("灯光指令已发送(快速模式): %s", expected)
            return {"ok": True, "light_mode": expected}

        # 等待打印机处理 + 多次尝试刷新确认
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

        # 最后一次尝试后，返回实际状态（可能是旧的）
        actual = self.last_status.get("lights_report", [{}])
        actual_mode = actual[0].get("mode", "off") if actual else "off"
        logger.warning("灯光状态最终: 期望=%s 实际=%s", expected, actual_mode)
        return {"ok": True, "light_mode": actual_mode}

    def control_resume(self):
        """恢复打印 — 暂停后继续执行当前任务

        Returns:
            {"ok": True} or {"ok": False, "error": str}
        """
        if self.login_required:
            return {"ok": False, "error": "需要先登录"}

        payload = {"print": {"sequence_id": "0", "command": "resume"}}
        if not self._send_mqtt_command(payload):
            return {"ok": False, "error": "MQTT发送失败"}
        return {"ok": True}

    # ── 暂停自动重试调度 ─────────────────────────

    def _handle_auto_retry(self, events, data, first_poll=False):
        """按配置调度暂停自动重试

        - 出现 paused 事件：登记并安排重试（等待 retry_delay 秒，把安排写回暂停事件）
        - 仍处于 PAUSE 且等待到点：发送 resume 指令，重试计数 +1
        - 已离开 PAUSE：清理等待态

        Returns:
            list[dict]: 需要推送给 Agent 的事件（最多一个 auto_retry 事件）
        """
        rm = self.retry_manager
        state = data.get("gcode_state", "")
        paused_evt = next((e for e in events if e["type"] == "paused"), None)

        # 刚发生暂停 → 登记，等待 retry_delay 后再重试
        if paused_evt:
            edata = paused_evt["data"]
            info = rm.register_pause(edata.get("reason_key") or "unknown",
                                     edata.get("reason"))
            if info["scheduled"]:
                edata["auto_retry"] = {
                    "index": info["index"], "max": info["max"], "delay": info["delay"],
                }
                logger.info("⏱️ 已安排自动重试: 第 %d/%d 次，%d 秒后发送 resume",
                            info["index"], info["max"], info["delay"])
            elif info["skip"]:
                logger.info("暂停未安排自动重试: %s", info["skip"])
            return []

        # 不在暂停状态 → 清理等待（重试计数保留到本轮打印结束）
        if state != "PAUSE":
            if rm.is_waiting():
                rm.clear("打印已恢复" if state == "RUNNING" else f"状态变为 {state}")
            return []

        # 服务启动时打印机已处于暂停：不替用户自动恢复
        if first_poll:
            if rm.get_config()["enabled"]:
                logger.info("启动时打印机已处于暂停，不自动重试（请手动处理或更新 token）")
            return []

        if not rm.due():
            if rm.is_waiting():
                logger.info("等待自动重试中: 还有 %d 秒", rm.remaining())
            return []

        # 到点 → 发送 resume
        result = self.control_resume()
        st = rm.mark_retry_sent(result["ok"])
        task_name = data.get("subtask_name", "") or self.last_task.get("name", "")

        if result["ok"]:
            logger.info("🔄 已自动重试(第 %d/%d 次): resume 指令已发送",
                        st["index"], st["max"])
            notify("🔄 已自动重试", f"第 {st['index']}/{st['max']} 次恢复打印",
                   on_click=self.dashboard_url)
        else:
            logger.warning("自动重试发送失败(第 %d/%d 次): %s",
                           st["index"], st["max"], result.get("error"))
            notify("⚠️ 自动重试失败", result.get("error") or "resume 指令发送失败",
                   on_click=self.dashboard_url)

        evt = {
            "type": "auto_retry",
            "data": {
                "index": st["index"],
                "max": st["max"],
                "ok": st["ok"],
                "exhausted": st["exhausted"],
                "reason": rm.status().get("reason"),
                "task_name": task_name,
            },
        }

        if st["exhausted"]:
            logger.warning("自动重试已达上限(%d 次)，停止自动重试", st["max"])
            notify("⚠️ 自动重试已达上限",
                   f"已重试 {st['max']} 次仍未恢复，请手动检查打印机",
                   on_click=self.dashboard_url)
            evt["data"]["stop"] = True

        return [evt]

    # ── 统一控制入口（两步确认）──────────────────

    def control_request(self, action: str, params: dict = None, direct: bool = False):
        """控制入口

        Args:
            action: light_on / light_off / light_toggle
            params: 附加参数
            direct: True  立即执行并返回结果（前端按钮直连，无需二次确认）
                    False 只返回待确认信息（保留原两步确认流程）

        Returns:
            direct=True  → {"ok": True, "status": "executed", "result": {...}}
            direct=False → {"ok": True, "confirm_id": str, "message": str,
                            "preview": dict, "status": "pending"}
        """
        # action → 目标灯光状态
        if action in ("light_on", "light_off", "light_toggle"):
            lights = self.last_status.get("lights_report", [{}])
            current = lights[0].get("mode", "off") if lights else "off"
            if action == "light_on":
                target = "on"
            elif action == "light_off":
                target = "off"
            else:
                target = "on" if current == "off" else "off"
        else:
            return {"ok": False, "error": f"不支持的操作: {action}"}

        if direct:
            # 一键直连：快速模式发送，不阻塞等待打印机回报
            r = self.control_light(target_mode=target, wait=False)
            if r.get("ok"):
                return {"ok": True, "status": "executed",
                        "result": {"light_mode": r.get("light_mode", target)}}
            return r

        info = self.confirm_manager.create(action, params)
        info["message"] = f"确认{'打开' if target == 'on' else '关闭'}灯光？"
        info["preview"] = {"current_light": current, "will_change_to": target}
        info["status"] = "pending"
        return info

    def control_confirm(self, confirm_id: str):
        """Step 2: 确认并执行控制指令

        Returns:
            {"ok": True, "status": "executed", "result": dict}
            or {"ok": False, "error": str}
        """
        result = self.confirm_manager.confirm(confirm_id)
        if not result["ok"]:
            return result

        action = result["action"]
        params = result["params"] or {}

        if action in ("light_on", "light_off", "light_toggle"):
            target = {"light_on": "on", "light_off": "off"}.get(action)
            r = self.control_light(target_mode=target)
            if r["ok"]:
                return {"ok": True, "status": "executed", "result": {"light_mode": r["light_mode"]}}
            return r

        return {"ok": False, "error": f"未知操作: {action}"}

    # ── 温度记录 ─────────────────────────────────

    def _record_temperature(self, data):
        """从状态数据中记录温度"""
        now = datetime.now().isoformat()
        entry = {
            "time": now,
            "nozzle": round(data.get("nozzle_temper", 0), 2),
            "bed": round(data.get("bed_temper", 0), 2),
            "chamber": round(data.get("chamber_temper", 0), 2),
        }
        self._temp_history.append(entry)
        # 最多保留 360 条（6小时 @ 60秒间隔）
        if len(self._temp_history) > 360:
            self._temp_history = self._temp_history[-360:]
        self._save_json(self.temp_history_file, self._temp_history)

    def get_temp_history(self, minutes=60):
        """返回最近 N 分钟的温度数据

        Returns:
            [{"time":str, "nozzle":float, "bed":float, "chamber":float}, ...]
        """
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

    # ── 打印历史 ─────────────────────────────────

    def _handle_print_event(self, event, data):
        """处理打印事件：记录历史 + 系统通知"""
        evt_type = event["type"]
        evt_data = event["data"]

        if evt_type == "printing_started":
            # 开始新打印
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
            # 系统弹窗由内置通知规则 _print_complete 统一发出（见 do_refresh），
            # 这里不再重复 notify，否则打印完成会弹两次

        elif evt_type == "error":
            # 打印失败时也保存记录
            if self._current_print:
                record = {
                    **self._current_print,
                    "end_time": datetime.now().isoformat(),
                    "status": "failed",
                }
                self._print_history.append(record)
                self._save_json(self.print_history_file, self._print_history)
                self._current_print = None
            # 同样交给内置规则 _print_error 发通知，避免重复弹窗

        elif evt_type == "paused":
            # 弹窗交给内置规则 _print_paused 统一发出，避免重复；这里只记录日志
            logger.info("⏸️ 打印暂停: %s | 进度 %s%% 第 %s 层",
                        evt_data.get("reason") or "未获取到暂停原因",
                        evt_data.get("percent", 0), evt_data.get("layer", 0))

    def get_print_history(self, limit=50):
        """获取打印历史记录

        Returns:
            [{"task_name":str, "start_time":str, "end_time":str,
              "status":str, "total_time_min":int}, ...]
        """
        return list(reversed(self._print_history[-limit:]))

    # ── 异步分发 ─────────────────────────────────

    def get_notify_rules(self):
        return self.notify_rules.get_all()

    def set_notify_rules(self, rules):
        self.notify_rules.set_rules(rules)
        self.notify_rules.save()

    # ── 暂停自动重试配置 ─────────────────────────

    def get_retry_rules(self):
        """暂停重试配置 + 可重试原因列表 + 当前运行状态"""
        return {
            "config": self.retry_manager.get_config(),
            "options": self.retry_manager.reason_options(),
            "status": self.retry_manager.status(),
        }

    def set_retry_rules(self, cfg):
        return self.retry_manager.set_config(cfg)

    async def _dispatch_events(self, events):
        """异步分发事件到回调"""
        if self.on_event_callback:
            await self.on_event_callback(events)

    def _get_task_detail(self, task_id):
        """从REST API获取任务详情"""
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
        """获取格式化后的状态JSON"""
        data = self.last_status
        task = self.last_task

        if not data:
            return {"connected": False, "error": "无数据"}

        state_map = {
            "IDLE": "idle", "RUNNING": "printing", "PAUSE": "paused",
            "FINISH": "finished", "FAILED": "failed",
        }
        state = data.get("gcode_state", "UNKNOWN")

        # 外挂耗材（不通过AMS）
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
            "auto_retry": self.retry_manager.status(),
        }
