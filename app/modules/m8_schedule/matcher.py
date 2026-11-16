"""M8 排程匹配引擎：在硬约束下给一批实验各配一个对照，再按走势接近度择优。

硬约束（任一不满足就不可用，绝不复用对照凑数）：
1. 对照必须是同渠道、且不是实验商品自己的其它商品；
2. 对照在实验窗 [start, end) 内没有自己生效的促销（沿用 M7 的 D_CONTROL_OVERLAP 判定）；
3. 同一对照商品在同一时段不能服务两场实验——已确认占用与本批内部分配都算，
   窗口有一天重叠即冲突，左闭右开，首尾相接（A.end == B.start）不算重叠；
4. 走势无法比较（前置半段天数不足）的候选不进可行集，避免「不知道差多少」也硬配。

目标：在全部可行的整批方案里，取各实验前置趋势差之和最小的一个；
不存在覆盖全部实验的方案时，不产出半成品分配，只把卡死的实验连同每个候选的
淘汰原因一起返回。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..m7_design import control, power

# 回溯搜索的节点上限：批次规模通常十几场，MRV 排序下远到不了；
# 到顶就退化为贪心，保证页面永远给得出结果（贪心不可行同样如实标阻断）。
SEARCH_NODE_LIMIT = 200_000


@dataclass
class Experiment:
    experiment_id: str
    sku: str
    channel: str
    start: dt.date
    end: dt.date

    def key(self) -> tuple[str, str]:
        return self.sku, self.channel

    def as_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "sku": self.sku,
            "channel": self.channel,
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "days": (self.end - self.start).days,
        }


@dataclass
class Edge:
    """一条「实验 → 候选对照」的评分边。"""
    experiment_id: str
    sku: str
    channel: str
    registered: bool
    trend_gap: float
    level_ratio: float
    target_trend: float | None
    control_trend: float | None
    feasible: bool
    code: str
    note: str
    busy_with: list[str] = field(default_factory=list)
    overlap_dates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "sku": self.sku,
            "channel": self.channel,
            "registered": 1 if self.registered else 0,
            "trend_gap": power.dec(self.trend_gap, "0.0001"),
            "level_ratio": power.dec(self.level_ratio, "0.0001"),
            "target_trend": (power.dec(self.target_trend, "0.0001")
                             if self.target_trend is not None else "—"),
            "control_trend": (power.dec(self.control_trend, "0.0001")
                              if self.control_trend is not None else "—"),
            "feasible": 1 if self.feasible else 0,
            "status": "可行" if self.feasible else "排除",
            "code": self.code,
            "note": self.note,
            "busy_with": self.busy_with,
            "overlap_dates": self.overlap_dates,
        }


def windows_overlap(a_start: dt.date, a_end: dt.date,
                    b_start: dt.date, b_end: dt.date) -> bool:
    """两个左闭右开窗口是否共享至少一天；首尾相接不算重叠。"""
    return a_start < b_end and b_start < a_end


def _busy_experiments(occupancy, sku: str, channel: str,
                      start: dt.date, end: dt.date) -> list[str]:
    """已确认占用里，与该候选窗口重叠的实验编号（按编号排序）。"""
    hits: set[str] = set()
    for occ in occupancy:
        if occ["control_sku"] != sku or occ["channel"] != channel:
            continue
        try:
            o_start = dt.date.fromisoformat(str(occ["start_date"]))
            o_end = dt.date.fromisoformat(str(occ["end_date"]))
        except (TypeError, ValueError):
            continue
        if windows_overlap(start, end, o_start, o_end):
            hits.add(str(occ["experiment_id"]))
    return sorted(hits)


def build_edges(rows, windows, occupancy, experiment: Experiment,
                trend_window_by_key: dict) -> list[Edge]:
    """给一场实验列出同渠道全部候选，逐个过硬约束并打分。"""
    target_sku, channel = experiment.key()
    target_series = power.build_series(rows, target_sku, channel)
    trend_dates = trend_window_by_key.get((target_sku, channel))
    candidates = control.build(
        rows, windows, target_series, (target_sku, channel),
        experiment.start, experiment.end, trend_dates)
    edges: list[Edge] = []
    for cand in candidates:
        # 防御性兜底：control.build 已排除同 sku，万一上层规则变化也不许自配
        if cand.sku == target_sku:
            edges.append(Edge(
                experiment_id=experiment.experiment_id, sku=cand.sku, channel=channel,
                registered=False, trend_gap=cand.trend_gap,
                level_ratio=cand.level_ratio, target_trend=cand.target_trend,
                control_trend=cand.control_trend, feasible=False,
                code="S_CONTROL_TARGET", note="对照不能是实验商品自己"))
            continue
        busy = _busy_experiments(occupancy, cand.sku, channel,
                                 experiment.start, experiment.end)
        feasible = True
        code, note = "", "趋势可比，窗口空闲"
        if cand.status == "排除":
            feasible, code = False, "S_CONTROL_OVERLAP"
            note = f"实验期内有 {cand.overlap_days} 天自有促销"
        elif cand.status == "不可比":
            feasible, code = False, "D_CONTROL_SHORT"
            note = "前置半段天数不足，趋势不可比"
        elif busy:
            feasible, code = False, "S_CONTROL_BUSY"
            note = f"窗口与已占用实验 {', '.join(busy)} 重叠"
        elif cand.status == "偏差":
            # 趋势差超门槛只排序不淘汰：硬约束之外尽量选最接近的
            code, note = "D_CONTROL_TREND", f"前置趋势差 {cand.trend_gap:.4f}，偏大但可用"
        if cand.registered and feasible:
            note = f"{note} · 映射内"
        edges.append(Edge(
            experiment_id=experiment.experiment_id,
            sku=cand.sku, channel=channel, registered=cand.registered,
            trend_gap=cand.trend_gap, level_ratio=cand.level_ratio,
            target_trend=cand.target_trend, control_trend=cand.control_trend,
            feasible=feasible, code=code, note=note, busy_with=busy,
            overlap_dates=cand.overlap_dates,
        ))
    edges.sort(key=lambda e: (0 if e.feasible else 1, e.trend_gap, e.sku))
    return edges


def _internal_conflict(assignment: dict, exp_by_id: dict, control_sku: str,
                       channel: str, start: dt.date, end: dt.date,
                       experiment_id: str) -> str | None:
    """本批已分配的实验里，是否已有同渠道同对照且窗口重叠的。"""
    for other_id, other_sku in assignment.items():
        if other_id == experiment_id or other_sku != control_sku:
            continue
        other = exp_by_id[other_id]
        if other.channel != channel:
            continue
        if windows_overlap(start, end, other.start, other.end):
            return other_id
    return None


def solve(experiments: list[Experiment], edges_by_id: dict) -> dict:
    """MRV + 回溯求趋势差总和最小的完整匹配；给不出完整匹配就返回阻断清单。

    返回 {"feasible": bool, "assignment": {exp_id: control_sku},
          "total_gap": float, "blocked": [...], "exhausted": bool}
    """
    exp_by_id = {e.experiment_id: e for e in experiments}
    feasible_edges: dict[str, list[Edge]] = {}
    blocked: list[dict] = []
    for exp in experiments:
        edges = [e for e in edges_by_id[exp.experiment_id] if e.feasible]
        if not edges:
            all_edges = edges_by_id[exp.experiment_id]
            blocked.append({
                "experiment_id": exp.experiment_id,
                "reason": "S_NO_CANDIDATE" if not all_edges else "S_NO_FEASIBLE",
                "detail": ("同渠道没有任何其它商品" if not all_edges
                           else "候选全部触发硬约束，见候选明细"),
            })
        else:
            feasible_edges[exp.experiment_id] = edges
    if blocked:
        return {"feasible": False, "assignment": {}, "total_gap": None,
                "blocked": blocked, "diagnostic": {}, "exhausted": False}

    # 每个实验按（趋势差, sku）排序，边顺序即偏好顺序
    ranked = {exp_id: sorted(edges, key=lambda e: (e.trend_gap, e.sku))
              for exp_id, edges in feasible_edges.items()}

    best: dict | None = None
    best_gap = float("inf")
    nodes = 0

    def order_ids(assignment: dict) -> list[str]:
        # MRV：剩余可行候选最少的先选；并列时候选最优趋势差更小的先选
        remaining = [exp_id for exp_id in ranked if exp_id not in assignment]
        remaining.sort(key=lambda exp_id: (
            sum(1 for e in ranked[exp_id]
                if _internal_conflict(assignment, exp_by_id, e.sku, e.channel,
                                      exp_by_id[exp_id].start, exp_by_id[exp_id].end,
                                      exp_id) is None),
            ranked[exp_id][0].trend_gap,
            exp_id,
        ))
        return remaining

    def backtrack(assignment: dict, total_gap: float) -> bool:
        """返回 True 表示触顶停止扩展；False 表示本子树已正常搜完。"""
        nonlocal best, best_gap, nodes
        nodes += 1
        if nodes > SEARCH_NODE_LIMIT:
            return True  # 触顶停止扩展，交给调用方按已有最优或贪心兜底
        if len(assignment) == len(ranked):
            if total_gap < best_gap:
                best, best_gap = dict(assignment), total_gap
            return False
        for exp_id in order_ids(assignment):
            if exp_id in assignment:
                continue
            exp = exp_by_id[exp_id]
            tried_any = False
            for edge in ranked[exp_id]:
                conflict = _internal_conflict(assignment, exp_by_id, edge.sku,
                                               edge.channel, exp.start, exp.end, exp_id)
                if conflict is not None:
                    continue
                # 分支定界：已经不可能优于当前最优就不展开
                if total_gap + edge.trend_gap >= best_gap:
                    continue
                tried_any = True
                assignment[exp_id] = edge.sku
                hit_limit = backtrack(assignment, total_gap + edge.trend_gap)
                del assignment[exp_id]
                if hit_limit:
                    return True
            if not tried_any:
                return False
            break
        return False

    hit_limit = backtrack({}, 0.0)

    # 触顶前若已找到完整匹配，它就是当前最优可行解，直接返回
    if best is not None:
        return {"feasible": True, "assignment": best, "total_gap": best_gap,
                "blocked": [], "exhausted": False}

    # 回溯不可行：用 MRV 贪心给出「卡在哪一场、与谁抢」的诊断信息。
    # 贪心分配只用于页面诊断，绝不作为可确认的结果；真正卡死的实验留空并带原因。
    greedy: dict[str, str] = {}
    greedy_blocked: list[dict] = []
    for exp in sorted(experiments, key=lambda e: (len(ranked[e.experiment_id]),
                                                  e.experiment_id)):
        chosen = None
        for edge in ranked[exp.experiment_id]:
            if _internal_conflict(greedy, exp_by_id, edge.sku, edge.channel,
                                  exp.start, exp.end, exp.experiment_id) is None:
                chosen = edge.sku
                break
        if chosen is None:
            # 记录它还能选哪些对照、分别被谁占着，方便人工改期
            busy = []
            for edge in ranked[exp.experiment_id]:
                holder = _internal_conflict(greedy, exp_by_id, edge.sku, edge.channel,
                                            exp.start, exp.end, exp.experiment_id)
                if holder is not None:
                    busy.append({"control_sku": edge.sku, "held_by": holder})
            greedy_blocked.append({
                "experiment_id": exp.experiment_id,
                "reason": "S_NO_FEASIBLE",
                "detail": "所有可行对照都与同批其它实验窗口重叠",
                "contention": busy,
            })
        else:
            greedy[exp.experiment_id] = chosen
    return {"feasible": False, "assignment": {}, "total_gap": None,
            "blocked": greedy_blocked, "diagnostic": greedy,
            "exhausted": not hit_limit}
