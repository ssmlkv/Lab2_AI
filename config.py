from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    warehouse_id: str = "warehouse-1"
    timezone: str = "Asia/Yekaterinburg"
    model: str = "openai/gpt-oss-120b"

    @classmethod
    def from_env(cls, data_dir: str | Path | None = None) -> "Settings":
        load_dotenv(ROOT / ".env")
        folder = Path(data_dir or os.getenv("WAREHOUSE_DATA_DIR", "runtime"))
        if not folder.is_absolute():
            folder = ROOT / folder
        zone = os.getenv("WAREHOUSE_TIMEZONE", "Asia/Yekaterinburg")
        ZoneInfo(zone)  # Fail early on an invalid timezone.
        return cls(
            folder.resolve(),
            os.getenv("WAREHOUSE_ID", "warehouse-1"),
            zone,
            os.getenv("OPENAI_MODEL", "openai/gpt-oss-120b"),
        )
