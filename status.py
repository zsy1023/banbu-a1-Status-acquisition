"""
命令行一键查看打印机状态
从运行中的 API 服务获取数据，终端格式化输出

颜色约定:
  🟢 绿色   — 标签/说明文字 (info)
  🔵 青色   — 数值/数据
  🔴 红色   — 温度、错误
  🟡 黄色   — 警告、速度、剩余时间
  ⚪ 灰色   — 次要信息
  ⚪ 亮白粗体 — 分隔线/标题
"""
import sys

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("\033[91m❌ 缺少 requests 库，请运行: pip install requests\033[0m")
    sys.exit(1)

API = "https://localhost:18443/api/status"

# ── 语义化颜色 ──────────────────────────────────
# 命名规则: 用途_颜色，便于理解
CLR = {
    "R": "\033[0m",      # Reset
    "B": "\033[1m",      # Bold
    # 语义色
    "info": "\033[92m",   # 绿色 — 标签/说明
    "val":  "\033[96m",   # 青色 — 数值
    "hot":  "\033[91m",   # 红色 — 温度/错误
    "warn": "\033[93m",   # 黄色 — 速度/剩余时间/警告
    "ok":   "\033[92m",   # 绿色 — 正面状态
    "muted":"\033[90m",   # 灰色 — 次要信息
    "head": "\033[1;97m", # 亮白粗体 — 标题/分隔线
}


def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    if len(hex_color) >= 6:
        return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))
    return (0, 0, 0)


def color_block(hex_color):
    """HEX颜色转ANSI色块■"""
    r, g, b = hex_to_rgb(hex_color)
    if r > 150 and g < 100 and b < 100: c = CLR["hot"]
    elif r < 100 and g > 150 and b < 100: c = CLR["ok"]
    elif r > 150 and g > 150 and b < 100: c = CLR["warn"]
    elif r < 100 and g < 100 and b > 150: c = "\033[94m"
    elif r > 150 and g < 100 and b > 150: c = "\033[95m"
    elif r < 100 and g > 150 and b > 150: c = CLR["val"]
    elif r > 200 and g > 200 and b > 200: c = "\033[97m"
    else: c = CLR["muted"]
    return f"{c}■{CLR['R']}"


def label(text):
    """绿色标签"""
    return f"{CLR['info']}{text}{CLR['R']}"


def val(text):
    """青色数值"""
    return f"{CLR['val']}{text}{CLR['R']}"


def hot(text):
    """红色温度/错误"""
    return f"{CLR['hot']}{text}{CLR['R']}"


def warn(text):
    """黄色警告"""
    return f"{CLR['warn']}{text}{CLR['R']}"


def muted(text):
    """灰色次要信息"""
    return f"{CLR['muted']}{text}{CLR['R']}"


def bold(text):
    """粗体"""
    return f"{CLR['B']}{text}{CLR['R']}"


def line():
    """分隔线"""
    return f"{CLR['head']}{'='*60}{CLR['R']}"


def print_status(s):
    """格式化终端输出"""
    if not s.get("connected"):
        print(f"{CLR['hot']}❌ 未连接 — 服务未运行或未登录{CLR['R']}")
        return

    state_map = {
        "idle": ("待机", CLR["muted"]),
        "printing": ("打印中", CLR["ok"]),
        "paused": ("暂停", CLR["warn"]),
        "finished": ("完成", CLR["val"]),
        "failed": ("失败", CLR["hot"]),
    }
    state_cn, state_color = state_map.get(s["state"], (s.get("state_raw", "?"), CLR["muted"]))

    pct = s["progress"]["percent"]
    bar_width = 20
    filled = int(bar_width * pct / 100)
    bar = "█" * filled + "░" * (bar_width - filled)

    # ── 标题 ─────────────────────────────────
    print(f"\n{line()}")
    print(f"{CLR['head']}🖨️ 拓竹A1 打印机状态 — {s.get('last_refresh', '?')[:19]}{CLR['R']}")
    print(f"{line()}")

    # ── 状态 + 进度 ───────────────────────────
    speed_val = s["speed"]
    print(f"\n  {label('状态')}  {state_color}{state_cn}{CLR['R']}  {muted('|')}  {label('速度')} {warn(str(speed_val)+'%')}")
    print(f"  {label('进度')}  [{CLR['ok']}{bar}{CLR['R']}] {bold(str(pct)+'%')}")

    # ── 任务信息 ──────────────────────────────
    task_name = s["task"]["name"] or muted("无")
    print(f"  {label('任务')}  {val(task_name)}")

    remain = s["time"]["remaining_min"]
    if remain:
        print(f"  {label('剩余')}  {warn(str(remain)+'分钟')}")

    weight = s["task"]["weight_g"]
    if weight:
        print(f"  {label('重量')}  {val(str(weight)+'g')}")

    layer = s["progress"]["layer"]
    total = s["progress"]["total_layers"]
    if total > 0:
        print(f"  {label('层数')}  {val(str(layer))} {muted('/')} {val(str(total))}")

    # ── 温度 ──────────────────────────────────
    print(f"\n  {bold('🌡️ 温度')}")
    nt_cur = s["temperature"]["nozzle"]["current"]
    nt_tgt = s["temperature"]["nozzle"]["target"]
    bt_cur = s["temperature"]["bed"]["current"]
    bt_tgt = s["temperature"]["bed"]["target"]
    ct = s["temperature"]["chamber"]
    print(f"    {label('喷头')}  {hot(str(nt_cur)+'°C')}  {muted('→ '+str(nt_tgt)+'°C')}")
    print(f"    {label('热床')}  {hot(str(bt_cur)+'°C')}  {muted('→ '+str(bt_tgt)+'°C')}")
    print(f"    {label('腔体')}  {hot(str(ct)+'°C')}")

    # ── 风扇 ──────────────────────────────────
    cooling_fan = s["fan"]["cooling"]
    heatbreak_fan = s["fan"]["heatbreak"]
    big1 = s["fan"]["big_fan1"]
    big2 = s["fan"]["big_fan2"]
    print(f"\n  {bold('💨 风扇')}")
    print(f"    {label('冷却')} {val(cooling_fan)}  {muted('|')}  {label('热端')} {val(heatbreak_fan)}")
    print(f"    {label('大风扇1')} {val(big1)}  {muted('|')}  {label('大风扇2')} {val(big2)}")

    # ── AMS ──────────────────────────────────
    ams_list = s.get("ams", [])
    if ams_list:
        print(f"\n  {bold('🎨 AMS')}")
        for unit in ams_list:
            uid = unit["id"]
            hum = unit.get("humidity", "N/A")
            print(f"    {label('槽位 '+str(uid))}  {muted('湿度: '+str(hum)+'%')}")
            for tray in unit.get("trays", []):
                block = color_block(tray["color"].lstrip("#"))
                tid = tray["id"]
                tcolor = tray["color"]
                tname = tray.get("color_name", tcolor)
                ttype = tray["type"]
                print(f"      [{tid}] {block}  {muted(tname)}  {val(ttype)}")

    # ── 其他 ──────────────────────────────────
    print(f"\n  {bold('⚙️ 其他')}")
    wifi = s["hardware"]["wifi"]
    nozzle_d = s["hardware"]["nozzle_diameter"]
    nozzle_t = s["hardware"]["nozzle_type"]
    light = s["hardware"]["light_mode"]
    print(f"    {label('WiFi')}  {val(wifi)}")
    print(f"    {label('喷嘴')}  {val(f'{nozzle_d}mm')} {muted(f'({nozzle_t})')}")
    print(f"    {label('灯光')}  {warn(light)}")

    print(f"\n{line()}\n")


def main():
    try:
        resp = requests.get(API, verify=False, timeout=10)
        data = resp.json()
    except requests.exceptions.ConnectionError:
        print(f"{CLR['hot']}❌ 无法连接服务{CLR['R']}")
        print(f"   {muted('请先启动 printer_service: 双击 start.bat')}")
        sys.exit(1)
    except Exception as e:
        print(f"{CLR['hot']}❌ 请求失败: {e}{CLR['R']}")
        sys.exit(1)

    if data.get("ok") is False and data.get("error") == "需要先登录":
        print(f"{CLR['warn']}⚠️ 未登录{CLR['R']}")
        print(f"   {muted('浏览器打开 https://localhost:18443 登录')}")
        sys.exit(1)

    print_status(data)
    sys.exit(0)


if __name__ == "__main__":
    main()
