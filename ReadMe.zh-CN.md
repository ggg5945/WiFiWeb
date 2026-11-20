# WiFiWeb 📶

[English](./ReadMe.md)

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue) ![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Backend](https://img.shields.io/badge/backend-FastAPI-teal) ![Port](https://img.shields.io/badge/port-8086-blue)

在局域网内通过浏览器管理 Windows 无线网络的轻量服务：本机启动一个小型 HTTP 服务，手机 / 平板 / 电脑的浏览器即可完成 Wi-Fi 的**扫描、连接与配置管理**，界面风格对标 OpenWrt/LuCI 的"WiFi 无线桥接 / 扫描加入网络"页面（[参考截图](./doc/search-networks.jpg)）。

**扫描与加入** —— 扫描附近网络：信号强度（dBm + 信号条 + 链路质量%）、SSID、频段（2.4 / 5 / 6 GHz）、信道、模式、BSSID、加密方式，按信号强度降序排列，同名 SSID 多 BSSID（Mesh）按行展开，隐藏网络以斜体"隐藏"显示；列表支持按 SSID 包含 / 频段 / BSSID 包含即时筛选。点击"加入网络"输入密码连接（WPA/WPA2/WPA3-PSK），开放网络免密码并提示明文传输，隐藏网络额外要求输入准确 SSID，也可通过"手动添加网络"直接输入 SSID + 密码（留空视为开放网络）。**新保存的配置只落在当前选中的无线网卡上**。

**自动刷新** —— OpenWrt 式：开关 + 间隔可调（3~600 秒），倒计时由后端计算下发；列表增量更新不闪烁。后端有定时扫描线程：有客户端心跳时按间隔扫描并缓存，超过 `max(30s, 间隔×2)` 无客户端自动停扫并清空缓存。

**多网卡** —— 支持多张无线网卡：顶栏 ⇄ 网卡胶囊点击弹出选择窗（内置网卡排在 USB 前，支持取消按钮与 ESC 关闭），选择按 GUID 持久化到 `app.properties`。切换后扫描页自动重扫、扫描列表"（已保存）"标记与"已保存网络"页同步刷新。遵循 Windows 事实：**配置文件按适配器分开存储**——加入的网络只存当前网卡、忘记只删当前网卡、已保存列表只列当前网卡（为空时给出说明提示）。

**状态概览** —— 当前连接的接口名、SSID、BSSID、信号、信道/频段、加密、IPv4 / IPv6 地址，支持断开连接（二次确认）；系统"定位服务"关闭导致部分信息被 Windows 隐藏时有明确提示。

**纯浏览器操作** —— 无需安装任何客户端，局域网内设备直接访问；手机端抽屉式导航 + 表格卡片化适配；页面通过 URL hash（`#/overview`、`#/scan`、`#/saved`）记忆当前页签。

## Quick Start

### 要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（x64；已在 Windows 11 24H2 完整实测，Win10 未测但 API 兼容） |
| Python | 3.9 及以上（实测 3.12），安装时勾选 "Add to PATH"；需自带 pip |
| Python 依赖 | fastapi、uvicorn（`start.bat` 首次运行时自动安装，也可手动 `pip install -r requirements.txt`） |
| 权限 | 管理员运行（`start.bat` 自动请求提升）：用于写入扫描旁路注册表键与部分连接操作 |
| 浏览器 | Chrome / Edge 88+、Firefox 85+ 等 ES2020 现代浏览器；手机端 Android Chrome / iOS Safari 14+ |
| 网络 | 浏览器设备需与主机同局域网；主机访问 `127.0.0.1:8086` 无需外网 |

### 安装与运行

管理员身份双击 **start.bat**（自动提权 → 写入扫描旁路键 → 安装缺失依赖 → 启动服务并打开浏览器）：

```
双击 start.bat    → 浏览器自动打开 http://127.0.0.1:8086
局域网内其他设备   → http://<主机IP>:8086
```

或手动启动（无虚拟环境，依赖装在系统 Python）：

```
pip install -r requirements.txt
python -m uvicorn server.main:app --host 0.0.0.0 --port 8086
```

多网卡机器首次打开页面会自动弹出网卡选择窗；选择结果写入 `app.properties`，重启服务后保持。

> 操作指南与常见问题排查：[doc/TechnicalNotes.zh-CN.md](./doc/TechnicalNotes.zh-CN.md)。

## Documentation

| 文档 | 内容 |
|---|---|
| [doc/TechnicalNotes.zh-CN.md](./doc/TechnicalNotes.zh-CN.md) | 交互原型与页面结构、操作指南、架构决策、实现细节（位置策略旁路 / 多网卡行为 / 连接与扫描实现 / netsh 解析）、常见问题排查、REST API、路线图 |
| [doc/LocationPolicyResearch.zh-CN.md](./doc/LocationPolicyResearch.zh-CN.md) | Windows 11 24H2 扫描位置策略的逆向分析与官方旁路完整过程 |
| [doc/prototype/index.html](./doc/prototype/index.html) | 交互原型（静态演示数据，含"原型说明"页签） |
| [doc/search-networks.jpg](./doc/search-networks.jpg) | 界面参考截图（OpenWrt，SSID/BSSID 已模糊处理） |

## Security Notes

- 页面可修改本机网络配置且无访问口令，请仅在**可信局域网**内使用
- 所有无线操作通过系统标准 `netsh` / 原生 WLAN API 完成，不做任何提权或驱动操作
- 密码仅写入 Windows 系统的 WLAN 配置文件；`app.properties` 不含任何密码

## License

本仓库未随附 LICENSE 文件，授权条款请与作者确认。
