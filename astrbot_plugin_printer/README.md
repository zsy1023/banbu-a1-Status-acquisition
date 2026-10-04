# 🖨️ AstrBot 3D Printer + Dryer Plugin

AstrBot 插件，用于监控拓竹（Bambu Lab）3D 打印机状态和控制耗材烘干机。

## 功能

### 🖨️ 打印机
- 实时查看打印状态（温度、进度、任务、风扇、AMS）
- 订阅推送通知（打印开始/完成/出错/暂停）
- 通过 Agent TCP 长连接接收事件推送

### 🔥 烘干机
- 查看烘干机状态（温度、剩余时间、加热/风扇状态）
- 开始/停止烘干
- 设置温度和烘干时间
- 手动控制加热片和风扇

## 安装

1. 将插件目录复制到 `AstrBot/data/plugins/`：
```bash
cp -r astrbot_plugin_printer AstrBot/data/plugins/
```

2. 在 AstrBot WebUI 中重载插件

3. 在插件配置页面填写以下配置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `printer_apikey` | (空) | 打印机服务 Agent TCP 的 API Key |
| `printer_host` | `127.0.0.1` | 打印机 Agent TCP 地址 |
| `printer_port` | `19555` | 打印机 Agent TCP 端口 |
| `dryer_host` | `192.168.1.100` | 烘干机 TCP 地址 |
| `dryer_port` | `8080` | 烘干机 TCP 端口 |

## 命令

### 打印机

| 命令 | 说明 |
|------|------|
| `/printer` | 查看打印机即时状态 |
| `/printer subscribe` | 订阅打印推送通知（需确认） |
| `/printer unsubscribe` | 取消推送订阅 |

**推送事件**：打印开始、完成、出错、暂停、层到达时自动推送到聊天。

### 烘干机

| 命令 | 说明 |
|------|------|
| `/dryer` | 查看烘干机状态 |
| `/dryer start` | 开始烘干 |
| `/dryer stop` | 停止烘干 |
| `/dryer temp 50` | 设置烘干温度（0~100°C） |
| `/dryer time 120` | 设置烘干时间（1~999 分钟） |
| `/dryer heater on/off` | 手动开关加热片 |
| `/dryer fan on/off` | 手动开关风扇 |

## 架构

```
聊天平台 (QQ/Telegram/微信)
      │
      ▼
   AstrBot
      │
      ▼
 PrinterBridge 插件
      │
      ├── TCP (:19555) ──→ 打印机状态服务 ──→ MQTT ──→ 打印机
      │       │
      │       └── 长连接订阅 ←── 事件推送
      │
      └── TCP (:8080)  ──→ 烘干机
```

- **打印机**：通过 Agent TCP 协议与本机的打印机状态服务通信，支持请求-响应和长连接推送
- **烘干机**：通过 TCP 直连烘干机硬件，发送文本指令

## 依赖

无额外依赖，使用 Python 标准库 + AstrBot SDK。

## 文件结构

```
astrbot_plugin_printer/
├── metadata.yaml      # 插件元数据与配置定义
├── main.py            # 插件主代码
└── requirements.txt   # 依赖声明
```

## 开发

### 打印机 Agent TCP 协议

通信地址：`127.0.0.1:19555`，JSON over TCP，换行符分隔。

**请求**：
```json
{"cmd": "get_status", "apikey": "sk-xxx"}
{"cmd": "subscribe", "apikey": "sk-xxx", "events": [{"type": "layer"}, ...]}
{"cmd": "ping", "apikey": "sk-xxx"}
```

**响应**：
```json
{"ok": true, "type": "response", "data": {...}}
```

**推送**：
```json
{"ok": true, "type": "push", "event": "complete", "data": {"task_name": "..."}}
```

### 烘干机 TCP 协议

通信地址：`192.168.1.100:8080`，纯文本命令，换行符分隔。

| 命令 | 说明 |
|------|------|
| `STATUS` | 查询状态 |
| `START` | 开始烘干 |
| `STOP` | 停止烘干 |
| `SETTEMP 50` | 设置温度 |
| `SETTIME 120` | 设置时间 |
| `HEATER ON/OFF` | 开关加热 |
| `FAN ON/OFF` | 开关风扇 |

## 许可证

AGPL-3.0
