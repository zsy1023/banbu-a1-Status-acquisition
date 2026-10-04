# 🤖 Agent TCP 通信说明

Agent 服务通过 **TCP 端口 19555** 提供，供 AI agent、自动化脚本、或其他程序与打印机状态服务通信。

协议：**JSON over TCP**，每条消息以换行符 `\n` 分隔。

---

## 快速开始

```python
import socket, json

HOST = "127.0.0.1"
PORT = 19555
APIKEY = "sk-your-agent-api-key"

sock = socket.create_connection((HOST, PORT), timeout=10)

# 获取打印机状态
msg = json.dumps({"cmd": "get_status", "apikey": APIKEY}) + "\n"
sock.send(msg.encode())
resp = sock.recv(65536).decode()
data = json.loads(resp)
print(data["data"]["state"])  # 输出: finished
```

---

## 命令列表

### 1️⃣ get_status — 获取打印机状态

**请求：**
```json
{"cmd": "get_status", "apikey": "sk-xxx"}
```

**响应：**
```json
{
  "ok": true,
  "type": "response",
  "data": {
    "connected": true,
    "serial": "01P00A0000000000",
    "state": "printing",
    "progress": {
      "percent": 65,
      "layer": 130,
      "total_layers": 200
    },
    "temperature": {
      "nozzle": {"current": 210.5, "target": 220},
      "bed": {"current": 60.2, "target": 60},
      "chamber": 42
    },
    "speed": 100,
    "task": {
      "name": "模型名称.stl",
      "weight_g": 45
    },
    "ams": [
      {
        "id": "0",
        "humidity": "5",
        "trays": [
          {"id": "0", "type": "PLA", "color": "#000000", "rgb": [0,0,0]},
          {"id": "1", "type": "PLA", "color": "#BCBCBC", "rgb": [188,188,188]}
        ]
      }
    ],
    "last_refresh": "2026-06-06T10:56:48"
  }
}
```

---

### 2️⃣ subscribe — 订阅事件（长连接，等待主动推送）

订阅后连接保持打开，当事件触发时服务端主动推送消息。

**请求：**
```json
{
  "cmd": "subscribe",
  "apikey": "sk-xxx",
  "events": [
    {"type": "layer", "value": 50},
    {"type": "complete"},
    {"type": "error"},
    {"type": "paused"}
  ]
}
```

| 事件类型 | 说明 | 可选参数 |
|----------|------|----------|
| `layer` | 打印到指定层时触发 | `value`（层数，到达该层时推送） |
| `complete` | 打印完成时触发 | 无 |
| `error` | 打印失败/报错时触发 | 无 |
| `paused` | 打印暂停时触发 | 无 |
| `printing_started` | 开始打印时触发 | 无 |
| `temp_below` | 温度低于阈值时触发 | `value`（温度阈值）`component`（可选，如`nozzle`/`bed`） |

**响应（订阅确认）：**
```json
{"ok": true, "type": "response", "data": {"subscribed": 3, "total": 3}}
```

**主动推送（事件触发时，服务端下发）：**
```json
{"ok": true, "type": "push", "event": "layer", "data": {"layer": 50, "total_layers": 200, "percent": 25}}
{"ok": true, "type": "push", "event": "complete", "data": {"task_name": "模型.stl", "total_time": 180, "percent": 100}}
{"ok": true, "type": "push", "event": "error", "data": {"state": "FAILED"}}
{"ok": true, "type": "push", "event": "paused", "data": {"percent": 55, "layer": 110}}
```

---

### 3️⃣ unsubscribe — 取消订阅

**请求：**
```json
{"cmd": "unsubscribe", "apikey": "sk-xxx", "events": ["layer", "error"]}
```

取消所有订阅（传空数组）：
```json
{"cmd": "unsubscribe", "apikey": "sk-xxx", "events": []}
```

**响应：**
```json
{"ok": true, "type": "response", "data": {"remaining": 1}}
```

---

### 4️⃣ ping — 心跳保活

连接超过 90 秒无消息会被自动断开。建议每 30 秒发一次 ping。

**请求：**
```json
{"cmd": "ping", "apikey": "sk-xxx"}
```

**响应：**
```json
{"ok": true, "type": "pong"}
```

---

## 完整示例

### Python — 获取状态并订阅事件

```python
import socket, json, threading, time

HOST = "127.0.0.1"
PORT = 19555
APIKEY = "sk-your-agent-api-key"

def listen():
    """接收推送消息的后台线程"""
    while True:
        try:
            data = sock.recv(65536)
            if not data:
                break
            for line in data.decode().strip().split("\n"):
                if line:
                    msg = json.loads(line)
                    if msg.get("type") == "push":
                        print(f"📨 事件: {msg['event']} → {msg['data']}")
                    else:
                        print(f"📩 响应: {msg}")
        except:
            break

sock = socket.create_connection((HOST, PORT), timeout=10)

# 启动监听线程
threading.Thread(target=listen, daemon=True).start()

# 订阅事件
sock.send((json.dumps({
    "cmd": "subscribe",
    "apikey": APIKEY,
    "events": [
        {"type": "layer", "value": 50},
        {"type": "complete"},
        {"type": "error"},
    ]
}) + "\n").encode())

# 保持连接，等待推送
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    sock.close()
```

### Python — 单次查询（不保持连接）

```python
import socket, json

sock = socket.create_connection(("127.0.0.1", 19555), timeout=10)
sock.send((json.dumps({
    "cmd": "get_status",
    "apikey": "sk-your-agent-api-key"
}) + "\n").encode())

resp = sock.recv(65536).decode()
status = json.loads(resp)["data"]

print(f"状态: {status['state']}")
print(f"进度: {status['progress']['percent']}%")
print(f"喷头: {status['temperature']['nozzle']['current']}°C")
sock.close()
```

### Node.js — 订阅事件

```javascript
const net = require('net');

const APIKEY = 'sk-your-agent-api-key';
const client = new net.Socket();

client.connect(19555, '127.0.0.1', () => {
  // 订阅事件
  client.write(JSON.stringify({
    cmd: 'subscribe',
    apikey: APIKEY,
    events: [{type: 'complete'}, {type: 'error'}]
  }) + '\n');
});

client.on('data', (data) => {
  const lines = data.toString().trim().split('\n');
  for (const line of lines) {
    const msg = JSON.parse(line);
    if (msg.type === 'push') {
      console.log(`📨 事件: ${msg.event}`, msg.data);
    }
  }
});
```

---

### 5️⃣ control — 控制打印机（两步确认）

为防止误操作，所有控制指令需要**两步确认**：

- **Step 1**: 发起请求 → 返回 `confirm_id`，操作处于待确认状态
- **Step 2**: 用 `confirm_id` 确认执行 → 操作才真正下发
- `confirm_id` 有效期 **30 秒**，过期需重新发起

#### 灯光开关

**Step 1 — 发起请求：**
```json
{"cmd": "control", "apikey": "sk-xxx", "action": "light_toggle"}
```

**响应：**
```json
{
  "ok": true,
  "type": "response",
  "data": {
    "ok": true,
    "confirm_id": "a1b2c3d4e5f6...",
    "status": "pending",
    "expires_in": 30,
    "message": "确认关闭灯光？",
    "preview": {
      "current_light": "on",
      "will_change_to": "off"
    }
  }
}
```

**Step 2 — 确认执行：**
```json
{
  "cmd": "control",
  "apikey": "sk-xxx",
  "action": "light_toggle",
  "confirm_id": "a1b2c3d4e5f6..."
}
```

**响应：**
```json
{
  "ok": true,
  "type": "response",
  "data": {
    "ok": true,
    "status": "executed",
    "result": {"light_mode": "off"}
  }
}
```

#### Python 示例

```python
import socket, json

sock = socket.create_connection(("127.0.0.1", 19555), timeout=10)
APIKEY = "sk-your-agent-api-key"

def send_cmd(cmd):
    sock.send((json.dumps(cmd) + "\n").encode())
    return json.loads(sock.recv(65536).decode())

# Step 1: 请求开关灯光
r = send_cmd({"cmd": "control", "apikey": APIKEY, "action": "light_toggle"})
print(r["data"]["message"])       # "确认关闭灯光？"
print(r["data"]["preview"])       # {"current_light":"on","will_change_to":"off"}

# Step 2: 确认执行（30秒内）
confirm_id = r["data"]["confirm_id"]
r2 = send_cmd({"cmd": "control", "apikey": APIKEY, "action": "light_toggle",
               "confirm_id": confirm_id})
print(r2["data"]["status"])       # "executed"
print(r2["data"]["result"])       # {"light_mode":"off"}

sock.close()
```

---

## 常见问题

**Q: 如何知道连接是否正常？**
A: 每 30 秒发一次 `ping`，收到 `pong` 即正常。90 秒无活动会自动断开。

**Q: 多个 agent 可以同时连接吗？**
A: 可以。每个连接独立订阅，互不影响。可以用不同 API key 区分。

**Q: 事件推送会丢失吗？**
A: 每次状态刷新（每30秒）检测事件。如果 agent 刚好断开重连，可能丢失一次推送。可以配合 `/api/status` REST API 定时轮询做双重保障。

**Q: 连接断开后需要重新订阅吗？**
A: 是的，重新连接后需要重新 `subscribe`。

**Q: layer 事件支持多个阈值吗？**
A: 支持。可以同时订阅 `{"type":"layer","value":50}` 和 `{"type":"layer","value":100}`，到达每个阈值都会推送。


---

## REST API 端点速查

Web 服务地址: \`https://localhost:18443\`

| 方法 | 路径 | 说明 |
|------|------|------|
| \`GET\` | \`/\` | HTML 仪表盘 |
| \`GET\` | \`/api/status\` | 打印机状态 JSON（缓存） |
| \`POST\` | \`/api/refresh\` | 强制从 MQTT 刷新状态 |
| \`POST\` | \`/api/control\` | 控制指令（两步确认） |
| \`GET\` | \`/api/temp-history?minutes=60\` | 温度历史数据 |
| \`GET\` | \`/api/print-history?limit=10\` | 打印历史记录 |
| \`GET\` | \`/api/config\` | 查看配置（脱敏） |
| \`POST\` | \`/api/send-code\` | 发送登录验证码 |
| \`POST\` | \`/api/login\` | 验证码登录 |

### 手动刷新

\`\`\`powershell
curl -X POST https://localhost:18443/api/refresh
\`\`\`
