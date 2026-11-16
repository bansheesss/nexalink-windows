"""Saved remote devices with editable labels/tags (like AnyDesk/RustDesk)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from utils.paths import data_dir


@dataclass
class Entry:
    key: str                      # 9-digit ID, or host/IP for direct entries
    label: str = ""
    tags: list[str] = field(default_factory=list)
    note: str = ""
    port: int = 0                 # 0 = default
    last_used: float = 0.0

    @property
    def title(self) -> str:
        return self.label or self.key


class AddressBook:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "addressbook.json"
        self.entries: dict[str, Entry] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        known = {f.name for f in fields(Entry)}
        for d in raw if isinstance(raw, list) else []:
            if isinstance(d, dict) and d.get("key"):
                e = Entry(**{k: v for k, v in d.items() if k in known})
                self.entries[e.key] = e

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(e) for e in self.entries.values()],
                                  ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    def upsert(self, entry: Entry) -> None:
        self.entries[entry.key] = entry
        self.save()

    def remove(self, key: str) -> None:
        if self.entries.pop(key, None) is not None:
            self.save()

    def touch(self, key: str) -> None:
        if key in self.entries:
            self.entries[key].last_used = time.time()
            self.save()

    def sorted(self) -> list[Entry]:
        return sorted(self.entries.values(), key=lambda e: (-e.last_used, e.title.lower()))

    def search(self, text: str) -> list[Entry]:
        t = text.strip().lower()
        return [e for e in self.sorted() if not t or t in e.key.lower()
                or t in e.label.lower() or t in e.note.lower()
                or any(t in g.lower() for g in e.tags)]

    def tags(self) -> list[str]:
        return sorted({g for e in self.entries.values() for g in e.tags})
