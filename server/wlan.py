"""netsh wlan 封装：无线状态、扫描、连接、配置文件管理。

仅支持 Windows（依赖系统自带 netsh，无第三方依赖）。
所有命令以参数列表形式调用 subprocess，避免 shell 注入与空格转义问题。
netsh 输出为本地化文本（中文系统为 GBK 编码），解析同时兼容中英文标签。
"""
import ctypes
import os
import re
import socket
import subprocess
import tempfile
import time
import uuid
import xml.sax.saxutils as saxutils

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 扫描失败时的友好提示（Windows 11 要求管理员 + 位置服务）
SCAN_PERMISSION_HINT = (
    "扫描需要管理员权限和系统位置服务：请以管理员身份运行 start.bat，"
    "并在 设置 > 隐私和安全性 > 位置 中开启位置服务后重试"
)


class WlanError(RuntimeError):
    pass


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _run(args):
    """执行一条 netsh 命令，返回解码后的 stdout；失败抛 WlanError。"""
    if os.name != "nt":
        raise WlanError("无线操作仅支持 Windows 系统")
    p = subprocess.run(
        ["netsh", *args],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    out = _decode(p.stdout)
    err = _decode(p.stderr)
    if p.returncode != 0:
        msg = (err or out).strip() or f"netsh 退出码 {p.returncode}"
        raise WlanError(msg)
    return out


def _first(text, *labels):
    """按中英文标签匹配 `标签: 值` 行，返回第一个匹配的值或 None。"""
    for line in text.splitlines():
        for lab in labels:
            m = re.match(rf"^\s*{lab}\s*[:：]\s*(.*?)\s*$", line)
            if m:
                return m.group(1)
    return None


def band_of_channel(ch):
    """根据信道号推断频段（netsh 多数版本不直接输出频带）。"""
    if ch is None:
        return None
    try:
        ch = int(ch)
    except (TypeError, ValueError):
        return None
    if 1 <= ch <= 14:
        return "2.4 GHz"
    if ch >= 32:
        return "5 GHz"
    return None


def _local_ip():
    """通过 UDP socket 获取默认路由出口 IP（不实际发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _adapter_ips(guid_bytes=None):
    """按网卡 GUID 精确获取该网卡的 IPv4 / IPv6 地址（GetAdaptersAddresses）。

    guid_bytes 为 wlanapi 枚举出的 16 字节接口 GUID（与 Get-NetAdapter 的
    InterfaceGuid 同一命名空间，实测一致）。匹配失败时返回 (None, None)。
    """
    import ctypes
    import uuid

    AF_INET, AF_INET6 = 2, 23
    IfOperStatusUp = 1

    class SOCKADDR(ctypes.Structure):
        _fields_ = [("sa_family", ctypes.c_ushort), ("sa_data", ctypes.c_ubyte * 126)]

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.POINTER(SOCKADDR)), ("iSockaddrLength", ctypes.c_int)]

    class UNICAST_ADDRESS(ctypes.Structure):
        # SDK IPTypes.h 真实顺序：Alignment(8) -> Next(8) -> Address(16)
        # （Next 在 Address 之前，与直觉相反）
        _fields_ = [("Alignment", ctypes.c_ulonglong),
                    ("Next", ctypes.c_void_p),
                    ("Address", SOCKET_ADDRESS)]

    class ADAPTER_ADDRESSES(ctypes.Structure):
        # 真实布局（Win10+ x64，经 SDK IPTypes.h 核对）：
        # Alignment | Next | AdapterName | FirstUnicastAddress | FirstAnycast |
        # FirstMulticast | FirstDnsServer | DnsSuffix | Description | FriendlyName |
        # PhysicalAddress[8] | PhysicalAddressLength | Flags | Mtu | IfType | OperStatus ...
        _fields_ = [
            ("Length", ctypes.c_ulong), ("IfIndex", ctypes.c_ulong),
            ("Next", ctypes.c_void_p),
            ("AdapterName", ctypes.c_char_p),
            ("FirstUnicastAddress", ctypes.POINTER(UNICAST_ADDRESS)),
            ("FirstAnycastAddress", ctypes.c_void_p),
            ("FirstMulticastAddress", ctypes.c_void_p),
            ("FirstDnsServerAddress", ctypes.c_void_p),
            ("DnsSuffix", ctypes.c_wchar_p),
            ("Description", ctypes.c_wchar_p),
            ("FriendlyName", ctypes.c_wchar_p),
            ("PhysicalAddress", ctypes.c_ubyte * 8),
            ("PhysicalAddressLength", ctypes.c_ulong),
            ("Flags", ctypes.c_ulong),
            ("Mtu", ctypes.c_ulong),
            ("IfType", ctypes.c_ulong),
            ("OperStatus", ctypes.c_ulong),
        ]

    target = None
    if guid_bytes and len(guid_bytes) == 16:
        target = "{%s}" % str(uuid.UUID(bytes_le=bytes(guid_bytes))).upper()

    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(16 * 1024)
    buf = ctypes.create_string_buffer(size.value)
    for _ in range(3):
        rc = iphlpapi.GetAdaptersAddresses(0, 0, None, buf, ctypes.byref(size))
        if rc == 0:
            break
        if rc == 111:  # ERROR_BUFFER_OVERFLOW
            buf = ctypes.create_string_buffer(size.value)
            continue
        return None, None

    ipv4 = ipv6 = None
    cur = ctypes.cast(buf, ctypes.POINTER(ADAPTER_ADDRESSES))
    while cur:
        a = cur.contents
        if target is None or (a.AdapterName or b"").decode("ascii", "replace").upper() == target:
            ua = a.FirstUnicastAddress
            while ua:
                sa_ptr = ua.contents.Address.lpSockaddr
                sa_len = ua.contents.Address.iSockaddrLength
                if not sa_ptr or sa_len < 6:
                    break
                # 按实际长度读取（SOCKADDR 结构体声明的 126 字节数组会越界读）
                raw = ctypes.string_at(ctypes.addressof(sa_ptr.contents), min(sa_len, 28))
                family = int.from_bytes(raw[0:2], "little")
                if family == AF_INET and not ipv4:
                    # sockaddr_in: family(2) port(2) addr(4..8)
                    ipv4 = ".".join(str(b) for b in raw[4:8])
                elif family == AF_INET6 and not ipv6:
                    # sockaddr_in6: family(2) port(2) flowinfo(4) addr(8..24)
                    import ipaddress as _ip
                    try:
                        text = str(_ip.IPv6Address(raw[8:24]))
                    except ValueError:
                        text = None
                    if text:
                        # 优先全局地址（2000::/3），否则链路本地地址
                        if ipv6 is None or raw[8] & 0xE0 == 0x20:
                            ipv6 = text
                if not ua.contents.Next:
                    break
                ua = ctypes.cast(ua.contents.Next, ctypes.POINTER(UNICAST_ADDRESS))
        if not cur.contents.Next:
            break
        cur = ctypes.cast(cur.contents.Next, ctypes.POINTER(ADAPTER_ADDRESSES))
    return ipv4, ipv6


def _get_bss_rssi_map(handle, guid):
    """从 WlanGetNetworkBssList 读取 {BSSID: RSSI(dBm)}。

    与扫描列表同一数据源，保证概览与扫描页对同一网络的信号口径一致。
    """
    class DOT11_SSID(ctypes.Structure):
        _fields_ = [("uLength", ctypes.c_ulong), ("ucSSID", ctypes.c_ubyte * 32)]

    class WLAN_RATE_SET(ctypes.Structure):
        _fields_ = [("uRateSetLength", ctypes.c_ulong), ("usRateSet", ctypes.c_ushort * 126)]

    class WLAN_BSS_ENTRY(ctypes.Structure):
        _fields_ = [
            ("dot11Ssid", DOT11_SSID),
            ("uPhyId", ctypes.c_ulong),
            ("dot11Bssid", ctypes.c_ubyte * 6),
            ("dot11BssType", ctypes.c_ulong),
            ("dot11BssPhyType", ctypes.c_ulong),
            ("lRssi", ctypes.c_long),
            ("uLinkQuality", ctypes.c_ulong),
            ("bInRegDomain", ctypes.c_ubyte),
            ("usBeaconPeriod", ctypes.c_ushort),
            ("ullTimestamp", ctypes.c_ulonglong),
            ("ullHostTimestamp", ctypes.c_ulonglong),
            ("usCapabilityInformation", ctypes.c_ushort),
            ("ulChCenterFrequency", ctypes.c_ulong),
            ("wlanRateSet", WLAN_RATE_SET),
            ("ulIeOffset", ctypes.c_ulong),
            ("ulIeSize", ctypes.c_ulong),
        ]

    class WLAN_BSS_LIST(ctypes.Structure):
        _fields_ = [
            ("dwTotalSize", ctypes.c_ulong),
            ("dwNumberOfItems", ctypes.c_ulong),
            ("wlanBssEntries", WLAN_BSS_ENTRY * 1),
        ]

    wlanapi = ctypes.WinDLL("wlanapi")
    wlanapi.WlanGetNetworkBssList.restype = ctypes.c_ulong
    wlanapi.WlanFreeMemory.restype = None
    bsslist = ctypes.c_void_p()
    if wlanapi.WlanGetNetworkBssList(handle, guid, None, 1, False, None, ctypes.byref(bsslist)) != 0:
        return {}
    out = {}
    try:
        lst = ctypes.cast(bsslist, ctypes.POINTER(WLAN_BSS_LIST)).contents
        items = ctypes.cast(
            ctypes.byref(lst, WLAN_BSS_LIST.wlanBssEntries.offset),
            ctypes.POINTER(WLAN_BSS_ENTRY),
        )
        for i in range(min(lst.dwNumberOfItems, 512)):
            e = items[i]
            bssid = ":".join(f"{b:02X}" for b in e.dot11Bssid)
            out[bssid] = e.lRssi
    finally:
        wlanapi.WlanFreeMemory(bsslist)
    return out


def _is_open_auth(auth):
    if not auth:
        return False
    return ("开放" in auth) or (auth.strip().lower() in ("open", "none"))


def get_status():
    """当前无线接口状态。优先原生 WLAN API（连接状态下 netsh 会被位置策略拦截），失败回退 netsh。"""
    try:
        return get_status_native()
    except (OSError, WlanError) as native_err:
        try:
            return get_status_netsh()
        except WlanError as netsh_err:
            if isinstance(native_err, OSError):
                raise netsh_err
            raise native_err


def get_status_netsh():
    text = _run(["wlan", "show", "interfaces"])
    state = (_first(text, "状态", "State") or "").strip()
    connected = ("已连接" in state) or (state.lower() == "connected")
    ssid = _first(text, "SSID")
    if ssid is not None and ssid.strip() == "":
        ssid = None
    sig = _first(text, "信号", "Signal")
    pct = None
    if sig:
        m = re.search(r"(\d+)", sig)
        if m:
            pct = int(m.group(1))
    ch = _first(text, "信道", "Channel")
    return {
        "connected": connected and ssid is not None,
        "interface": _first(text, "名称", "Name"),
        "state": state,
        "ssid": ssid,
        "bssid": _first(text, "BSSID", "BSSID"),
        "signal": pct,
        "dbm": (round(pct / 2 - 100) if pct is not None else None),
        "channel": int(ch) if ch and ch.isdigit() else ch,
        "band": band_of_channel(ch),
        "auth": _first(text, "身份验证", "Authentication"),
        "radio": _first(text, "无线电类型", "Radio type"),
        "profile": _first(text, "配置文件", "Profile"),
    }
    # netsh 输出中带 GUID，与网卡 GUID 同命名空间，按其精确取 WLAN 网卡 IP
    guid_text = _first(text, "GUID", "GUID")
    guid_bytes = None
    if guid_text:
        try:
            guid_bytes = uuid.UUID(guid_text.strip("{} ")).bytes_le
        except ValueError:
            guid_bytes = None
    result["ip"], result["ipv6"] = _adapter_ips(guid_bytes)
    result["ifaceBus"] = _iface_bus_type(_guid_str(guid_bytes)) if guid_bytes else None
    result["ifaceDesc"] = _first(text, "说明", "Description", "描述")
    if not result["ip"]:
        result["ip"] = _local_ip()
    return result


def get_status_native():
    """原生 WLAN API 状态查询，不受位置策略限制。"""
    import ctypes

    class DOT11_SSID(ctypes.Structure):
        _fields_ = [("uLength", ctypes.c_ulong), ("ucSSID", ctypes.c_ubyte * 32)]

    class WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("dot11Ssid", DOT11_SSID),
            ("dot11BssType", ctypes.c_ulong),
            ("dot11Bssid", ctypes.c_ubyte * 6),
            ("dot11PhyType", ctypes.c_ulong),
            ("uDot11PhyIndex", ctypes.c_ulong),
            ("wlanSignalQuality", ctypes.c_ulong),
            ("ulRxRate", ctypes.c_ulong),
            ("ulTxRate", ctypes.c_ulong),
        ]

    class WLAN_SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("bSecurityEnabled", ctypes.c_long),
            ("bOneXEnabled", ctypes.c_long),
            ("dot11AuthAlgorithm", ctypes.c_ulong),
            ("dot11CipherAlgorithm", ctypes.c_ulong),
        ]

    class WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("isState", ctypes.c_ulong),
            ("wlanConnectionMode", ctypes.c_ulong),
            ("strProfileName", ctypes.c_wchar * 256),
            ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
            ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES),
        ]

    class WLAN_INTERFACE_INFO(ctypes.Structure):
        _fields_ = [
            ("InterfaceGuid", ctypes.c_ubyte * 16),
            ("strInterfaceDescription", ctypes.c_wchar * 256),
            ("isState", ctypes.c_ulong),
        ]

    class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
        _fields_ = [
            ("dwNumberOfItems", ctypes.c_ulong),
            ("dwIndex", ctypes.c_ulong),
            ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
        ]

    wlanapi = ctypes.WinDLL("wlanapi")
    wlanapi.WlanOpenHandle.restype = ctypes.c_ulong
    wlanapi.WlanEnumInterfaces.restype = ctypes.c_ulong
    wlanapi.WlanQueryInterface.restype = ctypes.c_ulong
    wlanapi.WlanFreeMemory.restype = None
    wlanapi.WlanCloseHandle.restype = ctypes.c_ulong

    def check(code, what):
        if code != 0:
            raise WlanError(f"原生 WLAN API {what} 失败（错误码 {code}）")

    def query(handle, guid, opcode):
        size = ctypes.c_ulong()
        data = ctypes.c_void_p()
        check(
            wlanapi.WlanQueryInterface(
                handle, guid, opcode, None, ctypes.byref(size), ctypes.byref(data), None
            ),
            f"查询(opcode={opcode})",
        )
        return data, size

    handle = ctypes.c_void_p()
    ver = ctypes.c_ulong()
    check(wlanapi.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)), "打开句柄")
    try:
        pick = _pick_iface(handle)
        if not pick:
            raise WlanError("未找到无线网卡")
        guid_bytes, ifname = pick
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(guid_bytes)

        # 接口状态
        data, size = query(handle, guid, 6)  # wlan_intf_opcode_interface_state
        state = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong)).contents.value
        wlanapi.WlanFreeMemory(data)
        connected = state == 1

        result = {
            "connected": False,
            "interface": _netsh_iface_name(guid_bytes) or ifname,
            "state": STATE_NAMES.get(state, f"未知({state})"),
            "ssid": None,
            "bssid": None,
            "signal": None,
            "dbm": None,
            "channel": None,
            "band": None,
            "auth": None,
            "radio": None,
            "profile": None,
            "ip": None,
            "ipv6": None,
            "location_limited": False,
        }
        # WLAN 网卡的 IPv4/IPv6（按接口 GUID 精确匹配，避免取到有线网卡）
        result["ip"], result["ipv6"] = _adapter_ips(guid_bytes)
        # 网卡总线类型（USB / 内置 PCI），未连接时也返回
        result["ifaceBus"] = _iface_bus_type(_guid_str(guid_bytes))
        result["ifaceDesc"] = ifname
        if not connected:
            return result

        # 优先尝试完整连接属性（位置服务开启时可用）
        try:
            data, size = query(handle, guid, 7)  # wlan_intf_opcode_current_connection
            conn = ctypes.cast(data, ctypes.POINTER(WLAN_CONNECTION_ATTRIBUTES)).contents
            wlanapi.WlanFreeMemory(data)
            assoc = conn.wlanAssociationAttributes
            sec = conn.wlanSecurityAttributes
            ssid = _ssid_str(assoc.dot11Ssid)
            sig_pct = assoc.wlanSignalQuality
            result.update({
                "connected": bool(ssid),
                "ssid": ssid or None,
                "bssid": ":".join(f"{b:02X}" for b in assoc.dot11Bssid),
                "signal": sig_pct,
                "profile": conn.strProfileName or None,
                "auth": AUTH_NAMES.get(sec.dot11AuthAlgorithm, f"未知({sec.dot11AuthAlgorithm})"),
                "radio": PHY_NAMES.get(assoc.dot11PhyType),
            })
        except WlanError:
            # 位置服务关闭：current_connection 被系统拦截（管理员也无效）。
            # SSID 改从 NLA（Network List Manager）获取并与 WLAN 配置文件求交集，
            # 信号/加密/BSSID 置为受限。
            result["location_limited"] = True
            names = _get_connected_network_names() or []
            matched = [n for n in names if n in set(list_profiles())]
            if len(matched) == 1:
                result["ssid"] = matched[0]
            elif len(names) == 1:
                result["ssid"] = names[0]
            result["connected"] = bool(result["ssid"])

        # 信道（部分驱动不支持该 opcode，失败则忽略）
        try:
            data, size = query(handle, guid, 8)  # wlan_intf_opcode_channel_number
            ch = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong)).contents.value
            wlanapi.WlanFreeMemory(data)
            result["channel"] = ch or None
        except WlanError:
            pass

        # 真实 RSSI：从 BSS 列表按当前 BSSID 匹配（与扫描列表同一数据源，口径一致）
        if result["signal"]:
            dbm = None
            try:
                rssi_map = _get_bss_rssi_map(handle, guid)
                dbm = rssi_map.get(result["bssid"])
            except (WlanError, OSError):
                pass
            result["dbm"] = dbm if dbm is not None else round(result["signal"] / 2 - 100)

        result["band"] = band_of_channel(result["channel"])
        return result
    finally:
        wlanapi.WlanCloseHandle(handle, None)


# ===== 认证 / 加密算法显示名（值来自 wlantypes.h 的 DOT11_AUTH/CIPHER_ALGO） =====
AUTH_NAMES = {
    1: "开放",
    2: "WEP 共享密钥",
    3: "WPA",
    4: "WPA-PSK",
    5: "WPA-NONE",
    6: "WPA2-企业",
    7: "WPA2-个人",
    8: "WPA3-企业",
    9: "WPA3-个人",
    10: "OWE",
    11: "WPA3-企业",
}
CIPHER_NAMES = {
    0: "无",
    1: "WEP40",
    2: "TKIP",
    4: "CCMP",
    5: "WEP104",
    6: "BIP",
    8: "GCMP",
    9: "GCMP-256",
    10: "CCMP-256",
    0x100: "组加密",
}
PHY_NAMES = {
    1: "FHSS",
    2: "DSSS",
    4: "802.11a",
    5: "802.11b",
    6: "802.11g",
    7: "802.11n",
    8: "802.11ac",
    9: "802.11ad",
    10: "802.11ax",
    11: "802.11be",
}


def _netsh_iface_name(guid_bytes=None):
    """从 netsh 获取接口友好名称（如 WLAN）。

    多网卡时按 GUID 匹配对应接口的名称；guid_bytes 为空则返回第一个名称。
    被位置策略拦截等情况下返回 None。
    """
    try:
        text = _run(["wlan", "show", "interfaces"])
        if not guid_bytes:
            return _first(text, "名称", "Name")
        g = _guid_str(guid_bytes).strip("{}").lower()
        cur_name = None
        for line in text.splitlines():
            m = re.match(r"^\s*(?:名称|Name)\s*[:：]\s*(.*?)\s*$", line)
            if m:
                cur_name = m.group(1)
            m2 = re.match(r"^\s*(?:GUID)\s*[:：]\s*(\S+)\s*$", line)
            if m2 and m2.group(1).strip("{} ").lower() == g:
                return cur_name
        return None
    except WlanError:
        return None


def _get_connected_network_names():
    """通过 NLA (Network List Manager COM) 获取所有已连接网络名（WiFi 下即 SSID）。

    不受 Windows 11 位置策略限制。可能同时包含有线网络名，由调用方与
    WLAN 配置文件列表求交集确定 WiFi 的 SSID。
    """
    import ctypes
    import uuid

    ole32 = ctypes.oledll.ole32
    from ctypes import (POINTER, WINFUNCTYPE, byref, c_ulong, c_void_p,
                        cast, c_wchar_p)

    class GUID_C(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def mk(s):
        return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(s).bytes_le)

    CLSCTX_INPROC_SERVER = 1
    NLM_ENUM_NETWORK_ALL = 0x03

    def vfunc(ptr, idx, restype, argtypes=(), *args):
        vtbl = cast(cast(ptr, POINTER(c_void_p)).contents, POINTER(c_void_p))
        fn = WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[idx])
        return fn(ptr, *args)

    try:
        ole32.CoInitializeEx(None, 0)
    except OSError:
        pass  # 已在 COM 模式下

    p = c_void_p()
    hr = 0
    try:
        hr = ole32.CoCreateInstance(
            byref(GUID_C.from_buffer(mk("{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"))),
            None, CLSCTX_INPROC_SERVER,
            byref(GUID_C.from_buffer(mk("{DCB00000-570F-4A9B-8D69-199FDBA5723B}"))),
            byref(p),
        )
    except OSError as e:
        hr = getattr(e, "winerror", -1)
    if hr != 0 or not p.value:
        return None

    names = []
    try:
        enum_nets = c_void_p()
        hr = vfunc(p, 7, c_ulong, (c_ulong, POINTER(c_void_p)),
                   NLM_ENUM_NETWORK_ALL, byref(enum_nets))
        if hr != 0 or not enum_nets.value:
            return None
        while True:
            net = c_void_p()
            fetched = c_ulong()
            hr = vfunc(enum_nets, 8, c_ulong,
                       (c_ulong, POINTER(c_void_p), POINTER(c_ulong)),
                       1, byref(net), byref(fetched))
            if hr != 0 or fetched.value == 0 or not net.value:
                break
            # INetwork::GetName (vtable 7)
            pname = c_wchar_p()
            hr = vfunc(net, 7, c_ulong, (POINTER(c_wchar_p),), byref(pname))
            display = None
            if hr == 0 and pname.value:
                display = pname.value
                # 注：不调用 CoTaskMemFree —— 实测部分系统上释放会触发堆损坏，
                # 每次调用仅泄漏少量网络名字符串，可接受
            # INetwork::GetNetworkConnections (vtable 9)
            enum_conn = c_void_p()
            if vfunc(net, 13, c_ulong, (POINTER(c_void_p),), byref(enum_conn)) != 0:
                continue
            while True:
                conn = c_void_p()
                f2 = c_ulong()
                hr = vfunc(enum_conn, 8, c_ulong,
                           (c_ulong, POINTER(c_void_p), POINTER(c_ulong)),
                           1, byref(conn), byref(f2))
                if hr != 0 or f2.value == 0 or not conn.value:
                    break
                # INetworkConnection::GetAdapterId (vtable 8)
                ag = (ctypes.c_ubyte * 16)()
                hr = vfunc(conn, 12, c_ulong, (POINTER(ctypes.c_ubyte * 16),), byref(ag))
                if hr != 0:
                    continue
                if display and display not in names:
                    names.append(display)
    except Exception:
        pass
    return names


def channel_from_freq(freq_khz):
    """由中心频率(kHz)计算信道号，无法识别返回 None。"""
    mhz = freq_khz / 1000.0
    if 2411.9 <= mhz <= 2472.1:
        return int((mhz - 2407) // 5)
    if abs(mhz - 2484) < 0.5:
        return 14
    if 5169.9 <= mhz <= 5825.1:
        return int((mhz - 5000) // 5)
    if 5954.9 <= mhz <= 7115.1:
        return int((mhz - 5950) // 5)
    return None


def band_from_freq(freq_khz):
    mhz = freq_khz / 1000.0
    if mhz < 3000:
        return "2.4 GHz"
    if mhz < 5900:
        return "5 GHz"
    return "6 GHz"


def _ssid_str(ssid_struct):
    return bytes(ssid_struct.ucSSID[: ssid_struct.uLength]).decode("utf-8", errors="replace")


def get_networks():
    """扫描附近网络，每个 BSSID 一条记录，按信号降序。优先原生 WLAN API，失败回退 netsh。"""
    try:
        return get_networks_native()
    except (OSError, WlanError) as native_err:
        try:
            return get_networks_netsh()
        except WlanError as netsh_err:
            # 两个实现都失败时，优先展示更有指导意义的错误
            if ("位置" in str(netsh_err)) or ("提升" in str(netsh_err)) or isinstance(native_err, OSError):
                raise netsh_err
            raise native_err


def get_networks_netsh():
    """netsh 方式扫描（需要管理员权限 + 位置服务）。"""
    try:
        text = _run(["wlan", "show", "networks", "mode=bssid"])
    except WlanError as e:
        if ("位置" in str(e)) or ("提升" in str(e)):
            raise WlanError(SCAN_PERMISSION_HINT)
        raise
    if "位置" in text and ("权限" in text or "提升" in text):
        raise WlanError(SCAN_PERMISSION_HINT)

    nets = []
    cur_ssid = cur_auth = cur_enc = None
    cur = None

    def flush():
        nonlocal cur
        if cur is not None:
            nets.append(cur)
            cur = None

    for line in text.splitlines():
        m = re.match(r"^\s*SSID\s+\d+\s*[:：]\s*(.*?)\s*$", line)
        if m:
            flush()
            cur_ssid = m.group(1).strip()
            cur_auth = cur_enc = None
            continue
        m = re.match(r"^\s*(?:身份验证|Authentication)\s*[:：]\s*(.*?)\s*$", line)
        if m:
            cur_auth = m.group(1).strip()
            continue
        m = re.match(r"^\s*(?:加密|加密套件|Encryption|Cipher)\s*[:：]\s*(.*?)\s*$", line)
        if m:
            cur_enc = m.group(1).strip()
            continue
        m = re.match(r"^\s*BSSID\s+\d+\s*[:：]\s*(.*?)\s*$", line)
        if m:
            flush()
            cur = {
                "ssid": cur_ssid or "",
                "hidden": not cur_ssid,
                "bssid": m.group(1).strip(),
                "auth": cur_auth,
                "enc": cur_enc,
                "signal": None,
                "channel": None,
                "radio": None,
            }
            continue
        if cur is not None:
            m = re.match(r"^\s*(?:信号|Signal)\s*[:：]\s*(\d+)\s*%", line)
            if m:
                cur["signal"] = int(m.group(1))
                continue
            m = re.match(r"^\s*(?:信道|Channel)\s*[:：]\s*(\d+)", line)
            if m:
                cur["channel"] = int(m.group(1))
                continue
            m = re.match(r"^\s*(?:无线电类型|Radio type)\s*[:：]\s*(.*?)\s*$", line)
            if m:
                cur["radio"] = m.group(1).strip()
    flush()

    for n in nets:
        n["open"] = _is_open_auth(n["auth"])
        n["band"] = band_of_channel(n["channel"])
        n["dbm"] = (round(n["signal"] / 2 - 100) if n["signal"] is not None else None)

    nets.sort(key=lambda n: (n["signal"] if n["signal"] is not None else 0), reverse=True)
    return nets


def get_networks_native():
    """原生 WLAN API 扫描（wlanapi.dll），不受 netsh 的位置权限限制。

    WlanGetAvailableNetworkList 提供认证/加密方式，
    WlanGetNetworkBssList 提供每个 BSSID 的 RSSI(dBm) 与中心频率。
    """
    import ctypes

    class DOT11_SSID(ctypes.Structure):
        _fields_ = [("uLength", ctypes.c_ulong), ("ucSSID", ctypes.c_ubyte * 32)]

    class WLAN_RATE_SET(ctypes.Structure):
        _fields_ = [("uRateSetLength", ctypes.c_ulong), ("usRateSet", ctypes.c_ushort * 126)]

    class WLAN_BSS_ENTRY(ctypes.Structure):
        _fields_ = [
            ("dot11Ssid", DOT11_SSID),
            ("uPhyId", ctypes.c_ulong),
            ("dot11Bssid", ctypes.c_ubyte * 6),
            ("dot11BssType", ctypes.c_ulong),
            ("dot11BssPhyType", ctypes.c_ulong),
            ("lRssi", ctypes.c_long),
            ("uLinkQuality", ctypes.c_ulong),
            ("bInRegDomain", ctypes.c_ubyte),
            ("usBeaconPeriod", ctypes.c_ushort),
            ("ullTimestamp", ctypes.c_ulonglong),
            ("ullHostTimestamp", ctypes.c_ulonglong),
            ("usCapabilityInformation", ctypes.c_ushort),
            ("ulChCenterFrequency", ctypes.c_ulong),
            ("wlanRateSet", WLAN_RATE_SET),
            ("ulIeOffset", ctypes.c_ulong),
            ("ulIeSize", ctypes.c_ulong),
        ]

    class WLAN_AVAILABLE_NETWORK(ctypes.Structure):
        _fields_ = [
            ("strProfileName", ctypes.c_wchar * 256),
            ("dot11Ssid", DOT11_SSID),
            ("dot11BssType", ctypes.c_ulong),
            ("uNumberOfBssids", ctypes.c_ulong),
            ("bNetworkConnectable", ctypes.c_long),
            ("wlanNotConnectableReason", ctypes.c_ulong),
            ("uNumberOfPhyTypes", ctypes.c_ulong),
            ("dot11PhyTypes", ctypes.c_ulong * 8),
            ("bMorePhyTypes", ctypes.c_long),
            ("wlanSignalQuality", ctypes.c_ulong),
            ("bSecurityEnabled", ctypes.c_long),
            ("dot11DefaultAuthAlgorithm", ctypes.c_ulong),
            ("dot11DefaultCipherAlgorithm", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("dwReserved", ctypes.c_ulong),
        ]

    class WLAN_BSS_LIST(ctypes.Structure):
        _fields_ = [
            ("dwTotalSize", ctypes.c_ulong),
            ("dwNumberOfItems", ctypes.c_ulong),
            ("wlanBssEntries", WLAN_BSS_ENTRY * 1),
        ]

    class WLAN_AVAILABLE_NETWORK_LIST(ctypes.Structure):
        _fields_ = [
            ("dwNumberOfItems", ctypes.c_ulong),
            ("dwIndex", ctypes.c_ulong),
            ("Network", WLAN_AVAILABLE_NETWORK * 1),
        ]

    class WLAN_INTERFACE_INFO(ctypes.Structure):
        _fields_ = [
            ("InterfaceGuid", ctypes.c_ubyte * 16),
            ("strInterfaceDescription", ctypes.c_wchar * 256),
            ("isState", ctypes.c_ulong),
        ]

    class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
        _fields_ = [
            ("dwNumberOfItems", ctypes.c_ulong),
            ("dwIndex", ctypes.c_ulong),
            ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
        ]

    wlanapi = ctypes.WinDLL("wlanapi")
    wlanapi.WlanOpenHandle.restype = ctypes.c_ulong
    wlanapi.WlanEnumInterfaces.restype = ctypes.c_ulong
    wlanapi.WlanGetAvailableNetworkList.restype = ctypes.c_ulong
    wlanapi.WlanGetNetworkBssList.restype = ctypes.c_ulong
    wlanapi.WlanFreeMemory.restype = None
    wlanapi.WlanCloseHandle.restype = ctypes.c_ulong

    def check(code, what):
        if code != 0:
            raise WlanError(f"原生 WLAN API {what} 失败（错误码 {code}）")

    handle = ctypes.c_void_p()
    ver = ctypes.c_ulong()
    check(wlanapi.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)), "打开句柄")
    try:
        pick = _pick_iface(handle)
        if not pick:
            raise WlanError("未找到无线网卡")
        guid_bytes, ifname = pick
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(guid_bytes)

        # 网络级信息：认证/加密/是否开放；BSSID 级信息：RSSI / 频率
        # 触发系统扫描后分多轮读取并合并（驱动缓存逐步填充，提高完整性）
        auth_by_ssid = {}
        hidden_sec = None
        by_bssid = {}
        wlanapi.WlanScan(handle, guid, None, None, None)
        prev_count = -1
        for wait_s in (2.5, 2.0, 1.5):
            time.sleep(wait_s)
            avlist = ctypes.POINTER(WLAN_AVAILABLE_NETWORK_LIST)()
            check(wlanapi.WlanGetAvailableNetworkList(handle, guid, 0, None, ctypes.byref(avlist)), "获取可用网络")
            net_items = ctypes.cast(
                ctypes.byref(avlist.contents, WLAN_AVAILABLE_NETWORK_LIST.Network.offset),
                ctypes.POINTER(WLAN_AVAILABLE_NETWORK),
            )
            for i in range(min(avlist.contents.dwNumberOfItems, 512)):
                net = net_items[i]
                ssid = _ssid_str(net.dot11Ssid)
                info = {
                    "auth": AUTH_NAMES.get(net.dot11DefaultAuthAlgorithm, f"未知({net.dot11DefaultAuthAlgorithm})"),
                    "enc": CIPHER_NAMES.get(net.dot11DefaultCipherAlgorithm, f"未知({net.dot11DefaultCipherAlgorithm})"),
                    "auth_code": net.dot11DefaultAuthAlgorithm,
                    "connected": bool(net.dwFlags & 0x1),
                }
                if ssid:
                    auth_by_ssid.setdefault(ssid, info)
                elif hidden_sec is None:
                    hidden_sec = info
            wlanapi.WlanFreeMemory(avlist)

            bsslist = ctypes.POINTER(WLAN_BSS_LIST)()
            check(wlanapi.WlanGetNetworkBssList(handle, guid, None, 3, False, None, ctypes.byref(bsslist)), "获取 BSS 列表")
            items = ctypes.cast(
                ctypes.byref(bsslist.contents, WLAN_BSS_LIST.wlanBssEntries.offset),
                ctypes.POINTER(WLAN_BSS_ENTRY),
            )
            for i in range(min(bsslist.contents.dwNumberOfItems, 512)):
                e = items[i]
                bssid = ":".join(f"{b:02X}" for b in e.dot11Bssid)
                if bssid in by_bssid:
                    continue
                ssid = _ssid_str(e.dot11Ssid)
                hidden = e.dot11Ssid.uLength == 0
                info = auth_by_ssid.get(ssid) if not hidden else hidden_sec
                ch = channel_from_freq(e.ulChCenterFrequency)
                auth_code = (info or {}).get("auth_code")
                by_bssid[bssid] = {
                    "ssid": ssid,
                    "hidden": hidden,
                    "bssid": bssid,
                    "auth": (info or {}).get("auth"),
                    "enc": (info or {}).get("enc"),
                    "auth_code": auth_code,
                    "connected": bool((info or {}).get("connected")) and not hidden,
                    "signal": e.uLinkQuality,
                    "dbm": e.lRssi,
                    "channel": ch,
                    "band": band_from_freq(e.ulChCenterFrequency),
                    "radio": None,
                    "open": auth_code == 1,
                    "interface": ifname,
                }
            wlanapi.WlanFreeMemory(bsslist)
            if len(by_bssid) == prev_count:
                break   # 连续一轮无新增 → 驱动缓存已稳定
            prev_count = len(by_bssid)

        nets = list(by_bssid.values())
        nets.sort(key=lambda n: n["dbm"] if n["dbm"] is not None else -999, reverse=True)
        return nets
    finally:
        wlanapi.WlanCloseHandle(handle, None)


def get_selected_iface_name():
    """当前所选无线网卡的友好名称（如 WLAN 2）；无网卡时返回 None。"""
    api = _wlanapi()
    handle = ctypes.c_void_p()
    ver = ctypes.c_ulong()
    if api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
        return None
    try:
        pick = _pick_iface(handle)
        if not pick:
            return None
        return _netsh_iface_name(pick[0])
    finally:
        api.WlanCloseHandle(handle, None)


def list_profiles(ifname=None):
    """已保存的 WLAN 配置文件名列表；ifname 给定时只列出该接口的配置。

    Windows 的配置文件按适配器分开存储，跨接口不会互相可见。
    """
    args = ["wlan", "show", "profiles"]
    if ifname:
        args.append(f"interface={ifname}")
    try:
        text = _run(args)
    except WlanError as e:
        # 接口上没有任何配置文件时 netsh 返回非零，视为空列表
        if "配置文件" in str(e) or "profile" in str(e).lower():
            return []
        raise
    names = re.findall(
        r"^\s*(?:当前用户配置文件|所有用户配置文件|(?:All |Current )?User Profile)\s*[:：]\s*(.+?)\s*$",
        text, re.M,
    )
    return [n.strip() for n in names]


def get_profile(name):
    """读取单个配置文件详情：认证、加密、连接模式。"""
    text = _run(["wlan", "show", "profile", f"name={name}"])
    auto, auth, enc = True, None, None
    for line in text.splitlines():
        m = re.match(r"^\s*(?:连接模式|Connection mode)\s*[:：]\s*(.*?)\s*$", line)
        if m:
            auto = ("自动" in m.group(1)) or ("auto" in m.group(1).lower())
            continue
        m = re.match(r"^\s*(?:身份验证|Authentication)\s*[:：]\s*(.*?)\s*$", line)
        if m:
            auth = m.group(1).strip()
            continue
        # `show profile` 输出中加密套件的标签是"密码"（值如 CCMP/GCMP）
        m = re.match(r"^\s*(?:密码套件|密码|加密|Encryption|Cipher)\s*[:：]\s*(.*?)\s*$", line)
        if m:
            enc = m.group(1).strip()
    return {"name": name, "auto": auto, "auth": auth, "enc": enc}


def delete_profile(name, ifname=None):
    """删除配置文件；ifname 给定时只删该接口上的（Windows 配置按适配器分开存储）。"""
    args = ["wlan", "delete", "profile", f"name={name}"]
    if ifname:
        args.append(f"interface={ifname}")
    _run(args)


def set_auto_connect(name, auto):
    mode = "auto" if auto else "manual"
    _run(["wlan", "set", "profileparameter", f"name={name}", f"connectionmode={mode}"])


def connect_profile(name):
    """连接到已保存的配置文件（含切换时的断开-重连处理）。"""
    _connect_profile_smart(name)


def _wait_disconnected(timeout=10):
    for _ in range(timeout):
        try:
            if not get_status()["connected"]:
                return True
        except WlanError:
            return True
        time.sleep(1)
    return False


def _connect_profile_smart(name):
    """连接流程：已连接时先断开等待接口空闲，再原生连接；重试后回退 netsh。

    受限环境（位置服务关闭）下已连接状态直接切换会返回错误 87。
    """
    last_err = None
    for attempt in range(3):
        try:
            _wlan_connect_native(name)
            return
        except WlanError as e:
            last_err = e
        try:
            _wlan_disconnect_native()
        except WlanError:
            pass
        _wait_disconnected()
        time.sleep(1)
    try:
        _run(["wlan", "connect", f"name={name}"])
    except WlanError:
        raise last_err


def _wlan_connect_native(profile_name):
    """原生 WlanConnect（wlan_connection_mode_profile），不受位置策略限制。"""
    import ctypes

    class CONNPARAMS(ctypes.Structure):
        _fields_ = [
            ("mode", ctypes.c_ulong),
            ("strProfile", ctypes.c_wchar_p),
            ("pDot11Ssid", ctypes.c_void_p),
            ("pDesiredBssidList", ctypes.c_void_p),
            ("dot11BssType", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
        ]

    wlanapi = ctypes.WinDLL("wlanapi")
    wlanapi.WlanOpenHandle.restype = ctypes.c_ulong
    wlanapi.WlanEnumInterfaces.restype = ctypes.c_ulong
    wlanapi.WlanConnect.restype = ctypes.c_ulong
    wlanapi.WlanCloseHandle.restype = ctypes.c_ulong

    def check(code, what):
        if code != 0:
            raise WlanError(f"原生 WLAN API {what} 失败（错误码 {code}）")

    handle = ctypes.c_void_p()
    ver = ctypes.c_ulong()
    check(wlanapi.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)), "打开句柄")
    try:
        pick = _pick_iface(handle)
        if not pick:
            raise WlanError("未找到无线网卡")
        guid_bytes, _desc = pick
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(guid_bytes)
        # wlan_connection_mode_profile=0，dot11BssType infrastructure=1
        p = CONNPARAMS(mode=0, strProfile=profile_name, pDot11Ssid=None,
                       pDesiredBssidList=None, dot11BssType=1, dwFlags=0)
        check(wlanapi.WlanConnect(handle, guid, ctypes.byref(p), None), "连接")
    finally:
        wlanapi.WlanCloseHandle(handle, None)


class _InterfaceInfo(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", ctypes.c_ubyte * 16),
        ("strInterfaceDescription", ctypes.c_wchar * 256),
        ("isState", ctypes.c_ulong),
    ]


class _InterfaceInfoList(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", ctypes.c_ulong),
        ("dwIndex", ctypes.c_ulong),
        ("InterfaceInfo", _InterfaceInfo * 1),
    ]


STATE_NAMES = {0: "未就绪", 1: "已连接", 2: "正在断开", 3: "已断开连接",
               4: "正在关联", 5: "正在发现", 6: "正在验证"}

_WLANAPI = None


def _wlanapi():
    """延迟初始化并缓存 wlanapi 实例（统一设置 restype）。"""
    global _WLANAPI
    if _WLANAPI is None:
        api = ctypes.WinDLL("wlanapi")
        for f in ("WlanOpenHandle", "WlanCloseHandle", "WlanEnumInterfaces", "WlanFreeMemory",
                  "WlanQueryInterface", "WlanGetAvailableNetworkList", "WlanGetNetworkBssList",
                  "WlanScan", "WlanConnect", "WlanDisconnect"):
            getattr(api, f).restype = ctypes.c_ulong
        _WLANAPI = api
    return _WLANAPI


def _guid_str(guid_bytes):
    import uuid as _uuid
    return "{%s}" % str(_uuid.UUID(bytes_le=bytes(guid_bytes))).upper()


_BUS_TYPE_CACHE = {}


def _iface_bus_type(guid_str):
    """按 NetCfgInstanceId 从注册表反查网卡总线类型（USB / 内置 PCI）。

    网络 Class 键下的 MatchingDeviceId 指明 PnP 设备路径前缀。
    """
    if not guid_str:
        return None
    g = guid_str.strip("{} ").upper()
    if g in _BUS_TYPE_CACHE:
        return _BUS_TYPE_CACHE[g]
    bus = None
    try:
        import winreg
        base = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}")
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(base, i)
                i += 1
            except OSError:
                break
            try:
                k = winreg.OpenKey(base, sub)
                val, _t = winreg.QueryValueEx(k, "NetCfgInstanceId")
                if str(val).strip("{} ").upper() != g:
                    continue
                try:
                    mid, _t2 = winreg.QueryValueEx(k, "MatchingDeviceId")
                    mid = str(mid).upper()
                    if mid.startswith("USB\\"):
                        bus = "USB"
                    elif mid.startswith("PCI\\"):
                        bus = "内置 (PCI)"
                    else:
                        bus = mid.split("\\")[0]
                except FileNotFoundError:
                    bus = None
                break
            except FileNotFoundError:
                continue
    except OSError:
        pass
    _BUS_TYPE_CACHE[g] = bus
    return bus


# 用户选择的无线网卡 GUID（大括号大写字符串；None=自动选第一张）
_SELECTED_IFACE = None


def set_interface(guid_str):
    """选择使用的无线网卡（持久化由调用方负责）。"""
    global _SELECTED_IFACE
    import uuid as _uuid
    g = _uuid.UUID(guid_str.strip("{} "))
    _SELECTED_IFACE = "{%s}" % str(g).upper()


def get_selected_interface():
    return _SELECTED_IFACE


def _enum_ifaces(handle):
    """枚举所有 WLAN 接口，返回 [(guid_bytes, description, state_code)]。"""
    api = _wlanapi()
    il = ctypes.c_void_p()
    if api.WlanEnumInterfaces(handle, None, ctypes.byref(il)) != 0:
        return []
    lst = ctypes.cast(il, ctypes.POINTER(_InterfaceInfoList)).contents
    info_size = ctypes.sizeof(_InterfaceInfo)
    out = []
    for i in range(lst.dwNumberOfItems):
        info = ctypes.cast(
            ctypes.byref(lst, 8 + i * info_size),
            ctypes.POINTER(_InterfaceInfo),
        ).contents
        out.append((bytes(info.InterfaceGuid), info.strInterfaceDescription, info.isState))
    api.WlanFreeMemory(il)
    return out


def _pick_iface(handle):
    """按用户选择挑接口；未选择或选择已失效时取第一张。返回 (guid_bytes, description) 或 None。"""
    ifaces = _enum_ifaces(handle)
    if not ifaces:
        return None
    if _SELECTED_IFACE:
        for gb, desc, _st in ifaces:
            if _guid_str(gb) == _SELECTED_IFACE:
                return gb, desc
    return ifaces[0][:2]


def list_interfaces():
    """列出所有无线网卡（GUID/描述/状态/当前连接的 SSID），供前端选择。"""
    api = _wlanapi()
    handle = ctypes.c_void_p()
    ver = ctypes.c_ulong()
    if api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
        raise WlanError("原生 WLAN API 打开句柄失败")
    try:
        out = []
        for gb, desc, stc in _enum_ifaces(handle):
            ssid = None
            size = ctypes.c_ulong(); data = ctypes.c_void_p()
            guid_arr = (ctypes.c_ubyte * 16).from_buffer_copy(gb)
            if api.WlanQueryInterface(handle, guid_arr, 7, None, ctypes.byref(size), ctypes.byref(data), None) == 0 and data.value:
                try:
                    # WLAN_CONNECTION_ATTRIBUTES: isState(4)+mode(4)+strProfileName(512) -> dot11Ssid @520
                    raw = ctypes.string_at(data.value, min(size.value, 560))
                    ulen = int.from_bytes(raw[520:524], "little")
                    if 0 < ulen <= 32:
                        ssid = raw[524:524 + ulen].decode("utf-8", "replace")
                finally:
                    api.WlanFreeMemory(data)
            out.append({
                "guid": _guid_str(gb),
                "desc": desc,
                "name": _netsh_iface_name(gb),
                "bus": _iface_bus_type(_guid_str(gb)),
                # 以“是否真的连上网”为准（部分 USB 驱动断开后状态会卡在“正在关联”）
                "state": "已连接" if stc == 1 and ssid else "未连接",
                "ssid": ssid if ssid else None,
                "selected": _guid_str(gb) == _SELECTED_IFACE,
            })
        return out
    finally:
        api.WlanCloseHandle(handle, None)


def _profile_xml(ssid, auth, enc, password, auto, hidden):
    esc = saxutils.escape
    key_xml = ""
    if password is not None:
        key_xml = (
            "<sharedKey><keyType>passPhrase</keyType>"
            f"<protected>false</protected><keyMaterial>{esc(password)}</keyMaterial>"
            "</sharedKey>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
        f"<name>{esc(ssid)}</name>\n"
        "<SSIDConfig>\n"
        # 注意：加入 <hex> 元素会在部分 Windows 上触发 0x80001 架构校验失败，仅用 <name>
        f"<SSID><name>{esc(ssid)}</name></SSID>\n"
        f"<nonBroadcast>{'true' if hidden else 'false'}</nonBroadcast>\n"
        "</SSIDConfig>\n"
        "<connectionType>ESS</connectionType>\n"
        f"<connectionMode>{'auto' if auto else 'manual'}</connectionMode>\n"
        "<MSM><security>\n"
        f"<authEncryption><authentication>{auth}</authentication>"
        f"<encryption>{enc}</encryption><useOneX>false</useOneX></authEncryption>\n"
        f"{key_xml}\n"
        "</security></MSM>\n"
        "</WLANProfile>\n"
    )


def _pick_auth(auth_hint):
    """根据扫描到的认证方式选择 profile 的 authentication 值。"""
    hint = (auth_hint or "").upper()
    if "WPA3" in hint or "SAE" in hint:
        return "WPA3SAE"
    return "WPA2PSK"


def connect(ssid, password=None, auto=True, hidden=False, auth_hint=None):
    """连接到指定网络：生成 profile XML → add（当前选中接口）→ connect。

    Windows 的配置文件按适配器分开存储，这里明确指定 interface=，
    保证新保存的网络只落在当前选中的无线网卡上。
    """
    if password is None:
        auth, enc = "open", "none"
    else:
        auth, enc = _pick_auth(auth_hint), "AES"
        if len(password) < 8:
            raise WlanError("无线密码至少 8 位")

    ifname = get_selected_iface_name()

    # add profile 遇到同名配置文件会失败，先删除本接口上的旧配置（忽略失败）
    try:
        delete_profile(ssid, ifname)
    except WlanError:
        pass

    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".xml", prefix="wifiweb_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_profile_xml(ssid, auth, enc, password, auto, hidden))
        add_args = ["wlan", "add", "profile", f"filename={path}", "user=current"]
        if ifname:
            add_args.append(f"interface={ifname}")
        _run(add_args)
    finally:
        if path and os.path.exists(path):
            os.remove(path)

    # 连接：含"已连接时先断开"的切换处理（位置受限环境直接切换会失败）
    _connect_profile_smart(ssid)


def _wlan_disconnect_native():
    """原生 WlanDisconnect。"""
    wlanapi = ctypes.WinDLL("wlanapi")
    wlanapi.WlanOpenHandle.restype = ctypes.c_ulong
    wlanapi.WlanEnumInterfaces.restype = ctypes.c_ulong
    wlanapi.WlanDisconnect.restype = ctypes.c_ulong
    wlanapi.WlanCloseHandle.restype = ctypes.c_ulong

    handle = ctypes.c_void_p()
    ver = ctypes.c_ulong()
    if wlanapi.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(handle)) != 0:
        raise WlanError("原生 WLAN API 打开句柄失败")
    try:
        pick = _pick_iface(handle)
        if not pick:
            raise WlanError("未找到无线网卡")
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(pick[0])
        if wlanapi.WlanDisconnect(handle, guid, None) != 0:
            raise WlanError("原生 WLAN API 断开失败")
    finally:
        wlanapi.WlanCloseHandle(handle, None)


def disconnect():
    """断开当前连接。优先原生 WlanDisconnect（非管理员可用），失败回退 netsh。"""
    try:
        _wlan_disconnect_native()
    except WlanError:
        _run(["wlan", "disconnect"])
