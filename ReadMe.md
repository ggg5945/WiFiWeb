# WiFiWeb 📶

[中文](./ReadMe.zh-CN.md)

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue) ![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Backend](https://img.shields.io/badge/backend-FastAPI-teal) ![Port](https://img.shields.io/badge/port-8086-blue)

A lightweight service for managing Windows wireless networks from a browser over the LAN: it starts a small HTTP service on the local machine, and any phone / tablet / PC with a browser can **scan, join and manage Wi-Fi networks**. The UI is modeled after the OpenWrt/LuCI "Wireless scan & join" page ([reference screenshot](./doc/search-networks.jpg)).

**Scan & join** — Scans nearby networks: signal strength (dBm + bars + link quality %), SSID, band (2.4 / 5 / 6 GHz), channel, mode, BSSID, encryption, sorted by signal strength; same-SSID multi-BSSID (Mesh) entries are expanded per BSSID and hidden networks are shown as an italic "hidden" entry. The list can be filtered instantly by SSID contains / band / BSSID contains. Click "Join" and enter a password (WPA/WPA2/WPA3-PSK); open networks need no password and warn about plaintext traffic, hidden networks additionally require the exact SSID, and "manual network" connects by typing SSID + password directly (empty password = open). **New profiles are saved only to the currently selected wireless adapter.**

**Auto refresh** — OpenWrt-style: a toggle plus an adjustable interval (3–600 s) with the countdown computed by the backend; the list updates incrementally without flicker. A backend scan thread scans and caches at the configured interval while a client heartbeat exists, and stops scanning / clears the cache after `max(30s, interval×2)` without any client.

**Multi-adapter** — Multiple wireless adapters are supported: the header adapter pill (⇄) opens a selector dialog (built-in adapters listed before USB, with a Cancel button and ESC to dismiss), and the selection is persisted by GUID in `app.properties`. After switching, the scan page rescans automatically and the "(saved)" marks / saved-network page refresh in sync. Windows stores WLAN profiles **per adapter** and this project follows that fact — joined networks are saved to the current adapter only, "forget" deletes from the current adapter only, and the saved list shows the current adapter only (with an explanatory hint when empty).

**Status overview** — Current adapter name, SSID, BSSID, signal, channel/band, encryption, IPv4 / IPv6 address, with disconnect support (confirmed by a dialog); an explicit hint is shown when the Windows "location service" hides information.

**Browser-only operation** — No client installation required; LAN devices connect directly. Mobile gets a drawer navigation and card-style tables; the page remembers the current tab via URL hash (`#/overview`, `#/scan`, `#/saved`).

## Quick Start

### Requirements

| Item | Requirement |
|---|---|
| OS | Windows 10 / 11 (x64; fully tested on Windows 11 24H2; Win10 untested but API-compatible) |
| Python | 3.9+ (tested on 3.12), with "Add to PATH" checked; pip required |
| Python deps | fastapi, uvicorn (`start.bat` auto-installs on first run, or run `pip install -r requirements.txt`) |
| Privileges | Run as administrator (`start.bat` requests elevation automatically): needed for the scan-bypass registry key and some connection operations |
| Browser | Chrome / Edge 88+, Firefox 85+ or any ES2020-capable modern browser; Android Chrome / iOS Safari 14+ on mobile |
| Network | Browser devices must reach the host on the LAN; the host itself needs no internet access for `127.0.0.1:8086` |

### Install & Run

Double-click **start.bat** as administrator (auto-elevation → writes the scan-bypass key → installs missing dependencies → starts the service and opens the browser):

```
Double-click start.bat  → browser opens http://127.0.0.1:8086 automatically
Other devices on the LAN → http://<host-IP>:8086
```

Or start manually (no virtualenv; dependencies live in the system Python):

```
pip install -r requirements.txt
python -m uvicorn server.main:app --host 0.0.0.0 --port 8086
```

On multi-adapter machines a selector dialog opens on first load; the selection is persisted to `app.properties` and survives service restarts.

> Step-by-step guide and troubleshooting: [doc/TechnicalNotes.md](./doc/TechnicalNotes.md).

## Documentation

| Doc | Contents |
|---|---|
| [doc/TechnicalNotes.md](./doc/TechnicalNotes.md) | Interactive prototype & page structure, getting-started guide, architecture decisions, implementation details (location-policy bypass / multi-adapter behavior / connection & scanning / netsh parsing), troubleshooting, REST API, roadmap |
| [doc/LocationPolicyResearch.md](./doc/LocationPolicyResearch.md) | Full reverse-engineering of the Windows 11 24H2 scan location policy and the official bypass |
| [doc/prototype/index.html](./doc/prototype/index.html) | Interactive prototype (static demo data, with a prototype-notes tab) |
| [doc/search-networks.jpg](./doc/search-networks.jpg) | UI reference screenshot (OpenWrt; SSIDs/BSSIDs blurred) |

## Security Notes

- The page can modify this machine's network configuration without any login; use it only on a **trusted LAN**
- All wireless operations go through the standard `netsh` / native WLAN API; no privilege escalation or driver-level operations
- Passwords are only written to Windows WLAN profiles; `app.properties` contains no passwords

## License

No LICENSE file is included in this repository; please confirm licensing terms with the author before reuse.
