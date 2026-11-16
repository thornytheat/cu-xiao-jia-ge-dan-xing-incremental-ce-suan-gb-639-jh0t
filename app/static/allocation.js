/* allocation.js — M8 批次对照排程：方案生成、整批确认、失败重试、占用与候选渲染。
   只调用 /api/allocation/*，所有状态来自服务端，前端不缓存占用，避免重复占位。 */
(function () {
  "use strict";

  var state = { plan: null, busy: false };

  function $(id) { return document.getElementById(id); }

  function note(el, kind, text) {
    if (!el) { return; }
    el.className = "form-note" + (kind ? " is-" + kind : "");
    el.textContent = text;
    el.hidden = false;
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function tagClass(level) {
    if (level === "error") { return "error"; }
    if (level === "warn") { return "warn"; }
    if (level === "ok") { return "ok"; }
    return "info";
  }

  function api(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (response) {
      return response.json().then(function (body) {
        return { ok: response.ok, status: response.status, body: body };
      });
    });
  }

  function renderAssignments(plan) {
    var body = $("assignment-body");
    body.innerHTML = "";
    var matched = 0;
    plan.assignments.forEach(function (item) {
      var exp = item.experiment;
      if (item.status === "已配对") { matched += 1; }
      var tr = document.createElement("tr");
      if (item.status === "无法满足") { tr.setAttribute("data-level", "error"); }
      tr.innerHTML =
        "<td>" + esc(exp.experiment_id) + "</td>" +
        "<td>" + esc(exp.sku) + "</td>" +
        "<td>" + esc(exp.channel) + "</td>" +
        "<td>" + esc(exp.start_date) + " → " + esc(exp.end_date) +
        "（" + exp.days + " 天）</td>" +
        "<td><strong>" + (item.control_sku ? esc(item.control_sku) : "—") + "</strong></td>" +
        "<td class='num'>" + (item.trend_gap == null ? "—" : esc(item.trend_gap)) + "</td>" +
        "<td>" + (item.registered ? "映射内" : "—") + "</td>" +
        "<td><span class='tag tag-" +
          (item.control_sku ? (item.status === "诊断配对" ? "warn" : "ok") : "error") +
          "'>" + esc(item.status) + "</span></td>" +
        "<td class='trunc' title='" + esc(item.block_detail || "") + "'>" +
        esc(item.block_reason ? item.block_reason + " · " + item.block_detail
                              : "独立对照，窗口不撞车") + "</td>";
      body.appendChild(tr);
    });
    return matched;
  }

  function renderCandidates(plan) {
    var wrap = $("candidate-groups");
    wrap.innerHTML = "";
    Object.keys(plan.candidates).forEach(function (expId) {
      var rows = plan.candidates[expId];
      var block = document.createElement("div");
      block.className = "candidate-block";
      var html = "<h3 class='candidate-title'>" + esc(expId) + " 的候选（" +
        rows.length + " 个）</h3><div class='table-wrap'><table class='grid'><thead><tr>" +
        "<th>候选对照</th><th>渠道</th><th class='num'>趋势差</th><th class='num'>水平比</th>" +
        "<th>结论</th><th>原因码</th><th>说明</th></tr></thead><tbody>";
      rows.forEach(function (row) {
        html += "<tr" + (row.feasible ? "" : " data-level='error'") + ">" +
          "<td>" + esc(row.sku) + (row.registered ? " <small>映射内</small>" : "") + "</td>" +
          "<td>" + esc(row.channel) + "</td>" +
          "<td class='num'>" + esc(row.trend_gap) + "</td>" +
          "<td class='num'>" + esc(row.level_ratio) + "</td>" +
          "<td><span class='tag tag-" + (row.feasible ? "ok" : "error") + "'>" +
          esc(row.status) + "</span></td>" +
          "<td>" + (row.code ? esc(row.code) : "—") + "</td>" +
          "<td class='trunc' title='" + esc(row.note) + "'>" + esc(row.note) + "</td></tr>";
      });
      html += "</tbody></table></div>";
      block.innerHTML = html;
      wrap.appendChild(block);
    });
    $("candidate-section").hidden = false;
  }

  function renderQuarantine(plan) {
    var body = $("quarantine-body");
    body.innerHTML = "";
    plan.quarantine.forEach(function (row) {
      var tr = document.createElement("tr");
      tr.setAttribute("data-level", row.level);
      tr.innerHTML =
        "<td>" + esc(row.code) + "</td>" +
        "<td><span class='tag tag-" + tagClass(row.level) + "'>" + esc(row.level) + "</span></td>" +
        "<td class='trunc' title='" + esc(row.title) + "'>" + esc(row.title) + "</td>" +
        "<td class='trunc' title='" + esc(row.detail) + "'>" + esc(row.detail || "—") + "</td>" +
        "<td class='trunc' title='" + esc(row.hint) + "'>" + esc(row.hint) + "</td>";
      body.appendChild(tr);
    });
    $("quarantine-count").textContent = plan.quarantine.length + " 条";
    $("schedule-quarantine").hidden = false;
  }

  function renderOccupancy(occupancy) {
    var body = $("occupancy-body");
    body.innerHTML = "";
    occupancy.forEach(function (row) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(row.experiment_id) + "</td>" +
        "<td>" + esc(row.target_sku) + "</td>" +
        "<td>" + esc(row.control_sku) + "</td>" +
        "<td>" + esc(row.channel) + "</td>" +
        "<td>" + esc(row.start_date) + " → " + esc(row.end_date) + "</td>" +
        "<td>" + esc(row.plan_id) + "</td>";
      body.appendChild(tr);
    });
    $("occupancy-empty").hidden = occupancy.length > 0;
  }

  function renderHistory(plans) {
    if (!plans || !plans.length) {
      $("plan-history-section").hidden = true;
      return;
    }
    var body = $("plan-history-body");
    body.innerHTML = "";
    plans.forEach(function (row) {
      var cls = row.status === "confirmed" ? "ok"
        : (row.status === "superseded" ? "error" : "quiet");
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(row.plan_id) + "</td>" +
        "<td><span class='tag tag-" + cls + "'>" + esc(row.status) + "</span></td>" +
        "<td>" + esc((row.created_at || "").replace("T", " ").slice(0, 19)) + "</td>" +
        "<td>" + (row.confirmed_at
          ? esc(String(row.confirmed_at).replace("T", " ").slice(0, 19)) : "—") + "</td>" +
        "<td>" + esc(row.superseded_by || "—") + "</td>";
      body.appendChild(tr);
    });
    $("plan-history-section").hidden = false;
  }

  function renderPlan(plan) {
    state.plan = plan;
    var matched = renderAssignments(plan);
    renderCandidates(plan);
    renderQuarantine(plan);
    $("plan-result").hidden = false;
    $("plan-id").innerHTML = esc(plan.plan_id) + " <small>草稿</small>";
    $("plan-meta").textContent = "生成于 " + plan.generated_at.replace("T", " ");
    $("plan-count").textContent = String(plan.assignments.length);
    $("plan-matched").textContent = String(matched);
    $("plan-blocked").textContent = String(plan.assignments.length - matched);
    $("plan-gap").textContent = plan.total_gap == null ? "—" : String(plan.total_gap);
    var verdict = $("plan-verdict");
    var confirmBtn = $("confirm-btn");
    if (plan.feasible) {
      verdict.textContent = "全部实验都有独立且不撞促销窗口的对照，可整批确认";
      verdict.className = "verdict-note";
      confirmBtn.disabled = false;
    } else {
      verdict.textContent = "存在无法满足的实验，已明确标出；禁止确认，禁止拿复用对照凑数";
      verdict.className = "verdict-note is-error-text";
      confirmBtn.disabled = true;
    }
    note($("confirm-note"), "", "");
    $("confirm-note").hidden = true;
  }

  function refreshState() {
    return fetch("/api/allocation")
      .then(function (r) { return r.json(); })
      .then(function (view) {
        renderOccupancy(view.occupancy || []);
        renderHistory(view.plans || []);
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = $("plan-form");
    var input = $("experiments-input");

    document.querySelectorAll("[data-fill]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var source = btn.getAttribute("data-fill") === "stress"
          ? $("stress-payload") : $("sample-payload");
        input.value = source ? source.value : "";
      });
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (state.busy) { return; }
      state.busy = true;
      var button = form.querySelector("button[type=submit]");
      button.disabled = true;
      note(form.querySelector(".form-note"), "", "排程计算中…");
      api("/api/allocation/plan", { experiments: input.value })
        .then(function (result) {
          state.busy = false;
          button.disabled = false;
          if (!result.ok) {
            note(form.querySelector(".form-note"), "error",
              result.body.error + " · " + result.body.message);
            return;
          }
          note(form.querySelector(".form-note"), "ok",
            "方案 " + result.body.plan_id + " 已生成（草稿，尚未占用任何对照）");
          renderPlan(result.body);
          refreshState();
        })
        .catch(function (error) {
          state.busy = false;
          button.disabled = false;
          note(form.querySelector(".form-note"), "error", "网络不可用 · " + error.message);
        });
    });

    $("confirm-btn").addEventListener("click", function () {
      if (!state.plan || state.busy) { return; }
      var btn = $("confirm-btn");
      state.busy = true;
      btn.disabled = true;
      note($("confirm-note"), "", "正在整批占用…");
      api(btn.getAttribute("data-endpoint"), { plan_id: state.plan.plan_id })
        .then(function (result) {
          state.busy = false;
          btn.disabled = false;
          if (!result.ok) {
            note($("confirm-note"), "error",
              result.body.error + " · " + result.body.message +
              "（上一版分配保持不变，可点「按原分配重试」）");
            return;
          }
          note($("confirm-note"), "ok",
            "整批占用成功：" + result.body.occupancy.length +
            " 条占用，旧版方案已标记 superseded");
          state.plan = null;
          refreshState();
        })
        .catch(function (error) {
          state.busy = false;
          btn.disabled = false;
          note($("confirm-note"), "error", "网络不可用 · " + error.message);
        });
    });

    $("retry-btn").addEventListener("click", function () {
      if (state.busy) { return; }
      var refId = state.plan ? state.plan.plan_id : null;
      if (!refId) {
        note($("confirm-note"), "error", "没有可重试的方案，请先生成排程方案");
        return;
      }
      var btn = $("retry-btn");
      state.busy = true;
      btn.disabled = true;
      note($("confirm-note"), "", "按原实验清单重新排程，不触碰现有占用…");
      api(btn.getAttribute("data-endpoint"), { plan_id: refId })
        .then(function (result) {
          state.busy = false;
          btn.disabled = false;
          if (!result.ok) {
            note($("confirm-note"), "error",
              result.body.error + " · " + result.body.message);
            return;
          }
          note($("confirm-note"), "ok",
            "已生成新草稿 " + result.body.plan_id + "，原分配未改动、未重复占位");
          renderPlan(result.body);
          refreshState();
        })
        .catch(function (error) {
          state.busy = false;
          btn.disabled = false;
          note($("confirm-note"), "error", "网络不可用 · " + error.message);
        });
    });

    refreshState();
  });
})();
