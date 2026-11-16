"""SQLite 存储：批次、输入摘要、日销行、促销版本、隔离行、弹性、增量、分摊、阶段、合计、清单。

约定：
- 金额列一律 TEXT（Decimal 字符串），不落 float，避免 2^53 与二进制误差；
- 一次运行的全部行在同一个事务里「先删后插」，失败整体回滚，不留半批数据；
- 读数按 batch_id 过滤，页面永远只展示一个批次。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(batch_key, status);

CREATE TABLE IF NOT EXISTS sources (
    batch_id TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
    encoding TEXT, declared TEXT, bom_stripped INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL,
    records INTEGER NOT NULL DEFAULT 0, rows INTEGER NOT NULL DEFAULT 0,
    code TEXT, note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sales (
    batch_id TEXT NOT NULL, line_no INTEGER NOT NULL, sale_date TEXT NOT NULL,
    channel TEXT NOT NULL, sku TEXT NOT NULL, sku_name TEXT NOT NULL,
    price_incl TEXT, price_unit TEXT, tax_rate TEXT, price_excl TEXT,
    qty INTEGER NOT NULL, promo_id TEXT NOT NULL DEFAULT '',
    amount_excl TEXT NOT NULL DEFAULT '0',
    raw_json TEXT NOT NULL DEFAULT '{}', flags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_sales_batch ON sales(batch_id, sku, channel, sale_date);

CREATE TABLE IF NOT EXISTS promos (
    batch_id TEXT NOT NULL, promo_id TEXT NOT NULL, sku TEXT NOT NULL, channel TEXT NOT NULL,
    version INTEGER NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    activity_price TEXT, status TEXT NOT NULL, effective INTEGER NOT NULL DEFAULT 0,
    overlap INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', row_no INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS promo_windows (
    batch_id TEXT NOT NULL, promo_id TEXT NOT NULL, sku TEXT NOT NULL, channel TEXT NOT NULL,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, version INTEGER NOT NULL,
    price_incl TEXT NOT NULL, price_excl TEXT NOT NULL, days INTEGER NOT NULL,
    base_price_excl TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
    batch_id TEXT NOT NULL, code TEXT NOT NULL, stage TEXT NOT NULL, level TEXT NOT NULL,
    source TEXT, line_no INTEGER, field TEXT, raw_value TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS elasticity (
    batch_id TEXT NOT NULL, scope TEXT NOT NULL, group_key TEXT NOT NULL,
    n INTEGER NOT NULL, zero_count INTEGER NOT NULL, intercept TEXT NOT NULL,
    slope TEXT NOT NULL, se TEXT NOT NULL, tvalue TEXT NOT NULL, r2 TEXT NOT NULL,
    zero_mode TEXT NOT NULL, significant INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lift (
    batch_id TEXT NOT NULL, promo_id TEXT NOT NULL, sku TEXT NOT NULL, channel TEXT NOT NULL,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, days INTEGER NOT NULL,
    days_valid INTEGER NOT NULL, days_missing INTEGER NOT NULL, days_dropped INTEGER NOT NULL,
    actual_qty TEXT NOT NULL, base_qty TEXT NOT NULL, actual_gmv TEXT NOT NULL,
    base_gmv TEXT NOT NULL, lift_qty TEXT NOT NULL, lift_gmv TEXT NOT NULL,
    se_gmv TEXT NOT NULL, ci_low TEXT NOT NULL, ci_high TEXT NOT NULL,
    price_excl TEXT NOT NULL, base_price_excl TEXT NOT NULL,
    control_sku TEXT NOT NULL, control_factor TEXT NOT NULL, dlnp TEXT NOT NULL,
    slope TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS spread (
    batch_id TEXT NOT NULL, promo_id TEXT NOT NULL, channel TEXT NOT NULL,
    ratio TEXT NOT NULL, lift_gmv TEXT NOT NULL, actual_gmv TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stages (
    batch_id TEXT NOT NULL, stage TEXT NOT NULL, in_count INTEGER NOT NULL,
    out_count INTEGER NOT NULL, quarantine_count INTEGER NOT NULL,
    status TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS totals (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifests (
    batch_id TEXT PRIMARY KEY, json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocation_plans (
    plan_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    superseded_by TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_plans_batch ON allocation_plans(batch_id, created_at);

CREATE TABLE IF NOT EXISTS control_occupancy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    target_sku TEXT NOT NULL,
    control_sku TEXT NOT NULL,
    channel TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_occupancy_resource
    ON control_occupancy(control_sku, channel, start_date, end_date);
"""

BATCH_TABLES = (
    "sources", "sales", "promos", "promo_windows", "quarantine",
    "elasticity", "lift", "spread", "stages", "totals", "manifests",
    "allocation_plans", "control_occupancy",
)


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def upsert_batch(self, batch: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id, batch_key, run_id, dataset, status,
                        created_at, finished_at, input_digest, params_json, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     input_digest=excluded.input_digest, note=excluded.note,
                     params_json=excluded.params_json""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["dataset"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"],
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", "")),
            )

    def find_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (batch_key,)).fetchone()
        return dict(row) if row else None

    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return dict(row) if row else None

    def latest_batch(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def clear_batch_rows(self, batch_id: str, conn) -> None:
        for table in BATCH_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (batch_id,))

    # ---------- 写入（一次事务） ----------
    def write_batch(self, batch_id: str, payload: dict) -> None:
        """payload 的每个键对应一张表；整批一次事务，失败回滚不留半批。"""
        with self.connect() as conn:
            self.clear_batch_rows(batch_id, conn)
            for row in payload.get("sources", []):
                conn.execute(
                    """INSERT INTO sources (batch_id, name, role, encoding, declared,
                            bom_stripped, bytes, sha256, records, rows, code, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["name"], row["role"], row.get("encoding"),
                     row.get("declared"), int(row.get("bom_stripped", 0)),
                     row.get("bytes", 0), row["sha256"], row.get("records", 0),
                     row.get("rows", 0), row.get("code"), row.get("note", "")))
            for row in payload.get("sales", []):
                conn.execute(
                    """INSERT INTO sales (batch_id, line_no, sale_date, channel, sku,
                            sku_name, price_incl, price_unit, tax_rate, price_excl, qty,
                            promo_id, amount_excl, raw_json, flags_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["line_no"], row["sale_date"], row["channel"], row["sku"],
                     row["sku_name"], row.get("price_incl"), row.get("price_unit"),
                     row.get("tax_rate"), row.get("price_excl"), row["qty"],
                     row.get("promo_id", ""), row.get("amount_excl", "0"),
                     row.get("raw_json", "{}"),
                     json.dumps(row.get("flags", []), ensure_ascii=False)))
            for row in payload.get("promos", []):
                conn.execute(
                    """INSERT INTO promos (batch_id, promo_id, sku, channel, version,
                            start_date, end_date, activity_price, status, effective,
                            overlap, note, row_no)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["promo_id"], row["sku"], row["channel"], row["version"],
                     row["start_date"], row["end_date"], row.get("activity_price"),
                     row["status"], int(row.get("effective", 0)),
                     int(row.get("overlap", 0)), row.get("note", ""), row.get("row_no", 0)))
            for row in payload.get("promo_windows", []):
                conn.execute(
                    """INSERT INTO promo_windows (batch_id, promo_id, sku, channel,
                            start_date, end_date, version, price_incl, price_excl,
                            days, base_price_excl)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["promo_id"], row["sku"], row["channel"],
                     row["start_date"], row["end_date"], row["version"],
                     row["price_incl"], row["price_excl"], row["days"],
                     row["base_price_excl"]))
            for row in payload.get("quarantine", []):
                conn.execute(
                    """INSERT INTO quarantine (batch_id, code, stage, level, source,
                            line_no, field, raw_value, detail)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["code"], row["stage"], row["level"],
                     row.get("source"), row.get("line_no"), row.get("field"),
                     row.get("raw_value"), row.get("detail")))
            for row in payload.get("elasticity", []):
                conn.execute(
                    """INSERT INTO elasticity (batch_id, scope, group_key, n, zero_count,
                            intercept, slope, se, tvalue, r2, zero_mode, significant, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["scope"], row["group_key"], row["n"],
                     row["zero_count"], row["intercept"], row["slope"], row["se"],
                     row["tvalue"], row["r2"], row["zero_mode"],
                     int(row.get("significant", 0)), row.get("note", "")))
            for row in payload.get("lift", []):
                conn.execute(
                    """INSERT INTO lift (batch_id, promo_id, sku, channel, start_date,
                            end_date, days, days_valid, days_missing, days_dropped,
                            actual_qty, base_qty, actual_gmv, base_gmv, lift_qty,
                            lift_gmv, se_gmv, ci_low, ci_high, price_excl,
                            base_price_excl, control_sku, control_factor, dlnp, slope, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["promo_id"], row["sku"], row["channel"],
                     row["start_date"], row["end_date"], row["days"],
                     row["days_valid"], row["days_missing"], row["days_dropped"],
                     row["actual_qty"], row["base_qty"], row["actual_gmv"],
                     row["base_gmv"], row["lift_qty"], row["lift_gmv"],
                     row["se_gmv"], row["ci_low"], row["ci_high"],
                     row["price_excl"], row["base_price_excl"], row["control_sku"],
                     row["control_factor"], row["dlnp"], row["slope"],
                     row.get("note", "")))
            for row in payload.get("spread", []):
                conn.execute(
                    """INSERT INTO spread (batch_id, promo_id, channel, ratio,
                            lift_gmv, actual_gmv)
                       VALUES (?,?,?,?,?,?)""",
                    (batch_id, row["promo_id"], row["channel"], row["ratio"],
                     row["lift_gmv"], row["actual_gmv"]))
            for row in payload.get("stages", []):
                conn.execute(
                    """INSERT INTO stages (batch_id, stage, in_count, out_count,
                            quarantine_count, status, elapsed_ms, note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (batch_id, row["stage"], row["in_count"], row["out_count"],
                     row["quarantine_count"], row["status"], row["elapsed_ms"],
                     row.get("note", "")))
            for key, value in payload.get("totals", {}).items():
                conn.execute("INSERT INTO totals (batch_id, key, value) VALUES (?,?,?)",
                             (batch_id, key, str(value)))
            if payload.get("manifest") is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO manifests (batch_id, json) VALUES (?,?)",
                    (batch_id, json.dumps(payload["manifest"], ensure_ascii=False)))

    # ---------- 读取 ----------
    def _rows(self, sql: str, args=()) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def sources(self, batch_id: str) -> list[dict]:
        return self._rows("SELECT * FROM sources WHERE batch_id=? ORDER BY role, name",
                          (batch_id,))

    def sales(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM sales WHERE batch_id=? ORDER BY sale_date, channel, sku, line_no",
            (batch_id,))

    def sales_count(self, batch_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM sales WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return int(row["c"]) if row else 0

    def promos(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM promos WHERE batch_id=? ORDER BY promo_id, version", (batch_id,))

    def promo_windows(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM promo_windows WHERE batch_id=? ORDER BY start_date, promo_id",
            (batch_id,))

    def quarantine(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM quarantine WHERE batch_id=? "
            "ORDER BY CASE level WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
            "code, line_no", (batch_id,))

    def quarantine_by_reason(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT code, level, stage, COUNT(*) AS cnt FROM quarantine WHERE batch_id=? "
            "GROUP BY code, level, stage ORDER BY cnt DESC, code", (batch_id,))

    def quarantine_count(self, batch_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM quarantine WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return int(row["c"]) if row else 0

    def elasticity(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM elasticity WHERE batch_id=? ORDER BY scope, group_key",
            (batch_id,))

    def lift(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM lift WHERE batch_id=? ORDER BY promo_id, channel", (batch_id,))

    def spread(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM spread WHERE batch_id=? ORDER BY promo_id, channel", (batch_id,))

    def stages(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM stages WHERE batch_id=? ORDER BY rowid", (batch_id,))

    def totals(self, batch_id: str) -> dict:
        rows = self._rows("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def manifest(self, batch_id: str) -> dict | None:
        rows = self._rows("SELECT json FROM manifests WHERE batch_id=?", (batch_id,))
        return json.loads(rows[0]["json"]) if rows else None

    # ---------- M8 批次对照排程 ----------
    def insert_plan(self, plan: dict) -> None:
        """登记一份排程方案（draft / confirmed / superseded）。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO allocation_plans (plan_id, batch_id, status, created_at,
                        confirmed_at, superseded_by, payload_json, result_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (plan["plan_id"], plan["batch_id"], plan["status"], plan["created_at"],
                 plan.get("confirmed_at"), plan.get("superseded_by", ""),
                 json.dumps(plan.get("payload", {}), ensure_ascii=False),
                 json.dumps(plan.get("result", {}), ensure_ascii=False)),
            )

    def get_plan(self, plan_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM allocation_plans WHERE plan_id=?",
                               (plan_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = json.loads(out.pop("payload_json"))
        out["result"] = json.loads(out.pop("result_json"))
        return out

    def list_plans(self, batch_id: str, limit: int = 20) -> list[dict]:
        rows = self._rows(
            "SELECT plan_id, batch_id, status, created_at, confirmed_at, superseded_by "
            "FROM allocation_plans WHERE batch_id=? ORDER BY created_at DESC, rowid DESC "
            "LIMIT ?", (batch_id, limit))
        return rows

    def latest_confirmed_plan(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM allocation_plans WHERE batch_id=? AND status='confirmed' "
                "ORDER BY confirmed_at DESC, rowid DESC LIMIT 1", (batch_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = json.loads(out.pop("payload_json"))
        out["result"] = json.loads(out.pop("result_json"))
        return out

    def mark_plan_superseded(self, conn, batch_id: str, by_plan_id: str) -> None:
        conn.execute(
            "UPDATE allocation_plans SET status='superseded', superseded_by=? "
            "WHERE batch_id=? AND status='confirmed'", (by_plan_id, batch_id))

    def occupancy(self, batch_id: str) -> list[dict]:
        return self._rows(
            "SELECT o.* FROM control_occupancy o "
            "JOIN allocation_plans p ON p.plan_id=o.plan_id "
            "WHERE o.batch_id=? AND p.status='confirmed' "
            "ORDER BY o.start_date, o.experiment_id", (batch_id,))

    def confirm_plan(self, plan_id: str, occupancy_rows: list[dict],
                     confirmed_at: str, base_confirmed_plan: str = "") -> None:
        """整批确认的唯一落库点：一个事务里完成「校验 → 顶替旧版 → 写占用」。

        任一步失败整体回滚，不留半批占用；并发确认在 BEGIN IMMEDIATE 上排队，
        抢不到写锁直接抛 OperationalError，由编排层翻译成 S_OCCUPANCY_BUSY。
        基线在事务内复查：排队期间若已有别的方案确认，本事务回滚。
        """
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = conn.execute(
                "SELECT * FROM allocation_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise LookupError(plan_id)
            if plan["status"] != "draft":
                raise RuntimeError(f"plan {plan_id} status={plan['status']}")
            current = conn.execute(
                "SELECT plan_id FROM allocation_plans WHERE batch_id=? "
                "AND status='confirmed' ORDER BY confirmed_at DESC, rowid DESC LIMIT 1",
                (plan["batch_id"],)).fetchone()
            current_id = current["plan_id"] if current else ""
            if current_id != base_confirmed_plan:
                raise RuntimeError(f"stale base: {base_confirmed_plan!r} != {current_id!r}")
            batch_id = plan["batch_id"]
            self.mark_plan_superseded(conn, batch_id, plan_id)
            conn.execute(
                "UPDATE allocation_plans SET status='confirmed', confirmed_at=? "
                "WHERE plan_id=?", (confirmed_at, plan_id))
            for row in occupancy_rows:
                conn.execute(
                    """INSERT INTO control_occupancy (plan_id, batch_id, experiment_id,
                            target_sku, control_sku, channel, start_date, end_date, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (plan_id, batch_id, row["experiment_id"], row["target_sku"],
                     row["control_sku"], row["channel"], row["start_date"],
                     row["end_date"], confirmed_at))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
