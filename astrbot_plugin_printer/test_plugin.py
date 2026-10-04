"""
测试 AstrBot 打印机插件的 TCP 通信逻辑
模拟插件与服务端 Agent TCP 的交互流程
"""
import asyncio
import json
import sys
import os

# 加载插件模块来测试其工具函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOST = "127.0.0.1"
PORT = 19555
APIKEY = "sk-your-agent-api-key"


async def test_get_status():
    """测试 1: 请求打印机状态"""
    print("=" * 50)
    print("测试 1: GET_STATUS")
    print("=" * 50)
    try:
        reader, writer = await asyncio.open_connection(HOST, PORT)
        cmd = {"cmd": "get_status", "apikey": APIKEY}
        writer.write((json.dumps(cmd) + "\n").encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), timeout=15)
        writer.close()
        resp = json.loads(data.decode())
        assert resp.get("ok") is True, f"❌ get_status 失败: {resp}"
        d = resp.get("data", {})
        print(f"  ✅ 连接状态: {d.get('connected')}")
        print(f"  ✅ 打印机状态: {d.get('state')}")
        print(f"  ✅ 进度: {d.get('progress', {}).get('percent')}%")
        print(f"  ✅ 喷头温度: {d.get('temperature', {}).get('nozzle', {}).get('current')}°C")
        print(f"  ✅ 任务: {d.get('task', {}).get('name')}")
    except Exception as e:
        print(f"  ❌ get_status 异常: {e}")
        return False
    return True


async def test_subscribe_and_push():
    """测试 2: 订阅推送 + 等待事件（30秒超时）"""
    print("\n" + "=" * 50)
    print("测试 2: SUBSCRIBE + 等待推送")
    print("=" * 50)
    try:
        reader, writer = await asyncio.open_connection(HOST, PORT)
        cmd = {
            "cmd": "subscribe",
            "apikey": APIKEY,
            "events": [
                {"type": "layer"},
                {"type": "complete"},
                {"type": "error"},
                {"type": "paused"},
                {"type": "printing_started"},
            ],
        }
        writer.write((json.dumps(cmd) + "\n").encode())
        await writer.drain()
        # 读取订阅确认
        data = await asyncio.wait_for(reader.read(65536), timeout=10)
        resp = json.loads(data.decode())
        assert resp.get("data", {}).get("subscribed") == 5, f"❌ subscribe 确认异常: {resp}"
        print(f"  ✅ 订阅成功: {resp['data']['subscribed']} 个事件")

        # 等待最多 60 秒接收推送
        print("  ⏳ 等待推送事件 (60秒超时)...")
        try:
            push_data = await asyncio.wait_for(reader.read(65536), timeout=60)
            for line in push_data.decode().split("\n"):
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("type") == "push":
                    print(f"  ✅ 收到推送: event={msg['event']} data={msg['data']}")
                elif msg.get("type") == "pong":
                    print(f"  ℹ️  心跳回复")
        except asyncio.TimeoutError:
            print("  ℹ️  超时未收到推送（正常，打印机当前无事件）")

        writer.close()
        return True
    except Exception as e:
        print(f"  ❌ subscribe 异常: {e}")
        return False


async def test_ping():
    """测试 3: 心跳"""
    print("\n" + "=" * 50)
    print("测试 3: PING")
    print("=" * 50)
    try:
        reader, writer = await asyncio.open_connection(HOST, PORT)
        cmd = {"cmd": "ping", "apikey": APIKEY}
        writer.write((json.dumps(cmd) + "\n").encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), timeout=10)
        writer.close()
        resp = json.loads(data.decode())
        assert resp.get("type") == "pong", f"❌ ping 失败: {resp}"
        print("  ✅ 心跳正常")
        return True
    except Exception as e:
        print(f"  ❌ ping 异常: {e}")
        return False


async def test_format_functions():
    """测试 4: 插件格式化函数"""
    print("\n" + "=" * 50)
    print("测试 4: 格式化函数")
    print("=" * 50)
    from main import PrinterBridge

    plugin = PrinterBridge(None, None)

    # 模拟状态数据
    fake_status = {
        "connected": True,
        "state": "printing",
        "progress": {"percent": 65, "layer": 130, "total_layers": 200},
        "temperature": {
            "nozzle": {"current": 210.5, "target": 220},
            "bed": {"current": 60.2, "target": 60},
            "chamber": 42,
        },
        "speed": 100,
        "task": {"name": "测试模型.stl"},
        "hardware": {"light_mode": "on"},
    }
    text = plugin._format_status(fake_status)
    assert "65%" in text, "❌ 格式化状态中缺少进度"
    assert "210.5" in text, "❌ 格式化状态中缺少温度"
    print(f"  ✅ _format_status:\n{text}\n")

    # 模拟推送事件
    push_text = plugin._format_push(
        "complete", {"task_name": "测试模型", "total_time": 120}
    )
    assert "测试模型" in push_text, "❌ 格式化推送中缺少任务名"
    print(f"  ✅ _format_push(complete): {push_text}")

    push_text2 = plugin._format_push(
        "layer", {"layer": 50, "total_layers": 200, "percent": 25}
    )
    assert "50" in push_text2, "❌ 格式化推送中缺少层数"
    print(f"  ✅ _format_push(layer):    {push_text2}")
    return True


async def main():
    results = []
    results.append(await test_get_status())
    results.append(await test_ping())
    results.append(await test_subscribe_and_push())
    results.append(await test_format_functions())

    print("\n" + "=" * 50)
    passed = sum(results)
    total = len(results)
    print(f"测试结果: {passed}/{total} 通过")
    if all(results):
        print("✅ 全部通过！插件可正常运行")
    else:
        print("❌ 部分测试失败")


if __name__ == "__main__":
    asyncio.run(main())
