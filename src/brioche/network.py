"""Read-only LAN/Tailscale discovery. Never changes routing, login or ACLs."""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

TAILNET = ipaddress.ip_network("100.64.0.0/10")


def is_tailnet_address(address):
    try:
        ip = ipaddress.ip_address(address)
        return ip.version == 4 and ip in TAILNET
    except ValueError:
        return False


def address_mode(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "lan"
    return "loopback" if ip.is_loopback else "tailscale" if is_tailnet_address(address) else "lan"


def lan_addresses():
    from PySide6.QtNetwork import QNetworkInterface, QAbstractSocket
    addresses = []
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if (not flags & QNetworkInterface.InterfaceFlag.IsUp
                or flags & QNetworkInterface.InterfaceFlag.IsLoopBack
                or interface.name().lower().startswith(("tailscale", "docker", "br-", "veth", "virbr", "tun", "utun", "wg", "vbox", "vmnet"))):
            continue
        for entry in interface.addressEntries():
            if entry.ip().protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                continue
            ip = entry.ip().toString()
            parsed = ipaddress.ip_address(ip)
            if parsed.is_loopback or parsed.is_link_local or is_tailnet_address(ip):
                continue
            addresses.append({"ip": ip, "name": interface.humanReadableName()})
    return sorted(addresses, key=lambda a: (not ipaddress.ip_address(a["ip"]).is_private, a["name"], a["ip"]))


def tailscale_executable():
    executable = shutil.which("tailscale")
    if executable:
        return executable
    candidates = ([Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale/tailscale.exe"]
                  if sys.platform == "win32" else
                  [Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"), Path("/usr/local/bin/tailscale"), Path("/opt/homebrew/bin/tailscale")])
    return next((str(path) for path in candidates if path.is_file()), None)


def parse_tailscale_status(raw, peer_ip=None):
    if not isinstance(raw, dict):
        raise ValueError("无效的 Tailscale 状态")
    state = raw.get("BackendState", "Unknown")
    own = raw.get("Self") if isinstance(raw.get("Self"), dict) else {}
    ips = raw.get("TailscaleIPs") or own.get("TailscaleIPs") or []
    ips = [ip for ip in ips if isinstance(ip, str) and is_tailnet_address(ip)] if isinstance(ips, list) else []
    ready = state == "Running" and bool(ips) and raw.get("TUN", True) is not False
    messages = {"NeedsLogin": "请先登录 Tailscale，再点击刷新", "NeedsMachineAuth": "请在 Tailscale 管理页批准这台设备",
                "Stopped": "请先开启 Tailscale，再点击刷新", "Starting": "Tailscale 正在启动，请稍后刷新"}
    message = "Tailscale 已连接" if ready else messages.get(state, "Tailscale 尚未就绪，请检查客户端")
    if raw.get("TUN") is False:
        message = "请使用 Tailscale 系统网络模式；暂不支持用户态代理模式"
    result = {"ready": ready, "state": state, "ips": ips, "message": message, "path": "待检测"}
    peers = raw.get("Peer") or {}
    if peer_ip and isinstance(peers, dict):
        for peer in peers.values():
            if not isinstance(peer, dict) or peer_ip not in (peer.get("TailscaleIPs") or []):
                continue
            # Relay is also the home DERP region: it alone does not prove active relay use.
            if peer.get("Active"):
                if peer.get("PeerRelay"):
                    result["path"] = "节点中继"
                elif peer.get("CurAddr"):
                    result["path"] = "直连"
                elif peer.get("Relay"):
                    result["path"] = "DERP 中继"
            break
    return result


def tailscale_status(peer_ip=None):
    executable = tailscale_executable()
    if not executable:
        return {"ready": False, "state": "Missing", "ips": [], "message": "尚未安装 Tailscale，请先完成设置", "path": "待检测"}
    try:
        proc = subprocess.run([executable, "status", "--json"], capture_output=True, text=True,
                              timeout=4, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if len(proc.stdout) > 4 * 1024 * 1024:
            raise ValueError("状态数据过大")
        # Logged-out clients may still return valid status JSON with a nonzero exit code.
        return parse_tailscale_status(json.loads(proc.stdout), peer_ip)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {"ready": False, "state": "Unavailable", "ips": [], "message": "无法读取 Tailscale，请确认客户端已启动且当前用户可访问", "path": "待检测"}
