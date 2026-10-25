from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class Trace:
    def __init__(self, folder: Path, verbose: bool = False):
        folder.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid4().hex
        self.path = folder / f"{self.run_id}.jsonl"
        self.verbose = verbose
        self.events: list[dict] = []

    def emit(self, kind: str, **data) -> None:
        event = {"at": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id,
                 "kind": kind, **data}
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        if self.verbose:
            print(f"[{kind}] " + json.dumps(data, ensure_ascii=False, default=str), flush=True)

