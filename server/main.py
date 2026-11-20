"""WiFiWeb 入口：FastAPI REST API + 静态托管前端页面。

启动：python -m uvicorn server.main:app --host 0.0.0.0 --port 8086
      或直接 python server/main.py（自动打开本机浏览器）
"""
import sys
import threading
import time
import webbrowser
import configparser
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wlan

HOST = "0.0.0.0"
PORT = 8086
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "app.properties"

app = FastAPI(title="WiFiWeb", docs_url=None, redoc_url=None)


class InterfaceSelectBody(BaseModel):
    guid: str


def _load_config():
    """读取 app.properties（缺失时返回默认值）。"""
    cfg = {"autoScanEnabled": True, "autoScanInterval": 15, "interfaceGuid": None}
    if CONFIG_PATH.exists():
        parser = configparser.ConfigParser()
        parser.optionxform = str   # 保留键名大小写
        parser.read(CONFIG_PATH, encoding="utf-8")
        if parser.has_section("app"):
            try:
                cfg["autoScanEnabled"] = parser.getboolean("app", "autoScanEnabled")
            except (ValueError, configparser.NoOptionError):
                pass
            try:
                cfg["autoScanInterval"] = max(3, min(600, parser.getint("app", "autoScanInterval")))
            except (ValueError, configparser.NoOptionError):
                pass
            if parser.has_option("app", "interfaceGuid"):
                cfg["interfaceGuid"] = parser.get("app", "interfaceGuid") or None
    return cfg


def _save_config(cfg):
    """只更新键值，完整保留文件现有格式（注释、空行、键名大小写）。

    键不存在时插入到 [app] 节头之后；文件缺失时生成默认模板。
    """
    values = {
        "autoScanEnabled": "true" if cfg["autoScanEnabled"] else "false",
        "autoScanInterval": str(cfg["autoScanInterval"]),
    }
    if cfg.get("interfaceGuid"):
        values["interfaceGuid"] = cfg["interfaceGuid"]
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding="utf-8")
    else:
        text = (
            "# WiFiWeb 配置文件\n"
            "#\n"
            "[app]\n"
            "# 扫描页“自动刷新”开关（true 开启 / false 关闭）\n"
            "# autoScanEnabled = true\n"
            "# 自动刷新间隔（秒，3~600）\n"
            "# autoScanInterval = 15\n"
        )
    for key, val in values.items():
        pattern = re.compile(rf"(?im)^(\s*{re.escape(key)}\s*=\s*)([^\r\n]*?)(\s*)$")
        text, n = pattern.subn(lambda m, v=val: m.group(1) + v + m.group(3), text)
        if n == 0:
            m = re.search(r"(?im)^\s*\[app\][^\r\n]*\r?\n", text)
            insert = f"{key} = {val}\n"
            if m:
                text = text[: m.end()] + insert + text[m.end():]
            else:
                text = text.rstrip("\r\n") + "\n\n[app]\n" + insert
    CONFIG_PATH.write_text(text, encoding="utf-8")
    CONFIG_PATH.write_text(text, encoding="utf-8")


class ConfigBody(BaseModel):
    autoScanEnabled: Optional[bool] = None
    autoScanInterval: Optional[int] = None


@app.get("/api/config")
def api_config_get():
    return _load_config()


@app.post("/api/config")
def api_config_set(body: ConfigBody):
    cfg = _load_config()
    if body.autoScanEnabled is not None:
        cfg["autoScanEnabled"] = bool(body.autoScanEnabled)
    if body.autoScanInterval is not None:
        cfg["autoScanInterval"] = max(3, min(600, int(body.autoScanInterval)))
    _save_config(cfg)
    return cfg


class ConnectBody(BaseModel):
    ssid: str
    password: Optional[str] = None
    auto: bool = True
    hidden: bool = False
    auth_hint: Optional[str] = None


class AutoBody(BaseModel):
    ssid: str
    auto: bool


def _api(func, *args, **kwargs):
    """统一错误包装：WlanError -> HTTP 400。"""
    try:
        return func(*args, **kwargs)
    except wlan.WlanError as e:
        raise HTTPException(status_code=400, detail=str(e))


# 应用 app.properties 中保存的无线网卡选择（多网卡场景）
# 注意：必须在 _load_config 定义之后执行
try:
    if _load_config().get("interfaceGuid"):
        wlan.set_interface(_load_config()["interfaceGuid"])
except Exception:
    pass


@app.get("/api/interfaces")
def api_interfaces():
    selected = wlan.get_selected_interface()
    ifaces = _api(wlan.list_interfaces)
    for it in ifaces:
        it["selected"] = (it["guid"] == selected)
    return {"selected": selected, "list": ifaces}


@app.post("/api/interfaces/select")
def api_interface_select(body: InterfaceSelectBody):
    ifaces = _api(wlan.list_interfaces)
    match = [it for it in ifaces if it["guid"] == body.guid]
    if not match:
        raise HTTPException(status_code=400, detail="指定的无线网卡不存在")
    wlan.set_interface(body.guid)
    cfg = _load_config()
    cfg["interfaceGuid"] = body.guid
    _save_config(cfg)
    # 切换网卡后清空扫描缓存（旧缓存属于另一张网卡）
    with _scan_lock:
        _scan_state["networks"] = []
        _scan_state["ts"] = 0.0
    return {"ok": True, "guid": body.guid}


@app.get("/api/status")
def api_status():
    _touch_client()   # 客户端心跳：用于判断是否还有人在看页面
    data = _api(wlan.get_status)
    # 合并配置信息，前端据此同步开关/间隔（配置文件手动改动也会跟随）
    data.update(_load_config())
    # 下次自动扫描的倒计时（秒）由后端计算下发；关闭/空闲时为 null
    interval = max(3, int(data["autoScanInterval"]))
    with _scan_lock:
        ts = _scan_state["ts"]
        idle_for = time.time() - _scan_state["last_client"]
    if not data["autoScanEnabled"] or idle_for > max(30, interval * 2):
        data["nextScanIn"] = None
    elif ts <= 0:
        data["nextScanIn"] = 0
    else:
        data["nextScanIn"] = max(0, round(interval - (time.time() - ts)))
    return data


class ScanBody(BaseModel):
    force: bool = False   # true=立即触发真实扫描；false=返回后端缓存列表


# ===== 后端定时扫描：有客户端时按间隔扫描，无客户端时停止并清空缓存 =====
_scan_state = {"networks": [], "ts": 0.0, "last_client": time.time()}
_scan_lock = threading.Lock()


def _touch_client():
    with _scan_lock:
        _scan_state["last_client"] = time.time()


def _scan_worker():
    """后台扫描线程：定时刷新内存中的 Wi-Fi 列表。

    - 开关/间隔来自 app.properties，页面修改后即时生效
    - 超过阈值（max(30s, 间隔*2)）无任何客户端心跳 → 停止扫描并清空缓存
    """
    while True:
        try:
            cfg = _load_config()
            interval = max(3, int(cfg["autoScanInterval"]))
            idle_for = time.time() - _scan_state["last_client"]
            threshold = max(30, interval * 2)
            if cfg["autoScanEnabled"] and idle_for <= threshold:
                nets = wlan.get_networks()
                with _scan_lock:
                    _scan_state["networks"] = nets
                    _scan_state["ts"] = time.time()
                time.sleep(interval)
                continue
            # 无客户端或开关关闭：停止扫描并清空内存列表
            with _scan_lock:
                if _scan_state["networks"]:
                    _scan_state["networks"] = []
                    _scan_state["ts"] = 0.0
            time.sleep(5)   # 空闲态低频轮询，客户端一回来立刻恢复扫描
        except Exception:
            time.sleep(5)   # 单次扫描失败保留旧缓存，稍后重试


threading.Thread(target=_scan_worker, daemon=True).start()


@app.post("/api/scan")
def api_scan(body: Optional[ScanBody] = None):
    _touch_client()
    force = bool(body and body.force)
    with _scan_lock:
        cached = list(_scan_state["networks"])
        age = time.time() - _scan_state["ts"]
    if force or not cached or age > max(10, _load_config()["autoScanInterval"]):
        # 缓存为空/过期，或客户端明确要求立即扫描 → 同步执行一次
        try:
            nets = _api(wlan.get_networks)
            with _scan_lock:
                _scan_state["networks"] = nets
                _scan_state["ts"] = time.time()
            return {"networks": nets}
        except HTTPException as e:
            # 扫描失败时如有旧缓存则继续返回旧数据
            if not cached:
                raise e
    with _scan_lock:
        return {"networks": _scan_state["networks"]}


@app.post("/api/connect")
def api_connect(body: ConnectBody):
    _api(
        wlan.connect,
        body.ssid,
        password=body.password,
        auto=body.auto,
        hidden=body.hidden,
        auth_hint=body.auth_hint,
    )
    return {"started": True, "ssid": body.ssid}


@app.post("/api/disconnect")
def api_disconnect():
    _api(wlan.disconnect)
    return {"ok": True}


@app.get("/api/profiles")
def api_profiles():
    def _load():
        st = wlan.get_status()
        ifname = wlan.get_selected_iface_name()
        out = []
        # Windows 的 WLAN 配置文件按适配器分开存储，这里只列当前网卡上的配置
        for name in wlan.list_profiles(ifname):
            d = wlan.get_profile(name)
            d["connected"] = bool(st["connected"] and st["ssid"] == name)
            out.append(d)
        return {"profiles": out, "interface": ifname}

    return _api(_load)


@app.post("/api/profiles/connect")
def api_profile_connect(body: ConnectBody):
    _api(wlan.connect_profile, body.ssid)
    return {"started": True, "ssid": body.ssid}


@app.post("/api/profiles/auto")
def api_profile_auto(body: AutoBody):
    _api(wlan.set_auto_connect, body.ssid, body.auto)
    return {"ok": True}


@app.delete("/api/profiles/{ssid}")
def api_profile_delete(ssid: str):
    # 配置文件按适配器分开存储，只删当前选中网卡上的
    _api(wlan.delete_profile, ssid, wlan.get_selected_iface_name())
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    threading.Timer(1.5, webbrowser.open, (f"http://127.0.0.1:{PORT}",)).start()
    uvicorn.run(app, host=HOST, port=PORT)
