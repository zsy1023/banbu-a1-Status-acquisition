"""Windows 系统通知模块"""
import logging
import subprocess
import time
import webbrowser
import os

logger = logging.getLogger("printer.notify")

_last_notify_time = 0
_MIN_INTERVAL = 5

POWERSHELL_PATH = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

TOAST_APP_ID = "PythonPrinter"

# Toast 模板：整条通知可点击跳转（activationType=protocol）
_TOAST_PS = '''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = @"
__XML__
"@
$doc = [Windows.Data.Xml.Dom.XmlDocument]::new()
$doc.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
$notifier = $null
try { $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("__APPID__") } catch { $notifier = $null }
if ($null -eq $notifier) { $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier() }
$notifier.Show($toast)
'''

# 回退：模态对话框，点“确定”后打开浏览器（120秒无人响应自动关闭）
_MSGBOX_PS = '''
Add-Type -AssemblyName System.Windows.Forms
$r = [System.Windows.Forms.MessageBox]::Show("__MSG__", "__TITLE__", [System.Windows.Forms.MessageBoxButtons]::OKCancel, [System.Windows.Forms.MessageBoxIcon]::Warning)
if ($r -eq [System.Windows.Forms.DialogResult]::OK) { Start-Process "__URL__" }
'''


def _xml_escape(text):
    """转义 XML 文本，避免标题/正文里的特殊字符破坏模板"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _ps_escape(text):
    """转义 PowerShell 双引号字符串"""
    return str(text).replace('"', '""')


def _can_notify():
    global _last_notify_time
    now = time.time()
    if now - _last_notify_time < _MIN_INTERVAL:
        return False
    _last_notify_time = now
    return True


_DEFAULT_ICON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cert", "server.crt")


def notify(title: str, message: str, icon=_DEFAULT_ICON, on_click=None):
    """显示 Windows 通知"""
    if not _can_notify():
        return

    url = on_click if on_click and not callable(on_click) else ""

    # 后端1: PowerShell + AppID（支持点击跳转）
    try:
        if url:
            ps = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = @"
<toast>
  <visual>
    <binding template="ToastText02">
      <text id="1">{title}</text>
      <text id="2">{message}</text>
    </binding>
  </visual>
  <actions>
    <action content="查看详情" arguments="{url}" activationType="protocol"/>
  </actions>
</toast>
"@
$template = [Windows.Data.Xml.Dom.XmlDocument]::new()
$template.LoadXml($xml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("PythonPrinter").Show($toast)
            '''
        else:
            ps = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName("text")
$textNodes.Item(0).AppendChild($template.CreateTextNode("{title}")) > $null
$textNodes.Item(1).AppendChild($template.CreateTextNode("{message}")) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("PythonPrinter").Show($toast)
            '''
        subprocess.run(
            [POWERSHELL_PATH, "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=10
        )
        logger.info("通知(PowerShell): %s — %s", title, message)
        return
    except Exception as e:
        logger.debug("PowerShell通知失败: %s", e)
        pass

    # 后端2: mshta（简单弹窗）
    try:
        msg = message.replace('"', '\\"').replace("'", "\\'")
        title_esc = title.replace('"', '\\"').replace("'", "\\'")
        mshta_cmd = f'about:<html><script>new ActiveXObject("WScript.Shell").Popup("{msg}",5,"{title_esc}",0x40);close();</script></html>'
        subprocess.run(["mshta.exe", mshta_cmd], capture_output=True, timeout=10)
        logger.info("通知(mshta): %s — %s", title, message)
        return
    except Exception as e:
        logger.debug("mshta通知失败: %s", e)
        pass

    # 后端3: 仅日志
    logger.info("通知(日志): %s — %s", title, message)


# ── Token 失效提醒 ─────────────────────────────

def show_toast(title: str, message: str, url: str = "", action: str = "查看详情"):
    """发送 Windows Toast 通知；url 非空时整条通知/按钮可点击跳转

    Returns:
        bool: 是否发送成功
    """
    if url:
        xml = (
            '<toast activationType="protocol" launch="%s">'
            '<visual><binding template="ToastGeneric">'
            '<text id="1">%s</text><text id="2">%s</text>'
            '</binding></visual>'
            '<actions>'
            '<action content="%s" arguments="%s" activationType="protocol"/>'
            '<action content="稍后" arguments="dismiss" activationType="system"/>'
            '</actions></toast>'
        ) % (_xml_escape(url), _xml_escape(title), _xml_escape(message),
             _xml_escape(action), _xml_escape(url))
    else:
        xml = (
            '<toast><visual><binding template="ToastText02">'
            '<text id="1">%s</text><text id="2">%s</text>'
            '</binding></visual></toast>'
        ) % (_xml_escape(title), _xml_escape(message))

    ps = _TOAST_PS.replace("__XML__", xml).replace("__APPID__", TOAST_APP_ID)
    try:
        proc = subprocess.run(
            [POWERSHELL_PATH, "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=20
        )
        if proc.returncode == 0:
            return True
        logger.debug("Toast发送失败(rc=%s): %s", proc.returncode,
                     proc.stderr.decode("utf-8", "ignore")[:200])
    except Exception as e:
        logger.debug("Toast发送异常: %s", e)
    return False


def _show_message_box(title: str, message: str, url: str) -> bool:
    """回退方案：模态对话框，点“确定”打开浏览器"""
    ps = (_MSGBOX_PS
          .replace("__MSG__", _ps_escape(message))
          .replace("__TITLE__", _ps_escape(title))
          .replace("__URL__", _ps_escape(url)))
    try:
        proc = subprocess.run(
            [POWERSHELL_PATH, "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=120
        )
        return proc.returncode == 0
    except Exception as e:
        logger.debug("MessageBox弹窗失败: %s", e)
        return False


def prompt_token_update(dashboard_url: str, reason: str = "expired"):
    """token 失效提醒 — 点击通知直接进入仪表盘登录页更新 token

    Args:
        dashboard_url: 仪表盘根地址，如 https://localhost:18443
        reason: "expired" token已失效 / "missing" 无缓存token / 其他
    """
    if not _can_notify():
        return False

    title = {
        "expired": "🔐 拓竹 Token 已失效",
        "missing": "🔐 需要登录拓竹账号",
        "error": "⚠️ 登录状态异常",
    }.get(reason, "🔐 需要重新登录")
    message = "点击此通知前往仪表盘更新 Token"
    url = dashboard_url.rstrip("/") + "/?login=1"

    if show_toast(title, message, url=url, action="去更新 Token"):
        logger.info("已弹出Token更新提醒(Toast): %s", url)
        return True

    if _show_message_box(title, message + "\n\n点击“确定”打开仪表盘登录页。", url):
        logger.info("已弹出Token更新提醒(对话框): %s", url)
        return True

    logger.warning("Token提醒弹窗失败，请手动打开 %s", url)
    return False
