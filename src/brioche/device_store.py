"""Private, atomic local preferences and remembered LAN devices."""

import json
import os
from pathlib import Path
import secrets
import socket
import threading
from .security import data_dir


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
        self.value.setdefault("client_id", secrets.token_hex(16))
        self.value.setdefault("name", socket.gethostname()[:64])
        self.value.setdefault("lan", [])
        self.value.setdefault("settings", {})
        if not isinstance(self.value["lan"], list) or not isinstance(
            self.value["settings"], dict
        ):
            self.value["lan"], self.value["settings"] = [], {}
        self.value["lan"] = [
            d
            for d in self.value["lan"][:32]
            if isinstance(d, dict) and isinstance(d.get("ip"), str)
        ]

    @property
    def client_id(self):
        return self.value["client_id"]

    @property
    def name(self):
        return self.value["name"]

    def devices(self):
        with self.lock:
            return [dict(d) for d in self.value["lan"]]

    def upsert(self, device):
        with self.lock:
            key = (device["ip"], device["port"])
            for item in self.value["lan"]:
                if (item["ip"], item["port"]) == key:
                    item.update(device)
                    break
            else:
                if len(self.value["lan"]) >= 32:
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
