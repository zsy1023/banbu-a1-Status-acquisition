# BambuLab Printer Status Service

[![GitHub stars](https://img.shields.io/github/stars/zsy1023/banbu-a1-Status-acquisition)](https://github.com/zsy1023/banbu-a1-Status-acquisition/stargazers)
[![GitHub license](https://img.shields.io/github/license/zsy1023/banbu-a1-Status-acquisition)](https://github.com/zsy1023/banbu-a1-Status-acquisition/blob/main/LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)

A lightweight service for monitoring BambuLab 3D printers, with real-time status updates, event notifications, and remote control capabilities.

一个轻量级的拓竹3D打印机状态监控服务，支持实时状态更新、事件通知和远程控制功能。

---

## ✨ Features / 功能特性

| English | 中文 |
|---------|------|
| **Web Dashboard** - Real-time printer status monitoring via HTTPS | **Web仪表盘** - 通过HTTPS实时监控打印机状态 |
| **TCP API** - For external applications to connect and receive push events | **TCP API** - 供外部程序连接并接收事件推送 |
| **MQTT Integration** - Direct connection to BambuCloud for printer status | **MQTT集成** - 直接连接拓竹云获取打印机状态 |
| **Windows Notifications** - Push notifications on print completion, errors, etc. | **Windows通知** - 打印完成、错误等事件推送系统通知 |
| **Remote Control** - Control printer lights via API with two-step confirmation | **远程控制** - 通过API控制打印机灯光（两步确认） |
| **Print History** - Track past print jobs with details | **打印历史** - 记录并查看历史打印任务 |
| **Custom Notification Rules** - Set triggers for temperature thresholds, layer counts, etc. | **自定义通知规则** - 设置温度阈值、层数、剩余时间等触发通知 |
| **Webhook Support** - Push events to external services | **Webhook支持** - 将事件推送到外部服务 |

---

## 📋 Requirements / 环境要求

| English | 中文 |
|---------|------|
| Python 3.8+ | Python 3.8及以上版本 |
| Windows (for desktop notifications) | Windows系统（桌面通知功能） |
| BambuLab printer connected to BambuCloud | 已连接拓竹云的BambuLab打印机 |
| Dependencies: `aiohttp`, `paho-mqtt`, `requests`, `cryptography` | 依赖库：`aiohttp`, `paho-mqtt`, `requests`, `cryptography` |

---

## 🚀 Installation / 安装步骤

### 1. Install Dependencies / 安装依赖

```bash
pip install -r requirements.txt
```

### 2. Configuration / 配置

Run `start.bat` for first-time setup, or manually create `config.json`:

运行 `start.bat` 进行首次配置，或手动创建 `config.json`：

```json
{
  "phone": "your-phone-number",
  "serial": "printer-serial-number",
  "api_keys": ["your-secure-api-key"],
  "web_port": 18443,
  "agent_port": 19555,
  "refresh_interval": 30,
  "mqtt_timeout": 10,
  "mqtt_retries": 1
}
```

| Field / 字段 | Description / 说明 |
|-------------|-------------------|
| `phone` | Your BambuCloud registered phone number / 拓竹云注册手机号 |
| `serial` | Printer serial number / 打印机序列号 |
| `api_keys` | API keys for TCP authentication / TCP认证的API密钥 |
| `web_port` | HTTPS web dashboard port / HTTPS仪表盘端口 |
| `agent_port` | TCP API port / TCP API端口 |
| `refresh_interval` | Status refresh interval (seconds) / 状态刷新间隔（秒） |

### 3. Start the Service / 启动服务

```bash
python server.py
```

Or double-click `start.bat`.

或双击 `start.bat`。

---

## 🔗 Access / 访问地址

| English | 中文 | URL |
|---------|------|-----|
| Dashboard / 仪表盘 | Web控制面板 | https://localhost:18443 |
| TCP API | TCP接口 | tcp://localhost:19555 |
| Status API | 状态API | https://localhost:18443/api/status |

---

## 📖 Usage / 使用说明

### Web Dashboard / Web仪表盘

1. Open https://localhost:18443 in your browser
2. Click "Send SMS" to receive verification code
3. Enter the code to login
4. Dashboard will show real-time printer status

1. 在浏览器中打开 https://localhost:18443
2. 点击"发送验证码"获取短信验证码
3. 输入验证码登录
4. 仪表盘将显示实时打印机状态

### TCP API Protocol / TCP API协议

Send JSON commands over TCP:

通过TCP发送JSON命令：

```json
{"cmd": "get_status", "apikey": "your-api-key"}
{"cmd": "subscribe", "apikey": "your-api-key", "events": ["complete", "error", "layer"]}
{"cmd": "ping", "apikey": "your-api-key"}
```

### REST API

| Endpoint | Method | Description / 说明 |
|----------|--------|-------------------|
| `/api/status` | GET | Get current printer status / 获取当前打印机状态 |
| `/api/refresh` | POST | Force refresh status / 强制刷新状态 |
| `/api/login` | POST | Login with verification code / 验证码登录 |
| `/api/send-code` | POST | Send verification code / 发送验证码 |
| `/api/config` | GET | Get current configuration / 获取配置信息 |
| `/api/control` | POST | Control printer (two-step confirm) / 控制打印机（两步确认） |
| `/api/webhook` | POST | Register/unregister webhooks / 注册/注销Webhook |
| `/api/print-history` | GET | Get print history / 获取打印历史 |
| `/api/temp-history` | GET | Get temperature history / 获取温度历史 |
| `/api/notify-rules` | GET/POST | Manage notification rules / 管理通知规则 |

---

## 📁 Project Structure / 项目结构

```
BambuPrinter-OpenSource/
├── auth.py              # Authentication module (SMS/login) / 认证模块（短信/登录）
├── cert_util.py         # SSL certificate generation / SSL证书生成
├── core.py              # Core engine (state management, events) / 核心引擎（状态管理、事件）
├── mqtt_client.py       # MQTT communication with BambuCloud / MQTT通信模块
├── notification.py      # Windows notification module / Windows通知模块
├── server.py            # Main entry point / 主入口
├── web.py               # Web server (dashboard + REST API) / Web服务器（仪表盘+REST API）
├── agent_tcp.py         # TCP server for external agents / 外部程序TCP服务器
├── config.json          # Configuration file / 配置文件
├── token.txt            # Auto-generated login token / 自动生成的登录Token
├── data/                # Data storage / 数据存储
│   ├── print_history.json
│   ├── temp_history.json
│   ├── notify_rules.json
│   └── webhooks.json
├── templates/           # HTML templates / HTML模板
│   └── dashboard.html
├── cert/                # SSL certificates / SSL证书
│   ├── server.crt
│   └── server.key
├── astrbot_plugin_printer/  # AstrBot plugin / AstrBot插件
│   ├── main.py
│   ├── metadata.yaml
│   └── requirements.txt
├── start.bat            # Quick start script / 快速启动脚本
├── install_startup.py   # Auto-start configuration / 开机自启配置
└── requirements.txt     # Python dependencies / Python依赖
```

---

## 🔒 Security Notes / 安全说明

| English | 中文 |
|---------|------|
| HTTPS is enabled with auto-generated self-signed certificates | HTTPS已启用，使用自动生成的自签名证书 |
| API keys are required for TCP connections | TCP连接需要API密钥认证 |
| Token is stored locally | Token存储在本地 |
| Phone number is only used for BambuCloud authentication | 手机号仅用于拓竹云认证 |

---

## 📝 Notification Rules / 通知规则

Custom rules can be configured via the dashboard settings:

可通过仪表盘设置自定义规则：

| Rule Type / 规则类型 | Description / 说明 |
|---------------------|-------------------|
| Print duration alerts | 打印时长提醒 |
| Layer reached alerts | 到达指定层数提醒 |
| Temperature threshold alerts | 温度阈值提醒 |
| Remaining time alerts | 剩余时间提醒 |

---

## 📄 License / 许可证

MIT License - see [LICENSE](LICENSE) file for details.

---

## 🤝 Contributing / 贡献

Contributions are welcome! Please feel free to submit a Pull Request.

欢迎贡献代码！欢迎提交Pull Request。

---

## 📞 Contact / 联系方式

- GitHub: [https://github.com/zsy1023/banbu-a1-Status-acquisition](https://github.com/zsy1023/banbu-a1-Status-acquisition)
- Issues: [https://github.com/zsy1023/banbu-a1-Status-acquisition/issues](https://github.com/zsy1023/banbu-a1-Status-acquisition/issues)