"""MQTT通信模块 — 连接拓竹打印机获取状态，带重试"""
import json
import ssl
import time
import logging

import paho.mqtt.client as mqtt

logger = logging.getLogger("printer.mqtt")

BROKER = "cn.mqtt.bambulab.com"
PORT = 8883


def fetch_printer_status(token, username, serial, timeout=15, retries=2):
    """连接MQTT获取打印机状态，带超时和重试

    Args:
        token: 拓竹API accessToken
        username: MQTT用户名 (u_{uid})
        serial: 打印机序列号
        timeout: 单次等待超时（秒）
        retries: 失败重试次数

    Returns:
        dict: 打印机状态数据，失败返回{}
    """
    for attempt in range(retries + 1):
        if attempt > 0:
            logger.warning("MQTT重试第%d次...", attempt)
            time.sleep(3)

        result = _fetch_once(token, username, serial, timeout)
        if result:
            return result
        logger.warning("MQTT第%d次尝试未返回数据", attempt + 1)

    return {}


def send_command(token, username, serial, payload: dict):
    """向打印机发送MQTT控制指令（不等待响应）

    Args:
        token: 拓竹API accessToken
        username: MQTT用户名
        serial: 打印机序列号
        payload: 要发送的JSON指令，如 {"system":{"command":"ledctrl",...}}
    """
    sent = False

    def on_connect(client, userdata, flags, rc, properties=None):
        nonlocal sent
        if rc == 0:
            msg = json.dumps(payload)
            client.publish(f"device/{serial}/request", msg)
            logger.info("MQTT指令已发送: %s", msg[:200])
            sent = True
            client.disconnect()
        else:
            logger.error("MQTT连接失败: rc=%d", rc)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(username, password=token)
    client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.tls_insecure_set(True)
    client.on_connect = on_connect

    try:
        client.connect(BROKER, PORT, 60)
        start = time.time()
        while not sent and (time.time() - start) < 10:
            client.loop(timeout=1.0)
        client.disconnect()
    except Exception as e:
        logger.error("MQTT发送异常: %s", e)

    return sent


def _fetch_once(token, username, serial, timeout):
    """单次MQTT获取（阻塞）"""
    START_PUSH = {"pushing": {"sequence_id": "0", "command": "start"}}
    PUSH_ALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}

    result = {}

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(f"device/{serial}/report")
            client.publish(f"device/{serial}/request", json.dumps(START_PUSH))
            time.sleep(0.5)
            client.publish(f"device/{serial}/request", json.dumps(PUSH_ALL))
        else:
            logger.error("MQTT连接失败: rc=%d", rc)

    def on_message(client, userdata, msg):
        nonlocal result
        try:
            data = json.loads(msg.payload)
            if "print" in data and len(data["print"]) > 10:
                result = data["print"]
                client.disconnect()
        except json.JSONDecodeError:
            logger.warning("MQTT消息解析失败")
        except Exception:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(username, password=token)
    client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(BROKER, PORT, 60)
        start = time.time()
        while not result and (time.time() - start) < timeout:
            client.loop(timeout=1.0)
        client.disconnect()
    except Exception as e:
        logger.error("MQTT异常: %s", e)

    return result
