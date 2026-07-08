"""Add to Windows startup"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STARTUP_DIR = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
SHORTCUT_PATH = os.path.join(STARTUP_DIR, "BambuPrinterService.lnk")
BAT_PATH = os.path.join(HERE, "start.bat")


def install():
    if not os.path.exists(BAT_PATH):
        print(f"❌ Cannot find {BAT_PATH}")
        return

    ps_script = f'''
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("{SHORTCUT_PATH}")
$s.TargetPath = "{BAT_PATH}"
$s.WorkingDirectory = "{HERE}"
$s.Description = "BambuLab Printer Status Service - Auto Start"
$s.Save()
'''
    import subprocess
    r = subprocess.run(["powershell", "-Command", ps_script], capture_output=True, text=True)
    if os.path.exists(SHORTCUT_PATH):
        print(f"✅ Auto-start added")
        print(f"   Shortcut: {SHORTCUT_PATH}")
        print(f"   Points to: {BAT_PATH}")
    else:
        print(f"❌ Failed to add: {r.stderr}")


def uninstall():
    if os.path.exists(SHORTCUT_PATH):
        os.remove(SHORTCUT_PATH)
        print("✅ Auto-start removed")
    else:
        print("ℹ️  No auto-start entry found")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        uninstall()
    else:
        install()