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


def _can_notify():
    global _last_notify_time
    now = time.time()
    if now - _last_notify_time < _MIN_INTERVAL:
        return False
    _last_notify_time = now
    return True


def notify(title: str, message: str, icon="D:\\3d\\cert\\server.crt", on_click=None):
    """显示 Windows 通知"""
    if not _can_notify():
        return

    url = on_click if on_click and not callable(on_click) else ""

    try:
        if url:
            ps = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
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

    logger.info("通知(日志): %s — %s", title, message)