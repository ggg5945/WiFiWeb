# WiFiWeb 技术说明

> English: [TechnicalNotes.md](./TechnicalNotes.md)

> 本文档面向开发者，记录交互原型、页面结构、架构决策、实现细节、REST API 与开发进度。项目概览与使用方式见 [../ReadMe.zh-CN.md](../ReadMe.zh-CN.md)。

## 交互原型与页面结构

交互原型已放置在 [prototype/index.html](./prototype/index.html)，直接用浏览器打开即可点击演示（静态演示数据），页面内含"原型说明"页签（已确认决策、交互流程等）。

页面结构：

```
顶栏：☰(手机端) + 页面标题 ｜ ⇄ 当前网卡（点击切换）+ 全局连接状态
 ├─ 概览            当前无线状态 + 刷新状态 / 切换网卡 / 断开连接
 ├─ 扫描并加入网络   顶部：重新扫描 | 自动刷新开关+间隔+倒计时 | 手动添加网络
 │                  筛选：SSID 包含 / 频段 / BSSID 包含
 │                  表格：信号|SSID|频段|信道|模式|BSSID|加密|加入
 └─ 已保存网络       已保存配置（当前网卡）：连接 / 自动连接开关 / 忘记
```

页面通过 URL hash（`#/overview`、`#/scan`、`#/saved`）记忆当前页签，F5 刷新后不跳回概览。

## Getting Started

| 任务 | 操作 |
|---|---|
| 查看当前连接状态 / 断开 | 概览页 → 状态卡片 / 「断开连接」（二次确认） |
| 刷新当前状态 | 概览页 → 「刷新状态」 |
| 扫描附近网络 | 扫描页 → 🔄 重新扫描（或等待自动刷新倒计时） |
| 连接扫描到的网络 | 扫描页 → 「加入网络」→ 输入密码 |
| 连接隐藏 / 未扫到的网络 | 扫描页 → ➕ 手动添加网络 → 输入 SSID + 密码 |
| 筛选扫描结果 | 扫描页 → 筛选栏（SSID 包含 / 频段 / BSSID 包含） |
| 调整自动刷新 | 扫描页 → 自动刷新开关 + 间隔（3~600 秒） |
| 切换无线网卡 | 顶栏 ⇄ 网卡胶囊 → 选择 → 确定（取消 / ESC 可退出） |
| 管理已保存网络 | 已保存网络页 → 连接 / 自动连接开关 / 忘记（二次确认） |

## 已确认的技术决策

| 项 | 决策 |
|---|---|
| 架构 | **前后端分离**：FastAPI 提供 REST API（JSON），前端独立静态页面（原生 HTML/CSS/JS 单页，无构建步骤）；部署期由后端静态托管前端页面 |
| 无线操作 | 原生 WLAN API（wlanapi.dll，ctypes 调用）+ `netsh wlan` 兜底，无第三方依赖 |
| 多网卡 | 支持多张无线网卡，通过 GUID 选择并持久化到 `app.properties`；**配置文件严格跟随所选适配器**（与 Windows 原生行为一致） |
| 访问控制 | 监听 `0.0.0.0:8086`，**局域网内均可访问，无登录口令** |
| 持久化 | 仅 `app.properties`（网卡选择、自动刷新开关/间隔，页面修改即写回）；网络配置由 Windows WLAN 配置文件（profile）按适配器管理 |
| 端口 | **8086** |
| 启动方式 | `start.bat`（自动请求管理员权限、自动写入扫描旁路注册表键、直接使用系统 Python 启动服务） |

## 实现说明与已知限制

### Windows 11 24H2 位置策略（已通过官方旁路解决）

从 Windows 11 24H2 起，"定位服务"关闭时 Wi-Fi 扫描 API 会被系统拦截，仅微软 Shell（托盘）豁免。
**逆向 `wlansvc.dll` 发现了官方旁路开关**（完整分析见 [LocationPolicyResearch.zh-CN.md](./LocationPolicyResearch.zh-CN.md)）：

```
HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager
  \Capabilities\wlanLocationBypass   RequireWindowsCert=0 (DWORD)
```

`start.bat` 以管理员运行时会自动写入该键，因此**定位服务关闭时全部功能照常可用**（扫描、信号、加密、BSSID 均已实测恢复）。恢复系统默认策略：删除该注册表键。

### 多网卡（适配器）行为

Windows 的 WLAN 配置文件（profile）**按适配器分开存储**，本项目严格遵循该事实：

- 「已保存网络」只列出**当前选中网卡**上的配置；其他网卡保存的网络不会显示，页面为空时给出说明提示
- 「加入网络 / 手动添加」生成的 profile 通过 `netsh wlan add profile interface=<选中网卡>` 明确落在当前网卡上
- 「忘记网络」只删除当前网卡上的配置（`netsh wlan delete profile … interface=<选中网卡>`），其他网卡上保存的同名网络不受影响
- 切换网卡后：扫描页自动触发重新扫描、扫描列表"（已保存）"标记与"已保存网络"页同步刷新，残留筛选条件自动清空

### 连接与扫描实现

- 连接实现：生成 WLAN 配置文件 XML（**不含 `<hex>` 元素**，否则 0x80001 校验失败）→ `netsh wlan add profile`（带 `interface=`）→ 原生 `WlanConnect`（`wlan_connection_mode_profile`，不受位置策略限制）；已连接其他网络时自动先断开再连（受限环境下直接切换返回错误 87），重试后回退 `netsh wlan connect`
- 状态：优先原生 `WlanQueryInterface`，SSID/信号/加密完整；IPv4/IPv6 按 WLAN 网卡 GUID 经 `GetAdaptersAddresses` 精确取值；部分 USB 驱动断开后状态会卡在"正在关联"，已按"是否真的连上网"修正显示
- 扫描：`WlanScan` 触发系统扫描后读取 BSS 列表（等价于打开系统 Wi-Fi 弹窗的行为）
- 后台定时扫描线程：有客户端心跳（打开页面即心跳）时按配置间隔扫描并缓存；超过 `max(30s, 间隔×2)` 无任何客户端 → 停止扫描并清空缓存；`POST /api/scan` 的 `force=false` 优先返回缓存（空/过期时同步补扫），`force=true` 立即真实扫描

### netsh 输出解析

netsh 输出为本地化文本（中文系统 GBK 编码），解析器兼容中英文标签；profile 详情中加密套件标签为"密码"（值如 CCMP）；当前用户配置文件标签为"当前用户配置文件"。

## Troubleshooting

### Windows 11 24H2 下扫描被拦截（提示需要定位服务）

24H2 起"定位服务"关闭时 Wi-Fi 扫描 API 被系统拦截，仅微软 Shell（托盘）豁免。本项目通过逆向 `wlansvc.dll` 找到官方旁路开关（完整分析见 [LocationPolicyResearch.zh-CN.md](./LocationPolicyResearch.zh-CN.md)）：`start.bat` 以管理员运行时自动写入；手动写入 / 恢复：

```
:: 写入旁路（start.bat 已自动执行）
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\Capabilities\wlanLocationBypass" /v RequireWindowsCert /t REG_DWORD /d 0 /f

:: 恢复系统默认策略
reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\Capabilities\wlanLocationBypass" /f
```

写入后定位服务关闭时扫描、信号、加密、BSSID 均可正常工作（已实测）。

### 扫描失败提示管理员权限 / 定位服务

以管理员身份运行 `start.bat`（脚本会自动请求提升），并确认系统"设置 → 隐私和安全性 → 位置"已开启，或使用上一节的旁路键。

### USB 网卡断开后状态显示异常

部分 USB 网卡驱动断开后 WLAN 状态会卡在"正在关联"，页面已按"是否真的连上网"修正显示（显示"未连接"），无需处理。

### 切换网卡后"已保存网络"列表变化 / 找不到某网络的"（已保存）"标记

这是 Windows 的预期行为：WLAN 配置文件按适配器分开存储，每个网卡只看到自己的配置。切换网卡后页面会自动刷新；当前网卡无保存网络时页面会给出说明。见上文「多网卡（适配器）行为」一节。

## API（实际实现）

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/status` | 当前连接状态（接口、SSID、BSSID、信号、信道/频段、IP、IPv6、位置策略受限标志）+ 自动刷新配置 + 下次扫描倒计时 `nextScanIn` |
| POST | `/api/scan` | `{force}`：true 立即真实扫描；false 优先返回缓存（缓存空/过期时同步补扫），返回网络列表 |
| POST | `/api/connect` | 连接网络（SSID、密码、自动连接、隐藏网络标志、加密提示），profile 保存到当前选中网卡 |
| POST | `/api/disconnect` | 断开当前连接 |
| GET | `/api/profiles` | **当前选中网卡**的已保存网络列表 + 当前网卡名 `interface` |
| POST | `/api/profiles/connect` | 连接某个已保存网络 |
| POST | `/api/profiles/auto` | 切换自动连接 |
| DELETE | `/api/profiles/{ssid}` | 忘记网络（只删当前选中网卡上的） |
| GET | `/api/interfaces` | 列出所有无线网卡（GUID/名称/总线类型 USB·PCI/状态/SSID），标记当前选中 |
| POST | `/api/interfaces/select` | 切换选中网卡（GUID），持久化并清空扫描缓存 |
| GET | `/api/config` | 读取自动刷新开关/间隔 |
| POST | `/api/config` | 修改自动刷新开关/间隔（写回 `app.properties`） |

## 目录结构

```
WiFiWeb/
├── doc/
│   ├── search-networks.jpg        # 界面参考截图（openwrt）
│   ├── LocationPolicyResearch.zh-CN.md  # 位置策略研究报告（中文）
│   ├── LocationPolicyResearch.md  # 位置策略研究报告（英文）
│   ├── TechnicalNotes.zh-CN.md  # 技术说明（中文，本文件）
│   ├── TechnicalNotes.md     # 技术说明（英文）
│   └── prototype/                 # 交互原型（含"原型说明"页签）
├── server/                   # 后端（FastAPI）
│   ├── main.py               # 入口：REST API + 静态托管前端 + 后台定时扫描线程
│   └── wlan.py               # 原生 WLAN API + netsh wlan 封装（扫描/连接/状态/多网卡/配置文件管理）
├── frontend/                 # 前端（原生 HTML/CSS/JS 单页）
│   ├── index.html
│   ├── app.js                # 状态轮询、扫描、连接、已保存网络、多网卡切换
│   └── style.css
├── app.properties            # 运行配置（选中网卡 GUID、自动刷新开关/间隔）
├── start.bat                 # 启动脚本
├── ReadMe.zh-CN.md           # 项目概览与使用方式（中文）
├── ReadMe.md                 # 项目概览与使用方式（英文）
└── requirements.txt
```

## 路线图

- [x] 交互原型（`doc/prototype/`）
- [x] 后端：原生 WLAN API + netsh 封装（扫描/连接/状态/配置文件管理）+ REST API
- [x] 前端正式页面（Argon 风格，手机适配，前后端分离）
- [x] start.bat 启动脚本（自动提权 + 首次运行装依赖）
- [x] 实测：连接/断开/重连/已保存网络管理/扫描（实测网络验证通过，wlanLocationBypass 旁路生效）
- [x] Win11 24H2 位置策略逆向与官方旁路（详见 doc/LocationPolicyResearch.zh-CN.md）
- [x] 多网卡支持：选择/切换/持久化，配置文件严格跟随适配器
- [x] 扫描列表筛选、自动刷新（后端定时扫描 + 客户端心跳空闲停扫）
- [x] 顶栏网卡切换入口 + 弹窗取消/ESC，扫描中占位提示，已保存网络空列表说明
