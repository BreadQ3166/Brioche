"""Private, atomic local preferences, remembered LAN devices, per-device profiles and recents."""

import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from .security import data_dir

SCHEMA_VERSION = 2
MAX_DEVICES = 32
MAX_RECENTS = 24


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
            "w",
            encoding="utf-8",
        ) as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class DeviceStore:
    def __init__(self, folder=None):
        self.folder = Path(folder) if folder else data_dir()
        self.path = self.folder / "devices.json"
        self.lock = threading.RLock()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
        except (OSError, ValueError):
            value = {}
        self.value = value
        self.value["schema_version"] = SCHEMA_VERSION
        self.value.setdefault("client_id", secrets.token_hex(16))
        self.value.setdefault("name", socket.gethostname()[:64])
        self.value.setdefault("lan", [])
        self.value.setdefault("settings", {})
        self.value.setdefault("profiles", {})
        self.value.setdefault("recents", [])
        if not isinstance(self.value["lan"], list) or not isinstance(
            self.value["settings"], dict
        ):
            self.value["lan"], self.value["settings"] = [], {}
        if not isinstance(self.value["profiles"], dict):
            self.value["profiles"] = {}
        if not isinstance(self.value["recents"], list):
            self.value["recents"] = []
        self.value["lan"] = [
            d
            for d in self.value["lan"][:MAX_DEVICES]
            if isinstance(d, dict) and isinstance(d.get("ip"), str)
        ]
        self.value["profiles"] = {
            k: dict(v)
            for k, v in list(self.value["profiles"].items())[:64]
            if isinstance(k, str) and isinstance(v, dict)
        }
        self.value["recents"] = [
            r
            for r in self.value["recents"][:MAX_RECENTS]
            if isinstance(r, dict) and isinstance(r.get("key"), str)
        ]

    @property
    def client_id(self):
        return self.value["client_id"]

    @property
    def name(self):
        return self.value["name"]

    def set_name(self, name):
        clean = "".join(c for c in str(name).strip() if c.isprintable())[:64]
        if not clean:
            return
        with self.lock:
            self.value["name"] = clean
            private_json(self.path, self.value)

    def devices(self):
        with self.lock:
            return [dict(d) for d in self.value["lan"]]

    def upsert(self, device):
        with self.lock:
            key = (device["ip"], device["port"])
            pin = device.get("pin") or device.get("observed_pin")
            for item in self.value["lan"]:
                same_endpoint = (item["ip"], item["port"]) == key
                item_pin = item.get("pin") or item.get("observed_pin")
                same_pin = bool(pin and item_pin and pin == item_pin)
                if same_endpoint or same_pin:
                    old_key = f"lan:{item['ip']}:{item['port']}"
                    item.update(device)
                    new_key = f"lan:{item['ip']}:{item['port']}"
                    if old_key != new_key and old_key in self.value["profiles"] and new_key not in self.value["profiles"]:
                        self.value["profiles"][new_key] = self.value["profiles"].pop(old_key)
                    break
            else:
                if len(self.value["lan"]) >= MAX_DEVICES:
                    raise ValueError("局域网目录最多保存32台设备")
                self.value["lan"].append(dict(device))
            private_json(self.path, self.value)

    def remove(self, ip, port):
        with self.lock:
            self.value["lan"] = [
                d for d in self.value["lan"] if (d["ip"], d["port"]) != (ip, port)
            ]
            private_json(self.path, self.value)

    def setting(self, name, default=None):
        return self.value["settings"].get(name, default)

    def configure(self, **values):
        with self.lock:
            self.value["settings"].update(values)
            private_json(self.path, self.value)

    def device_profile(self, device_key):
        with self.lock:
            return dict(self.value["profiles"].get(device_key, {}))

    def set_device_profile(self, device_key, **values):
        if not device_key:
            return {}
        with self.lock:
            profile = dict(self.value["profiles"].get(device_key, {}))
            for k, v in values.items():
                if v is None or v == "":
                    profile.pop(k, None)
                else:
                    profile[k] = v
            if profile:
                self.value["profiles"][device_key] = profile
            else:
                self.value["profiles"].pop(device_key, None)
            private_json(self.path, self.value)
            return dict(profile)

    def all_groups(self):
        with self.lock:
            counts = {}
            for profile in self.value["profiles"].values():
                group = str(profile.get("group", "")).strip()
                if group:
                    counts[group] = counts.get(group, 0) + 1
            return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))

    def record_recent(self, device_key, name, mode, address=""):
        if not device_key:
            return
        with self.lock:
            recents = [r for r in self.value["recents"] if r.get("key") != device_key]
            recents.insert(
                0,
                {
                    "key": device_key,
                    "name": str(name)[:64],
                    "mode": mode,
                    "address": str(address)[:64],
                    "connected_at": int(time.time()),
                },
            )
            self.value["recents"] = recents[:MAX_RECENTS]
            private_json(self.path, self.value)

    def recent_connections(self):
        with self.lock:
            return [dict(r) for r in self.value["recents"]]
