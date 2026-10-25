from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str | None = None
    product_name: str | None = None
    expected_qty: StrictInt | None = Field(default=None, ge=0)
    actual_qty: StrictInt | None = Field(default=None, ge=0)

    @field_validator("sku")
    @classmethod
    def normalize_sku(cls, value: str | None) -> str | None:
        if value is None:
            return None 
        result = value.strip().upper()
        return result[:100] or None

    def missing(self) -> list[str]:
        labels = {"sku": "артикул", "expected_qty": "количество по документам", "actual_qty": "фактическое количество"}
        return [label for name, label in labels.items() if getattr(self, name) is None]

    def status(self) -> str:
        if self.missing():
            return "missing_data"
        difference = self.actual_qty - self.expected_qty
        return "shortage" if difference < 0 else "surplus" if difference > 0 else "match"


class IntakeItem(Item):
    pending_id: str | None = Field(default=None, max_length=64)


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[IntakeItem] = Field(max_length=20)
    question: str | None = Field(default=None, max_length=600)


class RegistrarResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=1000)


class JournalRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dedupe_key: str
    event_id: str
    created_at: datetime
    session_id: str
    warehouse_id: str
    sku: str | None
    product_name: str | None
    expected_qty: StrictInt | None = Field(ge=0)
    actual_qty: StrictInt | None = Field(ge=0)
    difference: StrictInt | None
    status: Literal["shortage", "surplus", "match", "missing_data"]
    chat_id: str | None = None
    response: str = ""
    record_id: str = Field(default_factory=lambda: uuid4().hex)


class Journal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    rows: list[JournalRow] = Field(default_factory=list)

