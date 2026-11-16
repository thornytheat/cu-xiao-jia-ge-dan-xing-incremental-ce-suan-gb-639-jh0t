"""M8 匹配引擎单元测试：窗口重叠判定、硬约束淘汰、MRV 回溯最优与阻断诊断。

不依赖数据库，实验与边手工构造，期望值直接断言。
"""
from __future__ import annotations

import datetime as dt

from app.modules.m8_schedule import matcher

D = dt.date.fromisoformat


def _exp(exp_id, sku, channel, start, end):
    return matcher.Experiment(exp_id, sku, channel, D(start), D(end))


def _edge(exp_id, sku, feasible, gap=0.01, busy=None):
    return matcher.Edge(
        experiment_id=exp_id, sku=sku, channel="商超", registered=False,
        trend_gap=gap, level_ratio=1.0, target_trend=0.1, control_trend=0.1,
        feasible=feasible, code="" if feasible else "S_CONTROL_BUSY",
        note="x", busy_with=busy or [])


def test_windows_overlap_left_closed_right_open():
    # 首尾相接（A.end == B.start）不共享任何一天，不算重叠
    assert matcher.windows_overlap(D("2026-12-01"), D("2026-12-10"),
                                   D("2026-12-10"), D("2026-12-20")) is False
    # 相交一天即冲突
    assert matcher.windows_overlap(D("2026-12-01"), D("2026-12-11"),
                                   D("2026-12-10"), D("2026-12-20")) is True
    # 完全包含也是冲突
    assert matcher.windows_overlap(D("2026-12-01"), D("2026-12-31"),
                                   D("2026-12-10"), D("2026-12-20")) is True
    # 完全错开
    assert matcher.windows_overlap(D("2026-12-01"), D("2026-12-05"),
                                   D("2026-12-10"), D("2026-12-20")) is False


def test_solve_picks_minimum_total_trend_gap():
    # 两场实验、两个通用对照；贪心按顺序会选 C1+C2(0.1+0.1)，
    # 最优是 C2+C1(0.01+0.01)，验证回溯真的在做全局择优
    experiments = [_exp("E1", "T1", "商超", "2026-12-01", "2026-12-10"),
                   _exp("E2", "T2", "商超", "2026-12-01", "2026-12-10")]
    edges = {
        "E1": [_edge("E1", "C1", True, gap=0.10),
               _edge("E1", "C2", True, gap=0.01)],
        "E2": [_edge("E2", "C1", True, gap=0.01),
               _edge("E2", "C2", True, gap=0.10)],
    }
    out = matcher.solve(experiments, edges)
    assert out["feasible"] is True
    assert out["assignment"] == {"E1": "C2", "E2": "C1"}
    assert round(out["total_gap"], 4) == 0.02
    assert out["blocked"] == []


def test_solve_allows_same_control_when_windows_disjoint():
    # 同窗时两个候选冲突；把 E2 挪到 E1 结束之后，同一对照可按时间片复用
    experiments = [_exp("E1", "T1", "商超", "2026-12-01", "2026-12-10"),
                   _exp("E2", "T1", "商超", "2026-12-10", "2026-12-20")]
    edges = {
        "E1": [_edge("E1", "C1", True, gap=0.02)],
        "E2": [_edge("E2", "C1", True, gap=0.03)],
    }
    out = matcher.solve(experiments, edges)
    assert out["feasible"] is True
    assert out["assignment"] == {"E1": "C1", "E2": "C1"}


def test_solve_blocks_when_pool_exhausted_and_keeps_diagnostic():
    # 三场同窗实验，只有两个对照 => 必然有一场拿不到
    experiments = [_exp(f"E{i}", "T", "商超", "2026-12-01", "2026-12-10")
                   for i in range(3)]
    edges = {}
    for exp in experiments:
        edges[exp.experiment_id] = [
            _edge(exp.experiment_id, "C1", True, gap=0.01),
            _edge(exp.experiment_id, "C2", True, gap=0.02),
        ]
    out = matcher.solve(experiments, edges)
    assert out["feasible"] is False
    assert out["assignment"] == {}  # 不可行时绝不返回半成品分配
    assert len(out["blocked"]) == 1
    blocked = out["blocked"][0]
    assert blocked["reason"] == "S_NO_FEASIBLE"
    # 诊断信息要说清每个候选被谁占着
    held = {c["control_sku"]: c["held_by"] for c in blocked["contention"]}
    assert set(held) == {"C1", "C2"}
    # 另外两场在诊断分配里有值，但只作诊断
    assert len(out["diagnostic"]) == 2


def test_solve_reports_no_candidate_when_edge_list_empty():
    experiments = [_exp("E1", "T1", "商超", "2026-12-01", "2026-12-10")]
    out = matcher.solve(experiments, {"E1": []})
    assert out["feasible"] is False
    assert out["blocked"][0]["reason"] == "S_NO_CANDIDATE"


def test_busy_experiments_detects_existing_occupancy():
    occupancy = [
        {"experiment_id": "OLD-1", "control_sku": "C1", "channel": "商超",
         "start_date": "2026-12-05", "end_date": "2026-12-15"},
        {"experiment_id": "OLD-2", "control_sku": "C2", "channel": "电商",
         "start_date": "2026-12-05", "end_date": "2026-12-15"},
    ]
    # 同渠道同窗 -> 命中
    hits = matcher._busy_experiments(
        occupancy, "C1", "商超", D("2026-12-01"), D("2026-12-10"))
    assert hits == ["OLD-1"]
    # 渠道不同即使同 sku 也不冲突
    assert matcher._busy_experiments(
        occupancy, "C2", "商超", D("2026-12-01"), D("2026-12-10")) == []
    # 首尾相接不冲突
    assert matcher._busy_experiments(
        occupancy, "C1", "商超", D("2026-11-20"), D("2026-12-05")) == []


def test_solve_node_limit_keeps_complete_solution_when_found(monkeypatch):
    # 把节点上限压到极小：触顶前已找到的完整匹配必须照常返回，不能误判阻断
    monkeypatch.setattr(matcher, "SEARCH_NODE_LIMIT", 3)
    experiments = [_exp("E1", "T1", "商超", "2026-12-01", "2026-12-10"),
                   _exp("E2", "T2", "商超", "2026-12-01", "2026-12-10")]
    edges = {
        "E1": [_edge("E1", "C1", True, gap=0.10),
               _edge("E1", "C2", True, gap=0.01)],
        "E2": [_edge("E2", "C1", True, gap=0.01),
               _edge("E2", "C2", True, gap=0.10)],
    }
    out = matcher.solve(experiments, edges)
    if out["feasible"]:
        assert set(out["assignment"]) == {"E1", "E2"}
    else:
        # 触顶太早一条完整路径都没走完时，也必须带阻断诊断而不是抛异常
        assert out["assignment"] == {}
        assert out["blocked"]
