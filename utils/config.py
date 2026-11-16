from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from core import security
from core.relay_client import new_identity_seed
from utils.paths import data_dir, default_download_dir


def config_path() -> Path:
    return data_dir() / "config.json"


@dataclass
class Config:
    machine_id: str = ""
    password: str = ""
    device_name: str = ""
    bind: str = "0.0.0.0"
    port: int = 7070
    codec: str = "jpeg"             # "jpeg" or "webp"
    jpeg_quality: int = 60          # 40-70 is the sweet spot on LAN
    fps: int = 20
    scale: float = 1.0              # <1.0 downsizes frames
    monitor: int = 1                # mss numbering: 0 = all, 1 = primary, ...
    max_sessions: int = 1
    confirm_connections: bool = False   # ask on the host before letting a viewer in
    share_clipboard: bool = True
    discoverable: bool = True       # answer LAN "who has ID x?" broadcasts
    discovery_port: int = 7071
    download_dir: str = ""          # where received files go
    max_file_mb: int = 4096
    # --- access ---
    permanent_enabled: bool = True      # fixed password usable for unattended access
    otp_ttl_minutes: int = 10           # one-time passwords expire after this
    otp_length: int = 8
    allow_control: bool = True          # False = viewers can only watch
    allow_files: bool = True
    # --- internet (relay) ---
    relay_server: str = ""              # host name / IP of your relay; "" = LAN only
    relay_port: int = 7072
    relay_token: str = ""
    relay_tls: bool = False
    relay_private: str = ""             # Ed25519 identity proving ownership of the ID
    # --- settings lock ---
    settings_lock: str = ""             # scrypt hash of the settings password

    def dest_dir(self) -> Path:
        return Path(self.download_dir) if self.download_dir else default_download_dir()

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        data: dict = {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            pass
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        changed = False
        if not cfg.machine_id:
            cfg.machine_id, changed = security.generate_machine_id(), True
        if not cfg.password:
            cfg.password, changed = security.generate_password(), True
        if not cfg.relay_private:
            cfg.relay_private, changed = new_identity_seed(), True
        if not cfg.device_name:
            cfg.device_name = socket.gethostname()
        if changed or not path.exists():
            cfg.save()
        return cfg

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        try:
            os.chmod(path, 0o600)  # contains the password
        except OSError:
            pass
