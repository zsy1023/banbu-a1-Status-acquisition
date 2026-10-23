"""认证模块 — 拓竹token管理"""
import os
import json
import logging

import requests

# curl_cffi 可伪装浏览器 TLS 指纹，用于绕过 Cloudflare 拦截
# （参考 ha-bambulab：裸 requests 会被 Cloudflare 挂起/拦截）
try:
    from curl_cffi import requests as _curl_requests
    _CURL_OK = True
except ImportError:
    _curl_requests = None
    _CURL_OK = False

logger = logging.getLogger("printer.auth")

IMPERSONATE_BROWSER = "chrome"

# ── 云端 API 端点（区分区域，与 greghesp/pybambu 一致）──
# 注意：Token 不跨区域通用，中国区和国际区是两套账号体系。
# 手机号走 bambulab.cn 收码 = 中国区，登录也必须用中国区端点，
# 否则会把中国区的验证码判为“不存在或已过期”（code:1）。
REGION_CN = "China"
REGION_GLOBAL = "Global"
DEFAULT_REGION = REGION_CN      # 本项目用手机号登录（中国区）

_ENDPOINTS = {
    # 区域: (API 基址, 短信域名)
    REGION_CN:     ("https://api.bambulab.cn",  "https://bambulab.cn"),
    REGION_GLOBAL: ("https://api.bambulab.com", "https://bambulab.cn"),
}


def _urls(region=DEFAULT_REGION):
    """按区域返回 (API_URL, LOGIN_URL, PREFERENCE_URL, SMS_DOMAIN, SMS_URL)"""
    api, sms_domain = _ENDPOINTS.get(region, _ENDPOINTS[DEFAULT_REGION])
    return (
        api,
        f"{api}/v1/user-service/user/login",
        f"{api}/v1/design-user-service/my/preference",
        sms_domain,
        f"{sms_domain}/api/v1/user-service/user/sendsmscode",
    )


API_URL, LOGIN_URL, PREFERENCE_URL, SMS_DOMAIN, SMS_URL = _urls(DEFAULT_REGION)
# 浏览器 UA：拓竹云端会对无 UA 的请求直接拦截
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 发码请求源站经常挂起不返回，Cloudflare 60s 后给 504；
# 但此时短信通常已经下发，所以 504/超时必须按“可能已发送”处理。
SMS_TIMEOUT = 75


def mask_phone(phone):
    """手机号脱敏，避免完整号码进日志"""
    phone = str(phone or "")
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return phone or "-"


def load_config(config_path):
    """加载配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_token(token):
    """测试拓竹token是否有效"""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(
            PREFERENCE_URL,
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
            PREFERENCE_URL,
            headers=headers, timeout=15, verify=False
        )
        if resp.status_code == 200:
            uid = resp.json().get("uid")
            if uid:
                return f"u_{uid}"
    except Exception:
        pass
    return None


def _fetch_csrf_token(session, domain):
    """获取 bbl_csrf_token（双提交 CSRF 防护）

    参考 bambuddy #2696 / ha-bambulab：
      - 拓竹给 web 域加了双提交 CSRF 防护，POST 缺 cookie 直接 403 missing_cookie
      - 只有 GET /api/csrf 会签发 cookie（登录页面只给 Cloudflare 的 __cf_bm）
      - cookie 由同一 session 自动带上，另外还需在请求头回显 x-bbl-csrf-token

    Returns:
        token 字符串；失败返回 None
    """
    try:
        resp = session.get(f"{domain}/api/csrf",
                           headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                           timeout=20, verify=False)
    except Exception as e:
        logger.warning("获取 CSRF token 失败: %s: %s", type(e).__name__, str(e)[:150])
        return None

    token = None
    try:
        cookies = getattr(session, "cookies", None)
        if cookies is not None:
            token = cookies.get("bbl_csrf_token")
    except Exception:
        token = None

    if not token:
        logger.warning("CSRF 端点未返回 bbl_csrf_token（HTTP %s）", resp.status_code)
        return None
    logger.debug("已获取 CSRF token（HTTP %s）", resp.status_code)
    return token


def send_sms(phone):
    """发送短信验证码

    照搬 ha-bambulab / bambuddy 的做法：
      - 端点 bambulab.cn/api/v1/user-service/user/sendsmscode
      - 先 GET /api/csrf 取 bbl_csrf_token，同一 session 带 cookie +
        x-bbl-csrf-token 头（拓竹 web 域的双提交 CSRF 防护）
      - 用 curl_cffi impersonate="chrome" 过 Cloudflare

    发出去就算成功，不根据响应码判断成败（拓竹这个接口经常超时/504，
    但短信其实已下发）。
    """
    phone = (phone or "").strip()
    if not phone:
        logger.warning("发送验证码失败：手机号为空")
        return {"ok": False, "status": 0, "error": "手机号为空"}
    if "*" in phone or not phone.isdigit():
        # 常见误用：把界面上的脱敏号（138****0000）直接发过来，
        # 云端会回 400 "Enter a valid phone number."，短信自然发不出
        logger.warning("发送验证码失败：手机号格式非法 %r", phone)
        return {"ok": False, "status": 0, "error": "手机号格式不正确"}

    masked = mask_phone(phone)
    logger.info("正在请求短信验证码: %s -> %s", masked, SMS_URL)

    try:
        session = (_curl_requests.Session(impersonate=IMPERSONATE_BROWSER)
                   if _CURL_OK else requests.Session())
        csrf = _fetch_csrf_token(session, SMS_DOMAIN)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": BROWSER_UA,
        }
        if csrf:
            headers["x-bbl-csrf-token"] = csrf
        kw = {"impersonate": IMPERSONATE_BROWSER, "verify": False} if _CURL_OK else {"verify": False}
        resp = session.post(SMS_URL, json={"phone": phone, "type": "codeLogin"},
                            headers=headers, timeout=SMS_TIMEOUT, **kw)
        logger.info("验证码请求已发出: %s | HTTP %s | 响应 %r",
                    masked, resp.status_code, (resp.text or "")[:200])
        try:
            session.close()
        except Exception:
            pass
    except Exception as e:
        # 请求已发出，只是响应超时/异常；短信通常已经下发
        logger.info("验证码请求已发出（响应异常）: %s | %s: %s",
                    masked, type(e).__name__, str(e)[:150])

    return {"ok": True, "status": 200}


def login_with_code(phone, code, token_file):
    """验证码登录，保存 token

    端点按区域选择（与 greghesp/pybambu 的 bambu_cloud.py 一致）：
        China  -> https://api.bambulab.cn/v1/user-service/user/login
        Global -> https://api.bambulab.com/v1/user-service/user/login
    Token 不跨区域通用，必须与收码的区域一致。

    400 时响应体里的 code（上游约定）：
        1 = 验证码不存在或已过期
        2 = 验证码不正确
    """
    masked = mask_phone(phone)
    try:
        session = (_curl_requests.Session(impersonate=IMPERSONATE_BROWSER)
                   if _CURL_OK else requests.Session())
        kw = {"impersonate": IMPERSONATE_BROWSER, "verify": False} if _CURL_OK else {"verify": False}
        resp = session.post(
            LOGIN_URL,
            json={"account": phone, "code": code},
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": BROWSER_UA},
            timeout=25, **kw,
        )
        try:
            session.close()
        except Exception:
            pass
    except Exception as e:
        logger.error("登录请求异常: %s: %s", type(e).__name__, str(e)[:150])
        return {"ok": False, "error": f"无法连接拓竹云服务（{type(e).__name__}）"}

    logger.info("登录接口响应(%s): HTTP %s | %r",
                masked, resp.status_code, (resp.text or "")[:200])

    if resp.status_code != 200:
        # 解析上游约定的错误码，给出可读提示
        try:
            data = resp.json()
        except Exception:
            data = {}
        ec = data.get("code")
        if ec == 1:
            return {"ok": False, "error": "验证码已过期或不存在，请重新发送验证码"}
        if ec == 2:
            return {"ok": False, "error": "验证码不正确，请重新输入"}
        return {"ok": False, "error": data.get("error") or (resp.text or "登录失败")[:200]}

    token = (resp.json() or {}).get("accessToken")
    if not token:
        return {"ok": False, "error": "登录成功但未返回 accessToken"}

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
