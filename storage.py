from __future__ import annotations

from datetime import datetime
import ast
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from langchain_core.tools import tool

from .models import Item, Journal, JournalRow


COLUMNS = tuple(JournalRow.model_fields)
READ_JOURNAL = f"SELECT {', '.join(COLUMNS)} FROM discrepancies ORDER BY rowid"
SCHEMA = '''
    CREATE TABLE IF NOT EXISTS discrepancies (
        dedupe_key TEXT PRIMARY KEY NOT NULL,
        event_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        session_id TEXT NOT NULL,
        warehouse_id TEXT NOT NULL,
        sku TEXT,
        product_name TEXT,
        expected_qty INTEGER CHECK(expected_qty IS NULL OR
            (typeof(expected_qty) = 'integer' AND expected_qty >= 0)),
        actual_qty INTEGER CHECK(actual_qty IS NULL OR
            (typeof(actual_qty) = 'integer' AND actual_qty >= 0)),
        difference INTEGER CHECK(difference IS NULL OR typeof(difference) = 'integer'),
        status TEXT NOT NULL CHECK(status IN ('shortage', 'surplus', 'match', 'missing_data')),
        chat_id TEXT,
        response TEXT NOT NULL DEFAULT '',
        record_id TEXT NOT NULL UNIQUE,
        CHECK(
            (status = 'missing_data' AND difference IS NULL AND
                (sku IS NULL OR expected_qty IS NULL OR actual_qty IS NULL))
            OR
            (sku IS NOT NULL AND expected_qty IS NOT NULL AND actual_qty IS NOT NULL
                AND difference IS NOT NULL AND difference = actual_qty - expected_qty AND (
                    (status = 'shortage' AND difference < 0) OR
                    (status = 'surplus' AND difference > 0) OR
                    (status = 'match' AND difference = 0)))
        )
    );
'''
JOURNAL_VIEW = '''
    CREATE VIEW IF NOT EXISTS journal AS SELECT
        replace(substr(created_at, 1, 19), 'T', ' ') AS "Дата и время",
        COALESCE(chat_id, session_id) AS "Чат / сессия",
        sku AS "Артикул", product_name AS "Товар",
        expected_qty AS "По документам", actual_qty AS "Фактически",
        difference AS "Разница",
        CASE status WHEN 'shortage' THEN 'Недостача' WHEN 'surplus' THEN 'Излишек'
            WHEN 'match' THEN 'Совпадение' ELSE 'Неполные данные' END AS "Статус",
        response AS "Ответ"
    FROM discrepancies ORDER BY rowid;
'''


def initialize_database(path: Path) -> None:
    """Create or migrate the schema; agents insert journal rows through MCP."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=30) as connection:
        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='discrepancies'").fetchone()
        old_columns = {row[1] for row in connection.execute("PRAGMA table_info(discrepancies)")}
        if existing and ("'missing_data'" not in existing[0] or "record_id" not in old_columns):
            stage = "before_all_statuses" if "'missing_data'" not in existing[0] else "before_inplace_updates"
            backup = path.with_name(f"{path.stem}.{stage}.{uuid4().hex[:8]}{path.suffix}")
            with sqlite3.connect(backup) as snapshot:
                connection.backup(snapshot)
            old_table = "discrepancies_legacy_" + uuid4().hex[:8]
            columns = ", ".join(COLUMNS)
            source = ", ".join("lower(hex(randomblob(16)))" if column == "record_id" and column not in old_columns
                               else column for column in COLUMNS)
            connection.executescript(
                "BEGIN IMMEDIATE; DROP VIEW IF EXISTS journal; "
                f"ALTER TABLE discrepancies RENAME TO {old_table}; " + SCHEMA +
                f"INSERT INTO discrepancies ({columns}) SELECT {source} FROM {old_table} ORDER BY rowid; "
                f"DROP TABLE {old_table}; " + JOURNAL_VIEW + "PRAGMA user_version=3; COMMIT;")
        else:
            connection.executescript(SCHEMA + JOURNAL_VIEW + "PRAGMA user_version=3;")


def journal_from_result(content: str) -> Journal:
    try:
        rows = json.loads(content)
    except json.JSONDecodeError:
        try:
            rows = ast.literal_eval(content)
        except (ValueError, SyntaxError) as exc:
            raise RuntimeError("MCP-сервер вернул ошибку или неверный результат SQL") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("MCP-сервер не вернул список строк журнала")
    return Journal(rows=[JournalRow.model_validate(row) for row in rows])


def sql_value(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "CAST(X'" + str(value).encode("utf-8").hex() + "' AS TEXT)"


def insert_query(rows: list[JournalRow]) -> str:
    values = []
    for row in rows:
        record = row.model_dump(mode="json")
        values.append("(" + ", ".join(sql_value(record[column]) for column in COLUMNS) + ")")
    return f"INSERT INTO discrepancies ({', '.join(COLUMNS)}) VALUES " + ", ".join(values)


def save_query(rows: list[JournalRow]) -> str:
    mutable = ("dedupe_key", "sku", "product_name", "expected_qty", "actual_qty", "difference", "status", "response")
    return (insert_query(rows) + " ON CONFLICT(record_id) DO UPDATE SET " +
            ", ".join(f"{column}=excluded.{column}" for column in mutable))


def pending_query(session_id: str, warehouse_id: str) -> str:
    return (f"SELECT {', '.join(COLUMNS)} FROM discrepancies WHERE status='missing_data' "
            f"AND session_id={sql_value(session_id)} AND warehouse_id={sql_value(warehouse_id)} ORDER BY rowid")


def pending_items(journal: Journal, session_id: str, warehouse_id: str) -> list[dict]:
    return [{**{field: getattr(row, field) for field in Item.model_fields}, "pending_id": row.record_id}
            for row in journal.rows if row.status == "missing_data"
            and row.session_id == session_id and row.warehouse_id == warehouse_id]


def dedupe_key(item, day: str) -> str:
    return "|".join(map(str, [day, item.sku, item.expected_qty, item.actual_qty, item.status()]))


def registrar_tools(items, session_id, event_id, settings, mcp, trace, now: datetime, pending_ids=None):
    pending_ids = pending_ids or [None] * len(items)
    if len(pending_ids) != len(items):
        raise ValueError("Неверное количество ссылок на незавершённые заявки")
    receipt = {"ready": False, "outcomes": []}
    cache = {"journal": None}
    calculated: set[tuple[int, int]] = set()

    @tool
    def calculate_difference(expected_qty: int, actual_qty: int) -> dict:
        """Compute actual minus expected for nonnegative integer warehouse quantities."""
        if type(expected_qty) is not int or type(actual_qty) is not int or min(expected_qty, actual_qty) < 0:
            raise ValueError("Количества должны быть целыми неотрицательными числами")
        calculated.add((expected_qty, actual_qty))
        return {"difference": actual_qty - expected_qty}

    @tool
    async def read_warehouse_journal() -> dict:
        """Read the SQLite discrepancy journal through an existing SQLite MCP server."""
        content = await mcp.call("read_query", {"query": READ_JOURNAL}, trace)
        journal = journal_from_result(content)
        cache["journal"] = journal
        # Do not expose old users' products to the model; the guard checks all keys.
        return {"row_count": len(journal.rows), "loaded": True}

    @tool
    async def register_discrepancies() -> dict:
        """Save every item's check result through SQLite MCP, including matches and incomplete items."""
        if receipt["ready"]:
            return receipt
        if cache["journal"] is None:
            raise RuntimeError("Сначала вызови read_warehouse_journal")
        journal = cache["journal"].model_copy(deep=True)
        by_key = {row.dedupe_key: row for row in journal.rows}
        by_id = {row.record_id: row for row in journal.rows}
        outcomes, changed_rows = [], []
        inserted = updated = 0
        touched = set()
        for position, item in enumerate(items):
            status = item.status()
            outcome = {**item.model_dump(), "status": status, "difference": None, "registration": "not_required"}
            if status != "missing_data":
                pair = (item.expected_qty, item.actual_qty)
                if pair not in calculated:
                    raise RuntimeError("Не вызван calculate_difference для полного товара")
                outcome["difference"] = item.actual_qty - item.expected_qty
            pending_id = pending_ids[position]
            target = by_id.get(pending_id) if pending_id else None
            if pending_id:
                if (target is None or target.status != "missing_data" or
                        target.session_id != session_id or target.warehouse_id != settings.warehouse_id):
                    raise RuntimeError("Нельзя дописать чужую или уже завершённую заявку")
                if pending_id in touched:
                    raise RuntimeError("Одна незавершённая заявка указана несколько раз")
                touched.add(pending_id)
            key = ((target.dedupe_key if target else f"missing|{event_id}|{position}")
                   if status == "missing_data" else dedupe_key(item, now.date().isoformat()))
            existing = by_key.get(key)
            if target and existing and existing.record_id != target.record_id:
                # Two independently opened requests may reach the same result.
                # Complete the selected row without deleting either request;
                # new repeated full messages still match the existing daily key.
                key = target.dedupe_key
            if target:
                outcome["registration"] = "updated"
                row = target.model_copy(update={**item.model_dump(), "dedupe_key": key,
                    "difference": outcome["difference"], "status": status,
                    "response": render_reply({"outcomes": [outcome]})})
                journal.rows = [row if previous.record_id == target.record_id else previous for previous in journal.rows]
                by_key.pop(target.dedupe_key)
                updated += 1
            elif existing:
                outcome["registration"] = "duplicate"
                outcome["record_id"] = existing.record_id
                outcomes.append(outcome)
                continue
            else:
                outcome["registration"] = "saved"
                row = JournalRow(dedupe_key=key, event_id=event_id,
                    created_at=now, session_id=session_id, warehouse_id=settings.warehouse_id,
                    **item.model_dump(), difference=outcome["difference"], status=status,
                    chat_id=session_id.split(":")[1] if session_id.startswith("tg:") else None,
                    response=render_reply({"outcomes": [outcome]}))
                journal.rows.append(row)
                inserted += 1
            outcome["record_id"] = row.record_id
            changed_rows.append(row)
            by_key[key] = row
            by_id[row.record_id] = row
            outcomes.append(outcome)
        if changed_rows:
            await mcp.call("write_query", {"query": save_query(changed_rows)}, trace)
            verified = await mcp.call("read_query", {"query": READ_JOURNAL}, trace)
            if journal_from_result(verified) != journal:
                raise RuntimeError("Не удалось подтвердить сохранение журнала")
        receipt.update(ready=True, outcomes=outcomes, inserted=inserted, updated=updated,
                       total_rows=len(journal.rows), pending=pending_items(journal, session_id, settings.warehouse_id))
        trace.emit("registration_receipt", **receipt)
        return receipt

    return [calculate_difference, read_warehouse_journal, register_discrepancies], receipt


def render_reply(receipt: dict, question: str | None = None) -> str:
    parts = []
    for outcome in receipt["outcomes"]:
        sku = outcome["sku"] or "Товар без артикула"
        status = outcome["status"]
        if status == "missing_data":
            missing = [label for name, label in [("sku", "артикул"), ("expected_qty", "количество по документам"),
                       ("actual_qty", "фактическое количество")] if outcome[name] is None]
            text = f"{sku}: уточни {', '.join(missing)}."
            if outcome["registration"] == "saved":
                text = "Неполная заявка сохранена. " + text
            elif outcome["registration"] == "updated":
                text = "Неполная заявка дополнена. " + text
            elif outcome["registration"] == "duplicate":
                text = "Эта неполная заявка уже сохранена. " + text
            parts.append(text)
            continue
        diff = outcome["difference"]
        result = "Недостача" if status == "shortage" else "Излишек" if status == "surplus" else "Количество совпадает"
        text = f"{sku}: {result}. По документам {outcome['expected_qty']}, фактически {outcome['actual_qty']}, разница {diff:+d}."
        if outcome["registration"] == "saved":
            text += " Результат проверки сохранён." if status == "match" else " Заявка зарегистрирована."
        elif outcome["registration"] == "updated":
            text += " Заявка дополнена; обновлена существующая запись."
        elif outcome["registration"] == "duplicate":
            text += " Повторная заявка: новая запись не создана."
        if status == "shortage":
            text += " Повтори пересчёт и сверь документы."
        elif status == "surplus":
            text += " Проверь учёт и поступления."
        parts.append(text)
    if question:
        parts.append(question)
    return "\n\n".join(parts) or "Пришли артикул, количество по документам и фактическое количество."

