# WiFiWeb Technical Notes

> 中文版：[TechnicalNotes.zh-CN.md](./TechnicalNotes.zh-CN.md)

> Developer-facing notes: interactive prototype, page structure, architecture decisions, implementation details, REST API and progress. Overview & usage: [../ReadMe.md](../ReadMe.md).

## Interactive Prototype & Page Structure

The interactive prototype lives at [prototype/index.html](./prototype/index.html) (Chinese UI) — open it directly in a browser for a clickable demo with static demo data; it includes a "原型说明" (prototype notes) tab covering confirmed decisions and interaction flows.

Page structure:

```
Header: ☰(mobile) + page title ｜ ⇄ current adapter (click to switch) + connection state pill
 ├─ Overview           Current wireless status + refresh / switch adapter / disconnect
 ├─ Scan & Join        Toolbar: rescan | auto-refresh toggle + interval + countdown | manual network
 │                     Filters: SSID contains / band / BSSID contains
 │                     Table: Signal|SSID|Band|Channel|Mode|BSSID|Encryption|Action
 └─ Saved Networks     Profiles on the current adapter: connect / auto-connect toggle / forget
```

The page remembers the current tab via URL hash (`#/overview`, `#/scan`, `#/saved`); pressing F5 does not jump back to Overview.

## Getting Started

| Task | Where / How |
|---|---|
| View current status / disconnect | Overview → status card / "Disconnect" (confirmed by dialog) |
| Refresh current status | Overview → "Refresh" |
| Scan nearby networks | Scan page → 🔄 Rescan (or wait for the auto-refresh countdown) |
| Join a scanned network | Scan page → "Join" → enter password |
| Join a hidden / unswept network | Scan page → ➕ Manual network → SSID + password |
| Filter scan results | Scan page → filter bar (SSID contains / band / BSSID contains) |
| Adjust auto refresh | Scan page → auto-refresh toggle + interval (3–600 s) |
| Switch wireless adapter | Header ⇄ adapter pill → pick one → OK (Cancel / ESC to dismiss) |
| Manage saved networks | Saved page → Connect / auto-connect toggle / Forget (confirmed by dialog) |

## Confirmed Technical Decisions

| Item | Decision |
|---|---|
| Architecture | **Front/back separation**: FastAPI serves a REST API (JSON); the frontend is a standalone static page (plain HTML/CSS/JS SPA, no build step), statically hosted by the backend |
| Wireless operations | Native WLAN API (wlanapi.dll via ctypes) + `netsh wlan` fallback; no third-party dependencies |
| Multi-adapter | Multiple wireless adapters supported; selection by GUID persisted in `app.properties`; **profiles strictly follow the selected adapter** (matches native Windows behavior) |
| Access control | Listens on `0.0.0.0:8086`, **open to the whole LAN, no login** |
| Persistence | Only `app.properties` (selected adapter, auto-refresh toggle/interval, written back on page changes); network profiles are managed by Windows WLAN profiles per adapter |
| Port | **8086** |
| Launch | `start.bat` (requests elevation, writes the scan-bypass registry key, uses the system Python directly) |

## Implementation Details & Known Limitations

### Windows 11 24H2 Location Policy (Solved via Official Bypass)

Since Windows 11 24H2, Wi-Fi scan APIs are blocked when the "location service" is off; only the Microsoft Shell (tray) is exempt. **Reverse-engineering `wlansvc.dll` uncovered an official bypass switch** (full analysis: [LocationPolicyResearch.md](./LocationPolicyResearch.md)):

```
HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager
  \Capabilities\wlanLocationBypass   RequireWindowsCert=0 (DWORD)
```

`start.bat` writes this key automatically when run as administrator, so **all features work normally with the location service off** (scan, signal, encryption, BSSID — all verified). To restore the system default policy, delete the registry key.

### Multi-Adapter Behavior

Windows stores WLAN profiles **per adapter**; this project follows that fact strictly:

- "Saved networks" lists only profiles on the **currently selected adapter**; profiles saved on other adapters are not shown, and an explanatory hint is displayed when the list is empty
- "Join / manual network" writes the profile to the selected adapter explicitly via `netsh wlan add profile interface=<adapter>`
- "Forget" deletes only from the selected adapter (`netsh wlan delete profile … interface=<adapter>`); the same network saved on other adapters is unaffected
- After switching adapters: the scan page triggers a fresh scan, "(saved)" marks and the saved-network page refresh in sync, and stale filter conditions are cleared

### Connection & Scanning Implementation

- Connect: generate a WLAN profile XML (**without a `<hex>` element**, which otherwise fails schema validation with 0x80001) → `netsh wlan add profile` (with `interface=`) → native `WlanConnect` (`wlan_connection_mode_profile`, immune to the location policy); when another network is connected, it disconnects first (direct switching returns error 87 in restricted environments), retries, then falls back to `netsh wlan connect`
- Status: native `WlanQueryInterface` first for complete SSID/signal/encryption; IPv4/IPv6 resolved precisely per WLAN adapter GUID via `GetAdaptersAddresses`; some USB drivers keep the state stuck at "associating" after disconnect — the display is corrected by "actually connected or not"
- Scan: `WlanScan` triggers a system scan, then the BSS list is read (equivalent to opening the native Wi-Fi flyout)
- Background scan thread: while a client heartbeat exists (any open page), it scans and caches at the configured interval; after `max(30s, interval×2)` without any client it stops scanning and clears the cache; `POST /api/scan` with `force=false` prefers the cache (synchronous rescan when empty/stale), `force=true` forces a real scan

### netsh Output Parsing

netsh output is localized text (GBK on Chinese systems); the parser understands both Chinese and English labels; the cipher label in profile details is "密码" (values like CCMP); the per-user profile label is "当前用户配置文件".

## Troubleshooting

### Scanning blocked on Windows 11 24H2 (prompt asks for location service)

Since 24H2, Wi-Fi scan APIs are blocked when the "location service" is off; only the Microsoft Shell (tray) is exempt. This project uses an official bypass switch found by reverse-engineering `wlansvc.dll` (full analysis: [LocationPolicyResearch.md](./LocationPolicyResearch.md)). `start.bat` writes it automatically when run as administrator; to write / restore manually:

```
:: Write the bypass (start.bat does this automatically)
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\Capabilities\wlanLocationBypass" /v RequireWindowsCert /t REG_DWORD /d 0 /f

:: Restore the system default policy
reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\Capabilities\wlanLocationBypass" /f
```

With the key in place, scanning, signal, encryption and BSSID all work with the location service off (verified).

### Scan fails asking for administrator / location service

Run `start.bat` as administrator (the script requests elevation automatically) and make sure "Settings → Privacy & security → Location" is on, or use the bypass key above.

### USB adapter shows an odd state after disconnect

Some USB drivers keep the WLAN state stuck at "associating" after disconnect; the UI already corrects this by "actually connected or not" (shows "not connected"). No action needed.

### The saved-network list changes after switching adapters / a "(saved)" mark is missing

This is expected Windows behavior: WLAN profiles are stored per adapter, and each adapter only sees its own profiles. Pages refresh automatically after switching, and an explanatory hint is shown when the current adapter has no saved networks. See the **Multi-Adapter Behavior** section above.

## API (Implemented)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Current connection (adapter, SSID, BSSID, signal, channel/band, IP, IPv6, location-policy flag) + auto-refresh config + `nextScanIn` countdown |
| POST | `/api/scan` | `{force}`: true = real scan now; false = prefer cache (synchronous rescan when empty/stale); returns the network list |
| POST | `/api/connect` | Connect (SSID, password, auto-connect, hidden flag, auth hint); the profile is saved to the selected adapter |
| POST | `/api/disconnect` | Disconnect |
| GET | `/api/profiles` | Saved networks on the **selected adapter** + adapter name `interface` |
| POST | `/api/profiles/connect` | Connect to a saved network |
| POST | `/api/profiles/auto` | Toggle auto-connect |
| DELETE | `/api/profiles/{ssid}` | Forget (current adapter only) |
| GET | `/api/interfaces` | List all wireless adapters (GUID/name/bus USB·PCI/state/SSID), marking the selected one |
| POST | `/api/interfaces/select` | Switch the selected adapter (GUID); persists the choice and clears the scan cache |
| GET | `/api/config` | Read auto-refresh toggle/interval |
| POST | `/api/config` | Modify auto-refresh toggle/interval (written back to `app.properties`) |

## Directory Structure

```
WiFiWeb/
├── doc/
│   ├── search-networks.jpg        # UI reference screenshot (OpenWrt; SSIDs/BSSIDs blurred)
│   ├── LocationPolicyResearch.md  # Location-policy research report (English)
│   ├── LocationPolicyResearch.md  # Location-policy research report (English, this file)
│   ├── TechnicalNotes.zh-CN.md  # Technical notes (Chinese)
│   ├── TechnicalNotes.md     # Technical notes (English, this file)
│   └── prototype/                 # Interactive prototype (with a prototype-notes tab)
├── server/                   # Backend (FastAPI)
│   ├── main.py               # Entry: REST API + static hosting + background scan thread
│   └── wlan.py               # Native WLAN API + netsh wlan wrapper (scan/connect/status/multi-adapter/profiles)
├── frontend/                 # Frontend (plain HTML/CSS/JS SPA)
│   ├── index.html
│   ├── app.js                # Status polling, scan, connect, saved networks, adapter switching
│   └── style.css
├── app.properties            # Runtime config (selected adapter GUID, auto-refresh toggle/interval)
├── start.bat                 # Launch script
├── ReadMe.zh-CN.md           # Project overview & usage (Chinese)
├── ReadMe.md                 # Project overview & usage (English, this file)
└── requirements.txt
```

## Roadmap

- [x] Interactive prototype (`doc/prototype/`)
- [x] Backend: native WLAN API + netsh wrapper (scan/connect/status/profiles) + REST API
- [x] Production frontend (Argon-style, mobile-friendly, front/back separation)
- [x] start.bat launcher (auto-elevation + first-run dependency install)
- [x] Verified live: connect/disconnect/reconnect/saved-network management/scan (wlanLocationBypass effective)
- [x] Win11 24H2 location-policy reverse engineering & official bypass (see doc/LocationPolicyResearch.md)
- [x] Multi-adapter support: select/switch/persist; profiles strictly follow the adapter
- [x] Scan list filters, auto refresh (backend scan thread + heartbeat-idle stop)
- [x] Header adapter switcher + dialog cancel/ESC, scanning placeholder, saved-network empty hint
