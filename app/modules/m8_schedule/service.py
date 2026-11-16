"""M8 编排：解析一批实验 → 候选评分 → 约束匹配 → 方案落库 → 原子确认。

两条不能破的规矩：
1. 方案确认是「全有或全无」：一场实验占不上就整批回滚，已确认的上一版原样保留；
2. 确认失败后的重试只写新的 draft，不产生任何占用行；只有 confirm 成功才会
   顶替旧版，旧占用随旧方案一起失效，不会重复占位。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import sqlite3
import uuid

from ... import config, reasons
from ...errors import Conflict, Unprocessable
from ..m2_price import coerce
from ..m7_design import control, power
from ..m7_design.service import _trend_window
from . import matcher

STAGE = "schedule"
ROW_FIELDS = ("experiment_id", "sku", "channel", "start_date", "end_date")
# 页面上「填入下月示例」用的固定样例（2026-12，历史数据 2026-09~10 之后的干净窗）
SAMPLE_EXPERIMENTS = [
    ("EXP-1201", "MY-0012", "电商", "2026-12-01", "2026-12-14"),
    ("EXP-1202", "MY-0021", "电商", "2026-12-08", "2026-12-21"),
    ("EXP-1203", "MY-0034", "电商", "2026-12-15", "2026-12-28"),
    ("EXP-1204", "MY-0012", "商超", "2026-12-03", "2026-12-16"),
]
# 刻意撞车的压力样例：同一目标商品 5 场重叠实验，而商超渠道只有 4 个对照，
# 第 5 场必然拿不到独立对照，用来演示阻断与「禁止复用对照凑数」
STRESS_EXPERIMENTS = [
    ("EXP-2101", "MY-0012", "商超", "2026-12-01", "2026-12-10"),
    ("EXP-2102", "MY-0012", "商超", "2026-12-02", "2026-12-11"),
    ("EXP-2103", "MY-0012", "商超", "2026-12-03", "2026-12-12"),
    ("EXP-2104", "MY-0012", "商超", "2026-12-04", "2026-12-13"),
    ("EXP-2105", "MY-0012", "商超", "2026-12-05", "2026-12-14"),
]


def now_iso() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def _bad(code: str, message: str, detail: dict | None = None):
    return Unprocessable(message, code=code, detail=detail or {})


def sample_text(stress: bool = False) -> str:
    rows = STRESS_EXPERIMENTS if stress else SAMPLE_EXPERIMENTS
    return "\n".join(",".join(row) for row in rows)


def parse_experiments(raw_text: str) -> list[dict]:
    """解析粘贴的实验清单：每行 `实验编号,商品,渠道,开始日,结束日`。

    空白行与 # 注释行跳过；结构错误带行号抛 422，不悄悄丢行。
    """
    if not raw_text or not raw_text.strip():
        raise _bad("S_BAD_ROW", "实验清单为空", {"field": "experiments"})
    reader = csv.reader(io.StringIO(raw_text))
    out: list[dict] = []
    seen: set[str] = set()
    for line_no, parts in enumerate(reader, start=1):
        cells = [cell.strip() for cell in parts]
        if not cells or all(not cell for cell in cells):
            continue
        if cells[0].startswith("#"):
            continue
        if len(cells) != len(ROW_FIELDS):
            raise _bad("S_BAD_ROW",
                       f"第 {line_no} 行有 {len(cells)} 列，应为 {len(ROW_FIELDS)} 列",
                       {"line_no": line_no, "expects": list(ROW_FIELDS)})
        row = dict(zip(ROW_FIELDS, cells))
        if not all(row.values()):
            raise _bad("S_BAD_ROW", f"第 {line_no} 行存在空字段", {"line_no": line_no})
        start = coerce.to_date(row["start_date"])
        end = coerce.to_date(row["end_date"])
        if start is None or end is None:
            raise _bad("S_BAD_ROW", f"第 {line_no} 行日期无法解析",
                       {"line_no": line_no, "raw": cells})
        if end <= start:
            raise _bad("S_BAD_ROW",
                       f"第 {line_no} 行结束日必须晚于开始日（左闭右开）",
                       {"line_no": line_no})
        if row["experiment_id"] in seen:
            raise _bad("S_DUP_EXPERIMENT",
                       f"实验编号 {row['experiment_id']} 重复",
                       {"experiment_id": row["experiment_id"]})
        seen.add(row["experiment_id"])
        row["start"] = start
        row["end"] = end
        out.append(row)
    if not out:
        raise _bad("S_BAD_ROW", "实验清单没有有效数据行", {"field": "experiments"})
    return out


def _validate_series(store, batch_id: str, parsed: list[dict]) -> None:
    available: set[tuple[str, str]] = set()
    for row in store.sales(batch_id):
        available.add((row["sku"], row["channel"]))
    for row in parsed:
        if (row["sku"], row["channel"]) not in available:
            raise _bad("S_UNKNOWN_SERIES",
                       f"本批次没有 {row['sku']}/{row['channel']} 的日销记录",
                       {"experiment_id": row["experiment_id"],
                        "sku": row["sku"], "channel": row["channel"]})


def plan(store, batch_id: str, raw_text: str) -> dict:
    """生成一版排程方案（只落 draft，不占任何对照）。"""
    parsed = parse_experiments(raw_text)
    _validate_series(store, batch_id, parsed)
    rows = store.sales(batch_id)
    windows = store.promo_windows(batch_id)
    occupancy = store.occupancy(batch_id)

    experiments = [matcher.Experiment(
        experiment_id=row["experiment_id"], sku=row["sku"], channel=row["channel"],
        start=row["start"], end=row["end"]) for row in parsed]

    # 前置趋势窗按「商品×渠道」缓存一次，和 M7 同一套干净窗口规则
    trend_window_by_key: dict = {}
    for exp in experiments:
        sku, channel = exp.key()
        if (sku, channel) in trend_window_by_key:
            continue
        series = power.build_series(rows, sku, channel)
        dates, _note = _trend_window(series, windows, sku, channel)
        trend_window_by_key[(sku, channel)] = dates

    edges_by_id: dict[str, list[matcher.Edge]] = {}
    for exp in experiments:
        edges_by_id[exp.experiment_id] = matcher.build_edges(
            rows, windows, occupancy, exp, trend_window_by_key)

    solution = matcher.solve(experiments, edges_by_id)

    assignments: list[dict] = []
    diagnostic = solution.get("diagnostic", {})
    for exp in experiments:
        picked_sku = solution["assignment"].get(exp.experiment_id)
        picked = next((e for e in edges_by_id[exp.experiment_id]
                       if e.sku == picked_sku), None)
        block = next((b for b in solution["blocked"]
                      if b["experiment_id"] == exp.experiment_id), None)
        if picked is None and not solution["feasible"] and not block:
            # 整批不可行但本实验在贪心诊断里能配上：只作诊断，不可确认
            diag_sku = diagnostic.get(exp.experiment_id)
            diag_edge = next((e for e in edges_by_id[exp.experiment_id]
                              if e.sku == diag_sku), None)
            assignments.append({
                "experiment": exp.as_dict(),
                "control_sku": diag_edge.sku if diag_edge else None,
                "trend_gap": power_dec(diag_edge.trend_gap) if diag_edge else None,
                "registered": 1 if (diag_edge and diag_edge.registered) else 0,
                "status": "诊断配对",
                "block_reason": "",
                "block_detail": "整批存在阻断实验，该配对仅供参考，不会被确认占用",
            })
            continue
        assignments.append({
            "experiment": exp.as_dict(),
            "control_sku": picked.sku if picked else None,
            "trend_gap": power_dec(picked.trend_gap) if picked else None,
            "registered": 1 if (picked and picked.registered) else 0,
            "status": "已配对" if picked else "无法满足",
            "block_reason": block["reason"] if block else "",
            "block_detail": block["detail"] if block else "",
            "contention": block.get("contention", []) if block else [],
        })

    latest_confirmed = store.latest_confirmed_plan(batch_id)
    result = {
        "batch_id": batch_id,
        "feasible": solution["feasible"],
        "total_gap": power_dec(solution["total_gap"]) if solution["total_gap"] is not None else None,
        "assignments": assignments,
        "candidates": {exp_id: [e.as_dict() for e in edges]
                       for exp_id, edges in edges_by_id.items()},
        "blocked": solution["blocked"],
        "existing_occupancy": occupancy,
        "quarantine": _quarantine(experiments, edges_by_id, solution, occupancy),
        "generated_at": now_iso(),
        # 乐观并发基线：draft 生成时最新已确认方案；确认时基线变了就拒绝
        "base_confirmed_plan": latest_confirmed["plan_id"] if latest_confirmed else "",
    }
    plan_id = f"PLAN-{uuid.uuid4().hex[:12]}"
    store.insert_plan({
        "plan_id": plan_id,
        "batch_id": batch_id,
        "status": "draft",
        "created_at": result["generated_at"],
        "payload": {"experiments": [e.as_dict() for e in experiments]},
        "result": result,
    })
    result["plan_id"] = plan_id
    return result


def power_dec(value) -> str | None:
    return None if value is None else power.dec(value, "0.0001")


def _quarantine(experiments, edges_by_id, solution, occupancy) -> list[dict]:
    out: list[dict] = []

    def row(code: str, detail: str, value: str = "") -> dict:
        reason = reasons.get(code)
        return {"code": reason.code, "level": reason.level, "stage": STAGE,
                "title": reason.title, "hint": reason.hint,
                "detail": detail, "raw_value": value}

    if occupancy:
        out.append(row("S_CONTROL_REUSE",
                       f"已有 {len({(o['control_sku'], o['channel']) for o in occupancy})} "
                       f"个对照处于占用中，新方案的窗口已与之逐日比对"))
    for exp in experiments:
        edges = edges_by_id[exp.experiment_id]
        if not edges:
            out.append(row("S_NO_CANDIDATE",
                           f"{exp.experiment_id}（{exp.sku}/{exp.channel}）"
                           f"同渠道没有其它商品可作对照"))
            continue
        for edge in edges:
            if edge.feasible:
                continue
            if edge.code == "S_CONTROL_BUSY":
                out.append(row("S_CONTROL_BUSY",
                               f"{exp.experiment_id} → {edge.sku}：与已占用实验 "
                               f"{', '.join(edge.busy_with)} 窗口重叠",
                               edge.sku))
            elif edge.code == "S_CONTROL_OVERLAP":
                out.append(row("S_CONTROL_OVERLAP",
                               f"{exp.experiment_id} → {edge.sku}：实验窗内 "
                               f"{len(edge.overlap_dates)} 天自有促销",
                               edge.sku))
            elif edge.code == "D_CONTROL_SHORT":
                out.append(row("D_CONTROL_SHORT",
                               f"{exp.experiment_id} → {edge.sku}：前置天数不足，趋势不可比",
                               edge.sku))
            else:
                out.append(row(edge.code,
                               f"{exp.experiment_id} → {edge.sku}：{edge.note}",
                               edge.sku))
    if solution["feasible"]:
        for exp_id, sku in sorted(solution["assignment"].items()):
            gap = next(e.trend_gap for e in edges_by_id[exp_id] if e.sku == sku)
            out.append(row("D_CONTROL_PICKED",
                           f"{exp_id} 建议对照 {sku}，前置趋势差 {gap:.4f}", sku))
        out.append(row("S_PLAN_CONFIRMED",
                       f"整批 {len(solution['assignment'])} 场实验均可独立占用，"
                       f"趋势差合计 {solution['total_gap']:.4f}"))
    else:
        for item in solution["blocked"]:
            contention = "；".join(f"{c['control_sku']} 被 {c['held_by']} 占用"
                                   for c in item.get("contention", []))
            detail = item["detail"] + (f"（{contention}）" if contention else "")
            out.append(row(item["reason"],
                           f"{item['experiment_id']}：{detail}",
                           item["experiment_id"]))
    return out


def confirm(store, plan_id: str) -> dict:
    """确认一版 draft：重新校验快照里的占用冲突，全过才顶替旧版并写占用。"""
    plan_row = store.get_plan(plan_id)
    if plan_row is None:
        raise _bad("S_PLAN_STALE", f"方案 {plan_id} 不存在", {"plan_id": plan_id})
    if plan_row["status"] != "draft":
        raise Conflict(f"方案 {plan_id} 状态为 {plan_row['status']}，不能确认",
                       code="S_PLAN_STALE",
                       detail={"plan_id": plan_id, "status": plan_row["status"]})
    result = plan_row["result"]
    if not result.get("feasible"):
        raise _bad("S_NO_FEASIBLE",
                   "该方案存在无法满足的实验，禁止确认；请改期或补对照后重新排程",
                   {"plan_id": plan_id, "blocked": result.get("blocked", [])})

    batch_id = plan_row["batch_id"]
    # 乐观并发：draft 生成后若已有别的方案确认，本方案基线过期，拒绝确认
    latest = store.latest_confirmed_plan(batch_id)
    base = result.get("base_confirmed_plan", "")
    current = latest["plan_id"] if latest else ""
    if base != current:
        raise Conflict(
            f"方案 {plan_id} 生成后占用已被 {current or '其它方案'} 更新，原分配保持不变",
            code="S_PLAN_SUPERSEDED",
            detail={"plan_id": plan_id, "base_confirmed_plan": base,
                    "current_confirmed_plan": current})
    # 落库前再比一次已确认占用：draft 生成后可能有别的批次动作改了占用表
    occupancy = store.occupancy(batch_id)
    for item in result["assignments"]:
        exp = item["experiment"]
        for occ in occupancy:
            if occ["experiment_id"] == exp["experiment_id"]:
                continue
            if occ["control_sku"] != item["control_sku"] or occ["channel"] != exp["channel"]:
                continue
            o_start = dt.date.fromisoformat(occ["start_date"])
            o_end = dt.date.fromisoformat(occ["end_date"])
            start = dt.date.fromisoformat(exp["start_date"])
            end = dt.date.fromisoformat(exp["end_date"])
            if matcher.windows_overlap(start, end, o_start, o_end):
                raise Conflict(
                    f"确认时发现 {item['control_sku']} 已被 {occ['experiment_id']} 占用",
                    code="S_PLAN_SUPERSEDED",
                    detail={"plan_id": plan_id, "control_sku": item["control_sku"],
                            "busy_with": occ["experiment_id"]})

    occupancy_rows = [{
        "experiment_id": item["experiment"]["experiment_id"],
        "target_sku": item["experiment"]["sku"],
        "control_sku": item["control_sku"],
        "channel": item["experiment"]["channel"],
        "start_date": item["experiment"]["start_date"],
        "end_date": item["experiment"]["end_date"],
    } for item in result["assignments"]]
    try:
        store.confirm_plan(plan_id, occupancy_rows, now_iso(),
                           base_confirmed_plan=base)
    except sqlite3.OperationalError as exc:  # database is locked
        raise Conflict(f"占用表写入冲突：{exc}，本次未占位，请重试",
                       code="S_OCCUPANCY_BUSY") from exc
    except RuntimeError as exc:  # 事务内基线复查失败
        raise Conflict(f"确认期间占用已被其它方案抢先：{exc}，原分配未改动",
                       code="S_PLAN_SUPERSEDED",
                       detail={"plan_id": plan_id}) from exc
    confirmed = store.get_plan(plan_id)
    return {"plan_id": plan_id, "batch_id": batch_id, "status": "confirmed",
            "confirmed_at": confirmed["confirmed_at"],
            "occupancy": store.occupancy(batch_id),
            "superseded": store.list_plans(batch_id)}


def retry_plan(store, plan_id: str) -> dict:
    """确认失败后的重试：用原方案快照重新排一版 draft，不触碰任何占用。

    重新走一遍 plan()，占用表以当前已确认状态为准；原分配在新方案确认前
    保持不变，新 draft 不写 control_occupancy，因此不可能重复占位。
    """
    plan_row = store.get_plan(plan_id)
    if plan_row is None:
        raise _bad("S_PLAN_STALE", f"方案 {plan_id} 不存在，无法重试",
                   {"plan_id": plan_id})
    lines = []
    for exp in plan_row["payload"]["experiments"]:
        lines.append(",".join((exp["experiment_id"], exp["sku"], exp["channel"],
                               exp["start_date"], exp["end_date"])))
    return plan(store, plan_row["batch_id"], "\n".join(lines))


def state(store, batch_id: str) -> dict:
    """页面只读状态：当前占用、方案历史与最近一版结果。"""
    plans = store.list_plans(batch_id)
    latest = store.latest_confirmed_plan(batch_id)
    latest_draft = next((p for p in plans if p["status"] == "draft"), None)
    recent_result = None
    if latest_draft:
        recent_result = store.get_plan(latest_draft["plan_id"])["result"]
    elif latest:
        recent_result = latest["result"]
    return {
        "batch_id": batch_id,
        "occupancy": store.occupancy(batch_id),
        "plans": plans,
        "latest_plan": recent_result,
        "latest_confirmed_at": latest["confirmed_at"] if latest else None,
    }
