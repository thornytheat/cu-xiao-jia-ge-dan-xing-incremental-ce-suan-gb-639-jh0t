"""M8 批次对照排程：解析校验、方案生成、整批确认原子性、顶替、重试与页面。

核心断言：
- 不可行方案禁止确认，占用表一行都不增加；
- 确认是全有或全无，成功后旧版变 superseded、占用不重复；
- 重试只产 draft，不触碰已确认占用；
- 并发确认只有一个赢家，输家 409 且占用不出现混合态。
"""
from __future__ import annotations

import threading

from app.modules.m8_schedule import service

PAGE = "/allocation"


def _plan(client, text):
    return client.post("/api/allocation/plan", json={"experiments": text})


def test_plan_generates_feasible_assignment_with_disjoint_reuse(client_run):
    body = _plan(client_run, service.sample_text()).json()
    assert body["feasible"] is True
    assert len(body["assignments"]) == 4
    assert all(a["status"] == "已配对" for a in body["assignments"])
    assert body["plan_id"].startswith("PLAN-")
    # 草稿阶段不写占用
    assert client_run.get("/api/allocation").json()["occupancy"] == []
    # 每场实验都有完整候选明细与原因码列
    for exp_id, candidates in body["candidates"].items():
        assert candidates
        assert all(c["experiment_id"] == exp_id for c in candidates)
    # 同一对照可在不重叠窗口复用（示例里 MY-0028 服务两场首尾相接的实验）
    used = [a["control_sku"] for a in body["assignments"]]
    assert len(used) != len(set(used))


def test_confirm_is_all_or_nothing_and_supersedes_previous(client_run):
    first = _plan(client_run, service.sample_text()).json()
    r1 = client_run.post("/api/allocation/confirm", json={"plan_id": first["plan_id"]})
    assert r1.status_code == 200
    assert len(r1.json()["occupancy"]) == 4

    # 第二版确认：旧版变 superseded，占用整体替换，不累积重复行
    second = _plan(client_run, "EXP-1401,MY-0045,商超,2026-12-20,2026-12-31").json()
    assert second["feasible"] is True
    r2 = client_run.post("/api/allocation/confirm", json={"plan_id": second["plan_id"]})
    assert r2.status_code == 200
    occupancy = r2.json()["occupancy"]
    assert len(occupancy) == 1
    assert occupancy[0]["experiment_id"] == "EXP-1401"
    plans = client_run.get("/api/allocation").json()["plans"]
    statuses = {p["plan_id"]: p["status"] for p in plans}
    assert statuses[first["plan_id"]] == "superseded"
    assert statuses[second["plan_id"]] == "confirmed"


def test_infeasible_plan_cannot_be_confirmed_and_leaves_no_occupancy(client_run):
    body = _plan(client_run, service.sample_text(stress=True)).json()
    assert body["feasible"] is False
    blocked = {a["experiment"]["experiment_id"]: a for a in body["assignments"]
               if a["status"] == "无法满足"}
    assert blocked, "必须明确标出无法满足的实验"
    for item in blocked.values():
        assert item["block_reason"] == "S_NO_FEASIBLE"
        assert item["control_sku"] is None
    # 其余实验给诊断配对，但明确不是可确认结果
    diagnostic = [a for a in body["assignments"] if a["status"] == "诊断配对"]
    assert diagnostic and all(a["control_sku"] for a in diagnostic)

    resp = client_run.post("/api/allocation/confirm", json={"plan_id": body["plan_id"]})
    assert resp.status_code == 422
    assert resp.json()["error"] == "S_NO_FEASIBLE"
    assert client_run.get("/api/allocation").json()["occupancy"] == []


def test_retry_creates_draft_without_duplicate_occupancy(client_run):
    plan_a = _plan(client_run, service.sample_text()).json()
    client_run.post("/api/allocation/confirm", json={"plan_id": plan_a["plan_id"]})
    before = client_run.get("/api/allocation").json()["occupancy"]
    assert len(before) == 4

    retry = client_run.post("/api/allocation/retry",
                            json={"plan_id": plan_a["plan_id"]})
    assert retry.status_code == 200
    new_draft = retry.json()
    assert new_draft["plan_id"] != plan_a["plan_id"]
    assert new_draft["feasible"] is True
    # 重试草稿不产生任何新占用，原分配原样保留
    mid = client_run.get("/api/allocation").json()["occupancy"]
    assert mid == before

    # 确认重试草稿后仍然只有 4 条占用（替换而非追加）
    confirmed = client_run.post(
        "/api/allocation/confirm", json={"plan_id": new_draft["plan_id"]})
    assert confirmed.status_code == 200
    assert len(confirmed.json()["occupancy"]) == 4


def test_double_confirm_and_unknown_plan_are_rejected(client_run):
    plan_a = _plan(client_run, service.sample_text()).json()
    client_run.post("/api/allocation/confirm", json={"plan_id": plan_a["plan_id"]})
    again = client_run.post("/api/allocation/confirm",
                            json={"plan_id": plan_a["plan_id"]})
    assert again.status_code == 409
    assert again.json()["error"] == "S_PLAN_STALE"
    missing = client_run.post("/api/allocation/confirm",
                              json={"plan_id": "PLAN-does-not-exist"})
    assert missing.status_code == 422
    assert missing.json()["error"] == "S_PLAN_STALE"
    retry_missing = client_run.post("/api/allocation/retry",
                                    json={"plan_id": "PLAN-does-not-exist"})
    assert retry_missing.status_code == 422


def test_existing_occupancy_blocks_overlapping_candidate(client_run):
    plan_a = _plan(client_run, service.sample_text()).json()
    client_run.post("/api/allocation/confirm", json={"plan_id": plan_a["plan_id"]})
    # 与已占用的 EXP-1202（MY-0034 电商，12/08-21）同窗的新实验
    body = _plan(client_run,
                 "EXP-1301,MY-0021,电商,2026-12-10,2026-12-20").json()
    busy = [c for c in body["candidates"]["EXP-1301"]
            if c["code"] == "S_CONTROL_BUSY"]
    assert busy, "占用撞车必须在候选明细里标出"
    assert all(c["busy_with"] for c in busy)
    # 整批仍可行：还有空闲对照，但绝不复用被占对照
    assert body["feasible"] is True
    picked = next(a for a in body["assignments"]
                  if a["experiment"]["experiment_id"] == "EXP-1301")
    assert picked["control_sku"] not in {c["sku"] for c in busy}


def test_promo_overlap_candidate_is_excluded(client_run):
    # MY-0012 电商在 2026-10-05~25 有生效促销，该窗内它不能当对照
    body = _plan(client_run,
                 "EXP-PROMO,MY-0021,电商,2026-10-10,2026-10-20").json()
    cand = {c["sku"]: c for c in body["candidates"]["EXP-PROMO"]}
    assert cand["MY-0012"]["status"] == "排除"
    assert cand["MY-0012"]["code"] == "S_CONTROL_OVERLAP"
    assert cand["MY-0012"]["overlap_dates"]


def test_input_validation_rejects_bad_rows(client_run):
    cases = [
        ("EXP-X,MY-0012,电商,2026-12-01", "S_BAD_ROW"),            # 缺列
        ("EXP-X,MY-0012,电商,2026-12-20,2026-12-01", "S_BAD_ROW"),  # 结束早于开始
        ("EXP-X,MY-0012,电商,not-date,2026-12-01", "S_BAD_ROW"),   # 坏日期
        ("", "S_BAD_ROW"),                                         # 空清单
        ("EXP-X,MY-9999,电商,2026-12-01,2026-12-10", "S_UNKNOWN_SERIES"),
    ]
    for text, code in cases:
        resp = _plan(client_run, text)
        assert resp.status_code == 422, text
        assert resp.json()["error"] == code, text

    dup = ("EXP-X,MY-0012,电商,2026-12-01,2026-12-10\n"
           "EXP-X,MY-0021,电商,2026-12-01,2026-12-10")
    resp = _plan(client_run, dup)
    assert resp.status_code == 422
    assert resp.json()["error"] == "S_DUP_EXPERIMENT"
    # 任何 422 都不能留下草稿或占用
    state = client_run.get("/api/allocation").json()
    assert state["occupancy"] == []
    assert state["plans"] == []


def test_comment_and_blank_lines_are_skipped(client_run):
    text = ("# 下月批次\n"
            "\n"
            "EXP-1,MY-0012,商超,2026-12-01,2026-12-10\n")
    body = _plan(client_run, text).json()
    assert len(body["assignments"]) == 1


def test_schedule_requires_batch(client):
    resp = client.post("/api/allocation/plan", json={"experiments": "x"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "S_NO_BATCH"
    assert client.get("/api/allocation").json()["batch_id"] is None


def test_concurrent_confirm_has_single_winner_and_no_mixed_occupancy(client_run):
    client = client_run
    plan_a = _plan(client, service.sample_text()).json()
    plan_b = _plan(client,
                   "EXP-1401,MY-0045,商超,2026-12-20,2026-12-31").json()
    results: dict = {}
    barrier = threading.Barrier(2)

    def confirm(key, plan_id):
        barrier.wait()
        resp = client.post("/api/allocation/confirm", json={"plan_id": plan_id})
        results[key] = (resp.status_code, resp.json().get("error", "ok"))

    threads = [threading.Thread(target=confirm, args=("a", plan_a["plan_id"])),
               threading.Thread(target=confirm, args=("b", plan_b["plan_id"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [k for k, v in results.items() if v[0] == 200]
    losers = [k for k, v in results.items() if v[0] != 200]
    assert winners == ["a"] or winners == ["b"]
    assert len(losers) == 1
    assert results[losers[0]][1] == "S_PLAN_SUPERSEDED"

    win_id = plan_a["plan_id"] if winners == ["a"] else plan_b["plan_id"]
    occupancy = client.get("/api/allocation").json()["occupancy"]
    # 赢的方案完整生效，输家一行占用都没混进去
    assert occupancy and all(row["plan_id"] == win_id for row in occupancy)

    # 输家走「重试 → 再确认」可以成功，占用仍然整批替换、不重复
    loser_plan = plan_b if winners == ["a"] else plan_a
    retry = client.post("/api/allocation/retry",
                        json={"plan_id": loser_plan["plan_id"]}).json()
    reconfirmed = client.post("/api/allocation/confirm",
                              json={"plan_id": retry["plan_id"]})
    assert reconfirmed.status_code == 200
    assert all(row["plan_id"] == retry["plan_id"]
               for row in reconfirmed.json()["occupancy"])


def test_allocation_page_renders(client_run):
    resp = client_run.get(PAGE)
    assert resp.status_code == 200
    text = resp.text
    for title in ("提交实验批次", "排程结果", "候选对照与淘汰原因",
                  "当前已确认占用", "方案历史", "排程校验"):
        assert title in text, title
    assert 'href="/allocation"' in text
    assert 'aria-current="page"' in text
    # 导航在所有页面可达
    for path in ("/", "/design", "/report"):
        assert f'href="{path}"' in text
