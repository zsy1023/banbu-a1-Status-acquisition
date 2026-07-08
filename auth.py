"""认证模块 — API key校验 + 拓竹token管理"""
import os
import json
import logging

import requests

logger = logging.getLogger("printer.auth")

API_URL = "https://api.bambulab.cn"


def load_config(config_path):
    """加载配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_apikey(config, apikey):
    """校验API key是否有效"""
    return apikey in config.get("api_keys", [])


def test_token(token):
    """测试拓竹token是否有效"""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(
            f"{API_URL}/v1/design-user-service/my/preference",
            headers=headers, timeout=10, verify=False
        )
        return resp.status_code == 200
    except Exception:
        return False


def get_username(token):
    """获取MQTT用户名"""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(
            f"{API_URL}/v1/design-user-service/my/preference",
            headers=headers, timeout=15, verify=False
        )
        if resp.status_code == 200:
            uid = resp.json().get("uid")
            if uid:
                return f"u_{uid}"
    except Exception:
        pass
    return None


def send_sms(phone):
    """发送验证码"""
    resp = requests.post(
        f"{API_URL}/v1/user-service/user/sendsmscode",
        json={"phone": phone, "type": "codeLogin"},
        timeout=15, verify=False
    )
    return {"ok": resp.status_code == 200, "status": resp.status_code}


def login_with_code(phone, code, token_file):
    """验证码登录，保存token"""
    resp = requests.post(
        f"{API_URL}/v1/user-service/user/login",
        json={"account": phone, "code": code},
        timeout=15, verify=False
    )
    if resp.status_code != 200:
        return {"ok": False, "error": resp.text}
    token = resp.json().get("accessToken")
    os.makedirs(os.path.dirname(token_file) or ".", exist_ok=True)
    with open(token_file, "w") as f:
        f.write(token)
    logger.info("登录成功，token已保存")
    return {"ok": True, "token": token}


def load_token(token_file):
    """加载缓存的token"""
    if os.path.exists(token_file):
        with open(token_file, "r") as f:
            return f.read().strip()
    return None