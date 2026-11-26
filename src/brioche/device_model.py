"""Unified device aggregation across LAN and embedded Tailscale channels."""


def _norm_name(name):
    return str(name or "").strip().lower()


def _pick_channel(channels, preferred="auto"):
    lan = channels.get("lan")
    ts = channels.get("tailscale")
    order = (
        ("lan", "tailscale")
        if preferred in ("auto", "lan")
        else ("tailscale", "lan")
    )
    # Prefer an online & connectable channel first
    for mode in order:
        ch = channels.get(mode)
        if ch and ch.get("connectable"):
            return mode, ch
    # Otherwise prefer an online channel
    for mode in order:
        ch = channels.get(mode)
        if ch and (ch.get("online") or ch.get("reachable")):
            return mode, ch
    # Otherwise fallback to preferred present channel
    for mode in order:
        ch = channels.get(mode)
        if ch:
            return mode, ch
    return None, None


def _path_badge(mode, channel):
    if not channel:
        return ("离线", "offline")
    online = bool(channel.get("online") or channel.get("reachable"))
    if not online:
        return ("离线", "offline")
    if channel.get("busy"):
        return ("使用中", "relay")
    if mode == "lan":
        return ("LAN 直连" if channel.get("token") else "LAN · 待配对", "online")
    if not channel.get("sharing"):
        return ("未共享", "offline")
    path = channel.get("path", "")
    if path == "直连":
        return ("外网 P2P", "online")
    if "中继" in path:
        return (path, "relay")
    return ("外网在线", "online")


def build_unified_devices(tailscale_records, lan_records, store):
    """Aggregate Tailscale and LAN entries into unified device records.

    Each input entry is a dict with keys like:
      key, name, ip, port (lan), online/reachable, connectable, busy, sharing,
      status_text, platform, path, token, pin, observed_pin, error
    """
    by_name = {}
    devices = []

    for ts in tailscale_records:
        key = f"ts:{ts['ip']}"
        entry = {
            "key": key,
            "name": ts.get("name", ts["ip"]),
            "platform": ts.get("platform", ""),
            "channels": {"tailscale": dict(ts)},
        }
        devices.append(entry)
        norm = _norm_name(entry["name"])
        if norm:
            by_name.setdefault(norm, entry)

    for lan in lan_records:
        norm = _norm_name(lan.get("name"))
        merged = by_name.get(norm) if norm else None
        if merged and "lan" not in merged["channels"]:
            merged["channels"]["lan"] = dict(lan)
            if not merged["platform"] and lan.get("platform"):
                merged["platform"] = lan["platform"]
        else:
            key = f"lan:{lan['ip']}:{lan['port']}"
            entry = {
                "key": key,
                "name": lan.get("name", lan["ip"]),
                "platform": lan.get("platform", ""),
                "channels": {"lan": dict(lan)},
            }
            devices.append(entry)

    result = []
    for entry in devices:
        # Look up saved profile under canonical key, or fallback to LAN key if merged
        profile = store.device_profile(entry["key"])
        if not profile and "lan" in entry["channels"]:
            lan_ch = entry["channels"]["lan"]
            alt_key = f"lan:{lan_ch['ip']}:{lan_ch['port']}"
            if alt_key != entry["key"]:
                profile = store.device_profile(alt_key)

        preferred = profile.get("preferred_channel", "auto")
        active_mode, active_channel = _pick_channel(entry["channels"], preferred)
        online = any(
            ch.get("online") or ch.get("reachable")
            for ch in entry["channels"].values()
        )
        connectable = any(
            ch.get("connectable") for ch in entry["channels"].values()
        )
        busy = bool(active_channel and active_channel.get("busy"))
        badge_text, badge_tone = _path_badge(active_mode, active_channel)
        display_name = profile.get("alias") or entry["name"]
        address_text = (
            f"{active_channel['ip']}:{active_channel['port']}"
            if active_mode == "lan" and active_channel.get("port", 47995) != 47995
            else (active_channel.get("ip", "") if active_channel else "")
        )
        if len(entry["channels"]) > 1:
            address_text += "  ·  双通道"

        result.append(
            {
                "key": entry["key"],
                "name": entry["name"],
                "display_name": display_name,
                "platform": entry["platform"],
                "channels": entry["channels"],
                "active_mode": active_mode,
                "active_channel": active_channel,
                "online": online,
                "connectable": connectable,
                "busy": busy,
                "status_text": active_channel.get("status_text", "") if active_channel else "",
                "badge_text": badge_text,
                "badge_tone": badge_tone,
                "ip": address_text,
                "favorite": bool(profile.get("favorite", False)),
                "group": str(profile.get("group", "")).strip(),
                "profile": profile,
            }
        )

    result.sort(
        key=lambda d: (
            not d["favorite"],
            not d["connectable"],
            not d["online"],
            d["display_name"].lower(),
        )
    )
    return result


def filter_devices(devices, query="", category="all", group=None):
    q = str(query or "").strip().lower()
    filtered = []
    for d in devices:
        if category == "online" and not d["online"]:
            continue
        if category == "favorite" and not d["favorite"]:
            continue
        if category == "lan" and "lan" not in d["channels"]:
            continue
        if category == "tailscale" and "tailscale" not in d["channels"]:
            continue
        if group and d["group"] != group:
            continue
        if q:
            haystack = " ".join(
                [
                    d["display_name"],
                    d["name"],
                    d["group"],
                    d["platform"],
                    *(ch.get("ip", "") for ch in d["channels"].values()),
                ]
            ).lower()
            if q not in haystack:
                continue
        filtered.append(d)
    return filtered


def effective_stream_config(global_config, profile):
    """Merge per-device profile overrides onto global StreamConfig."""
    from .protocol import StreamConfig

    if not profile:
        return global_config
    resolution = profile.get("resolution") or global_config.resolution
    fps = profile.get("fps") or global_config.fps
    bitrate = profile.get("bitrate") or global_config.bitrate_mbps
    match_display = (
        bool(profile["match_display"])
        if "match_display" in profile
        else global_config.match_display
    )
    if resolution == "native":
        match_display = False
    try:
        return StreamConfig(resolution, int(fps), int(bitrate), match_display)
    except Exception:
        return global_config
