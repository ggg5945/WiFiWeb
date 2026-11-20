# Windows 11 24H2 Wi-Fi 扫描位置策略研究报告

> English: [LocationPolicyResearch.md](./LocationPolicyResearch.md)

> 2026-11-19 实测环境：Windows 11 24H2（build 26100），RDP 会话，"定位服务"关闭状态

## 现象

系统托盘（右下角）点击网络图标**可以看到附近的 Wi-Fi 网络列表**，但第三方应用调用任何扫描 API 均返回错误 5（拒绝访问）。

## 根因

Windows 11 24H2 把位置检查做在 **wlansvc 系统服务内部**：定位服务关闭时，只放行微软签名的 Shell 受信任组件（Explorer / ShellHost），其余调用方一律拒绝。这是隐私策略设计，不是权限配置问题。

## 实测矩阵

| 调用方 | API | 结果 |
|---|---|---|
| 普通 / 管理员 / **SYSTEM 计划任务** | `netsh wlan show networks mode=bssid` | ❌ 错误 5（提示需要位置权限 + 提权） |
| 普通 / 管理员 / SYSTEM 计划任务 | `WlanGetAvailableNetworkList`（原生） | ❌ 错误 5 |
| 管理员 | `WlanGetNetworkBssList`（原生） | ❌ 错误 5 |
| 管理员 | `WlanQueryInterface(opcode=current_connection)` | ❌ 错误 5（**连接状态也无法读取**） |
| 任意 | `WlanQueryInterface(opcode=interface_state / channel_number)` | ✅ 可用 |
| Shell（托盘 UI） | — | ✅ 豁免，列表正常 |

## 尝试过的绕行方案与结论

| 方案 | 结果 | 结论 |
|---|---|---|
| 管理员提权 | ❌ 被拦截 | 权限不是关键，位置服务主开关才是 |
| SYSTEM 计划任务扫描 | ❌ 错误 5 | 系统账户同样不豁免 |
| `rundll32 van.dll,ShowVAN` 经典"查看可用网络"对话框 | ❌ | Win11 已移除 van.dll |
| UI 自动化读取系统 Wi-Fi 弹窗 | ⚠️ 不可靠 | RDP 会话下合成点击无法打开 ShellHost 弹窗；即使可用也依赖会话/焦点，不适合服务化 |

## 可用的替代方案（本项目已实现）

| 功能 | 位置服务关闭时的实现 |
|---|---|
| 获取当前 SSID | NLA COM（INetworkListManager，网络名即 SSID）∩ WLAN 配置文件列表求交集 |
| 获取信道 / 频段 | `WlanQueryInterface(opcode=8)` 信道号 → 推断频段 |
| 连接网络 | `netsh wlan add profile`（XML，**不要含 `<hex>`**）+ 原生 `WlanConnect`（profile 模式；已连接时需先断开，否则错误 87） |
| 断开 / 配置文件管理 | 原生 WlanDisconnect / netsh profile 命令 |
| 扫描附近网络 | **无解**，页面显示引导提示（开启定位服务） |

## ✅ 最终方案：官方旁路开关 wlanLocationBypass（已实现并实测）

用 REA（Ghidra）逆向 `wlansvc.dll`（SHA-256 4d0a6df2...）发现微软内置的旁路开关：

```
注册表键：HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager
          \Capabilities\wlanLocationBypass
值：       RequireWindowsCert = 0 (DWORD)   ← 非微软认证应用也允许跳过位置检查
```

逆向证据（Ghidra 反编译，引用点 0x1800efe52 / 0x1800ffdec / 0x180100c60）：

- 服务启动时读取该键，`wlanLocationBypass` 与 `location`、`wiFiControl` 并列为
  wlansvc 对 CapabilityAccessManager 注册的三个能力项
- 扫描/连接权限检查路径：**先检查 `wlanLocationBypass` 能力，Allowed 则直接放行
  （完全跳过 location 检查）**；否则回落到 location 检查（被位置策略拦截）
- `RequireWindowsCert != 0` 时仅微软认证（签名）调用方可享受旁路——即 Shell 的豁免机制

### 实测效果（定位服务保持关闭）

| 功能 | 之前 | 加键后 |
|---|---|---|
| 扫描附近网络 | ❌ 错误 5 | ✅ 完整列表（14 个网络，真实 RSSI/BSSID/认证） |
| 当前状态（信号/加密/BSSID/radio） | ❌ 受限 | ✅ 完整（opcode current_connection 也放行） |
| 连接/断开/配置文件管理 | ✅ | ✅ |

### 实现要点

1. `start.bat` 以管理员运行时自动写入该注册表键（管理员一次，永久生效）
2. 扫描前调用 `WlanScan` 触发系统扫描（等价于打开系统 Wi-Fi 弹窗的行为），等待约 2.5 秒后读取列表
3. 若要恢复系统默认策略：删除该注册表键即可

### 其他实现要点（netsh 输出解析）

- netsh 输出为本地化文本（中文系统 GBK 编码），解析器兼容中英文标签
- profile 详情中加密套件标签为"密码"（值如 CCMP）
- 当前用户配置文件标签为"当前用户配置文件"（`user=current` 添加的 profile）
- profile XML 中**不要加 `<hex>` 元素**，否则报 0x80001 架构校验失败
