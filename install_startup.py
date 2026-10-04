"""添加到Windows开机自启"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STARTUP_DIR = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
SHORTCUT_PATH = os.path.join(STARTUP_DIR, "拓竹打印机服务.lnk")
BAT_PATH = os.path.join(HERE, "start.bat")


def install():
    if not os.path.exists(BAT_PATH):
        print(f"❌ 找不到 {BAT_PATH}")
        return

    # 用 PowerShell 创建快捷方式
    ps_script = f'''
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("{SHORTCUT_PATH}")
$s.TargetPath = "{BAT_PATH}"
$s.WorkingDirectory = "{HERE}"
$s.Description = "拓竹打印机状态服务 - 开机自启"
$s.Save()
'''
    import subprocess
    r = subprocess.run(["powershell", "-Command", ps_script], capture_output=True, text=True)
    if os.path.exists(SHORTCUT_PATH):
        print(f"✅ 开机自启已添加")
        print(f"   快捷方式: {SHORTCUT_PATH}")
        print(f"   指向: {BAT_PATH}")
    else:
        print(f"❌ 添加失败: {r.stderr}")


def uninstall():
    if os.path.exists(SHORTCUT_PATH):
        os.remove(SHORTCUT_PATH)
        print("✅ 已移除开机自启")
    else:
        print("ℹ️  未找到开机自启项")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        uninstall()
    else:
        install()
