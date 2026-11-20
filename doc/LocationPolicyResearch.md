# Research Report: Windows 11 24H2 Wi-Fi Scan Location Policy

> 中文版：[LocationPolicyResearch.zh-CN.md](./LocationPolicyResearch.zh-CN.md)

> Verified on 2026-11-19 in a Windows 11 24H2 (build 26100) RDP session with the "location service" off.

## Symptom

Clicking the network icon in the system tray (bottom-right) **shows the list of nearby Wi-Fi networks**, but any scan API called from a third-party application fails with error 5 (access denied).

## Root Cause

Windows 11 24H2 performs the location check **inside the wlansvc system service**: when the location service is off, only Microsoft-signed shell-trusted components (Explorer / ShellHost) are allowed through; every other caller is rejected. This is a privacy-policy design, not a permission-configuration problem.

## Test Matrix

| Caller | API | Result |
|---|---|---|
| Standard user / admin / **SYSTEM scheduled task** | `netsh wlan show networks mode=bssid` | ❌ Error 5 (asks for location permission + elevation) |
| Standard user / admin / SYSTEM scheduled task | `WlanGetAvailableNetworkList` (native) | ❌ Error 5 |
| Admin | `WlanGetNetworkBssList` (native) | ❌ Error 5 |
| Admin | `WlanQueryInterface (opcode=current_connection)` | ❌ Error 5 (**even the connection state is unreadable**) |
| Any | `WlanQueryInterface (opcode=interface_state / channel_number)` | ✅ Works |
| Shell (tray UI) | — | ✅ Exempt; the list renders normally |

## Workarounds Attempted and Conclusions

| Approach | Result | Conclusion |
|---|---|---|
| Administrator elevation | ❌ Blocked | Privileges are not the key; the location-service master switch is |
| Scan from a SYSTEM scheduled task | ❌ Error 5 | The system account is not exempt either |
| `rundll32 van.dll,ShowVAN` (classic "View Available Networks" dialog) | ❌ | van.dll has been removed in Win11 |
| UI automation reading the native Wi-Fi flyout | ⚠️ Unreliable | In an RDP session, synthetic clicks cannot open the ShellHost flyout; even if it worked it would depend on session/focus — unsuitable for a service |

## Workable Alternatives (implemented in this project)

| Function | Implementation while the location service is off |
|---|---|
| Current SSID | NLA COM (`INetworkListManager`, network name = SSID) ∩ intersection with the WLAN profile list |
| Channel / band | `WlanQueryInterface (opcode=8)` channel number → band inferred |
| Connecting | `netsh wlan add profile` (XML, **do not include `<hex>`**) + native `WlanConnect` (profile mode; disconnect first when already connected, otherwise error 87) |
| Disconnect / profile management | Native `WlanDisconnect` / netsh profile commands |
| Scanning nearby networks | **No workaround**; the page shows guidance (turn the location service on) |

## ✅ Final Solution: the Official `wlanLocationBypass` Switch (implemented and verified)

Reversing `wlansvc.dll` with REA (Ghidra) (SHA-256 4d0a6df2...) revealed a built-in Microsoft bypass switch:

```
Registry key: HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager
              \Capabilities\wlanLocationBypass
Value:        RequireWindowsCert = 0 (DWORD)   ← allows non-Microsoft-certified apps to skip the location check
```

Reverse-engineering evidence (Ghidra decompilation, referenced at 0x1800efe52 / 0x1800ffdec / 0x180100c60):

- On service startup the key is read; `wlanLocationBypass` is registered with CapabilityAccessManager alongside `location` and `wiFiControl` as one of wlansvc's three capabilities
- In the scan/connect permission-check path, **the `wlanLocationBypass` capability is checked first — if Allowed, the call passes immediately (the location check is skipped entirely)**; otherwise it falls back to the location check (blocked by the location policy)
- When `RequireWindowsCert != 0`, only Microsoft-certified (signed) callers may benefit from the bypass — i.e., the Shell's exemption mechanism

### Verified Effect (location service kept off)

| Function | Before | After adding the key |
|---|---|---|
| Scanning nearby networks | ❌ Error 5 | ✅ Full list (14 networks, real RSSI/BSSID/auth) |
| Current state (signal/encryption/BSSID/radio) | ❌ Restricted | ✅ Complete (opcode current_connection also allowed) |
| Connect / disconnect / profile management | ✅ | ✅ |

### Implementation Notes

1. `start.bat` writes this registry key automatically when run as administrator (elevate once; the key persists)
2. Before scanning, `WlanScan` is called to trigger a system scan (equivalent to opening the native Wi-Fi flyout); the list is read after roughly 2.5 seconds
3. To restore the system default policy, simply delete the registry key

### Other Implementation Notes (netsh output parsing)

- netsh output is localized text (GBK on Chinese systems); the parser understands both Chinese and English labels
- The cipher label in profile details is "密码" (values like CCMP)
- The per-user profile label is "当前用户配置文件" (profiles added with `user=current`)
- **Do not add a `<hex>` element** to the profile XML, otherwise schema validation fails with 0x80001
