(() => {
  "use strict";

  let dashboardPayload = null;
  let anonWindow = "24h";
  let autoTimer = null;
  const charts = {};

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtInt(n) {
    const num = Number(n ?? 0);
    return Number.isFinite(num) ? Math.round(num).toLocaleString("en-US") : "0";
  }

  function fmtNum(n, digits = 2) {
    const num = Number(n ?? 0);
    if (!Number.isFinite(num)) return "0";
    return num.toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function fmtDuration(sec) {
    const s = Math.max(0, Math.round(Number(sec ?? 0)));
    if (s < 60) return `${s}s`;
    const mins = Math.floor(s / 60);
    const rem = s % 60;
    if (mins < 60) return `${mins}m ${rem}s`;
    const hrs = Math.floor(mins / 60);
    const rMins = mins % 60;
    return `${hrs}h ${rMins}m`;
  }

  function initChartDefaults() {
    if (typeof Chart === "undefined") return;
    Chart.defaults.color = "#94a3b8";
    Chart.defaults.borderColor = "rgba(148, 163, 184, 0.12)";
    Chart.defaults.font.family = "'Inter', -apple-system, BlinkMacSystemFont, sans-serif";
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.boxWidth = 9;
    Chart.defaults.plugins.legend.labels.color = "#cbd5e1";
    Chart.defaults.plugins.tooltip.backgroundColor = "rgba(5, 8, 18, 0.94)";
    Chart.defaults.plugins.tooltip.borderColor = "rgba(56, 189, 248, 0.4)";
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.titleColor = "#f8fafc";
    Chart.defaults.plugins.tooltip.bodyColor = "#bae6fd";
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 10;
  }

  function renderChart(canvasId, config) {
    if (typeof Chart === "undefined") return;
    const canvas = $(canvasId);
    if (!canvas) return;
    if (charts[canvasId]) {
      charts[canvasId].destroy();
    }
    const ctx = canvas.getContext("2d");
    charts[canvasId] = new Chart(ctx, config);
  }

  function renderExecutiveKpis(data) {
    const live = data.live || {};
    const adv = data.anonymous_chat_advanced || {};
    const w24 = adv.window_24h || {};
    const w30 = adv.window_30d || {};
    const wAll = adv.window_all_time || {};
    const peaks = adv.peak_hours || {};
    const ai = data.ai_assistant || {};
    const bot = data.bot_overview || {};
    const web = data.website || {};
    const webUsers = web.users || {};
    const modules = web.modules || {};
    const chatMod = modules.campus_chat || {};
    const fbMod = modules.feedbacks || {};

    const cards = [
      {
        accent: "accent-emerald",
        title: "Live Telemetry Now",
        badge: `${fmtInt(live.web_ws_clients)} WS`,
        value: fmtInt(live.active_sessions_now),
        unit: "active anon chats",
        subs: [
          ["Actively Chatting", fmtInt(live.actively_chatting_now)],
          ["Paused Chats", fmtInt(live.paused_now)],
          ["Search Queue Now", fmtInt(bot.searching_users)],
          ["24h Queue Mean", fmtNum(live.queue_mean_24h, 2)],
        ],
      },
      {
        accent: "accent-ice",
        title: "Active Sessions Median & Mean",
        badge: "Concurrency",
        value: fmtNum(w24.concurrent_median, 2),
        unit: `24h median (mean ${fmtNum(w24.concurrent_mean, 2)})`,
        subs: [
          ["30d Concurrent Median", fmtNum(w30.concurrent_median, 2)],
          ["30d Concurrent Mean", fmtNum(w30.concurrent_mean, 2)],
          ["24h Peak Concurrent", fmtInt(w24.concurrent_peak)],
          ["30d / All-Time Peak", `${fmtInt(w30.concurrent_peak)} / ${fmtInt(wAll.concurrent_peak)}`],
        ],
      },
      {
        accent: "accent-ice",
        title: "Chats Initiated Totally",
        badge: "Anonymous Bot",
        value: fmtInt(wAll.initiated),
        unit: "all-time initiated",
        subs: [
          ["Last 24h Initiated", `${fmtInt(w24.initiated)} (${fmtNum(w24.engagement_rate, 1)}% engaged)`],
          ["Last 30d Initiated", `${fmtInt(w30.initiated)} (${fmtNum(w30.engagement_rate, 1)}% engaged)`],
          ["30d Daily Mean", fmtNum(w30.chats_initiated_daily_mean, 1)],
          ["30d Daily Median", fmtNum(w30.chats_initiated_daily_median, 1)],
        ],
      },
      {
        accent: "accent-amber",
        title: "Most Active Chat Hours (AMT)",
        badge: "Peak Clock",
        value: peaks.most_active_init_window || "—",
        unit: "peak initiation hour (30d)",
        subs: [
          ["Peak Concurrency Hour", peaks.most_active_conc_window || "—"],
          [
            "Top 3 Init Hours",
            (peaks.top_by_initiated_30d || []).slice(0, 3).map((x) => x.label).join(", ") || "—",
          ],
          ["24h Median Chat Len", fmtDuration(w24.engaged_duration_median_sec)],
          ["30d Median Chat Len", fmtDuration(w30.engaged_duration_median_sec)],
        ],
      },
      {
        accent: "accent-violet",
        title: "AI Assistant & RAG Usage",
        badge: `${fmtNum(ai.gemini_success_rate, 1)}% Gemini`,
        value: fmtInt(ai.total_prompts),
        unit: `prompts (${fmtInt(ai.unique_sessions_total)} sessions)`,
        subs: [
          ["24h Prompts / Sessions", `${fmtInt(ai.prompts_24h)} / ${fmtInt(ai.unique_sessions_24h)}`],
          ["30d Prompts / Sessions", `${fmtInt(ai.prompts_30d)} / ${fmtInt(ai.unique_sessions_30d)}`],
          ["Mean / Med Prompts", `${fmtNum(ai.mean_prompts_per_session, 2)} / ${fmtNum(ai.median_prompts_per_session, 1)}`],
          ["Median Latency", `${fmtNum(ai.median_latency_ms, 0)} ms (mean ${fmtNum(ai.mean_latency_ms, 0)})`],
        ],
      },
      {
        accent: "accent-emerald",
        title: "AUA Ecosystem Scale",
        badge: "Bot + Web",
        value: fmtInt(bot.total_users),
        unit: `bot users (${fmtInt(webUsers.total)} web accounts)`,
        subs: [
          ["Bot Verified Users", fmtInt(bot.total_verified)],
          ["Web Bot-Synced", fmtInt(webUsers.synced_with_bot)],
          ["Campus Chat Msgs", fmtInt(chatMod.total_messages)],
          ["Course Reviews", `${fmtInt(fbMod.total)} (avg ${fmtNum(fbMod.avg_rating, 2)}★)`],
        ],
      },
    ];

    $("kpiGrid").innerHTML = cards
      .map(
        (c) => `
      <article class="kpi-card ${c.accent}">
        <div class="kpi-label">
          <span>${escapeHtml(c.title)}</span>
          <span class="pill pill-muted">${escapeHtml(c.badge)}</span>
        </div>
        <div class="kpi-main-row">
          <div class="kpi-value">${escapeHtml(c.value)}</div>
          <div class="kpi-unit">${escapeHtml(c.unit)}</div>
        </div>
        <div class="kpi-sub-grid">
          ${c.subs
            .map(
              ([lbl, val]) => `
            <div class="sub-stat">
              <span>${escapeHtml(lbl)}</span>
              <strong>${escapeHtml(val)}</strong>
            </div>`
            )
            .join("")}
        </div>
      </article>`
      )
      .join("");
  }

  function renderAnonComparisonTable(adv) {
    const w24 = adv.window_24h || {};
    const w30 = adv.window_30d || {};
    const wAll = adv.window_all_time || {};

    const rows = [
      [
        "Active Sessions Concurrency Mean",
        fmtNum(w24.concurrent_mean, 2),
        fmtNum(w30.concurrent_mean, 2),
        fmtNum(wAll.concurrent_mean, 2),
      ],
      [
        "Active Sessions Concurrency Median",
        fmtNum(w24.concurrent_median, 2),
        fmtNum(w30.concurrent_median, 2),
        fmtNum(wAll.concurrent_median, 2),
      ],
      [
        "Peak Concurrent Active Sessions",
        fmtInt(w24.concurrent_peak),
        fmtInt(w30.concurrent_peak),
        fmtInt(wAll.concurrent_peak),
      ],
      [
        "Chats Initiated Total",
        fmtInt(w24.initiated),
        fmtInt(w30.initiated),
        fmtInt(wAll.initiated),
      ],
      [
        "Chats Initiated Daily Mean / Median",
        `${fmtInt(w24.initiated)} / day`,
        `${fmtNum(w30.chats_initiated_daily_mean, 2)} / ${fmtNum(w30.chats_initiated_daily_median, 1)}`,
        "—",
      ],
      [
        "Engaged Chats (>0 msgs) & Rate",
        `${fmtInt(w24.engaged)} (${fmtNum(w24.engagement_rate, 1)}%)`,
        `${fmtInt(w30.engaged)} (${fmtNum(w30.engagement_rate, 1)}%)`,
        `${fmtInt(wAll.engaged)} (${fmtNum(wAll.engagement_rate, 1)}%)`,
      ],
      [
        "Anonymous Messages Relayed Total",
        fmtInt(w24.messages_total),
        fmtInt(w30.messages_total),
        fmtInt(wAll.messages_total),
      ],
      [
        "Messages per Chat (Mean / Median)",
        `${fmtNum(w24.messages_mean, 2)} / ${fmtNum(w24.messages_median, 1)}`,
        `${fmtNum(w30.messages_mean, 2)} / ${fmtNum(w30.messages_median, 1)}`,
        `${fmtNum(wAll.messages_mean, 2)} / ${fmtNum(wAll.messages_median, 1)}`,
      ],
      [
        "All Sessions Duration (Mean / Median)",
        `${fmtDuration(w24.duration_mean_sec)} / ${fmtDuration(w24.duration_median_sec)}`,
        `${fmtDuration(w30.duration_mean_sec)} / ${fmtDuration(w30.duration_median_sec)}`,
        `${fmtDuration(wAll.duration_mean_sec)} / ${fmtDuration(wAll.duration_median_sec)}`,
      ],
      [
        "Engaged Sessions Duration Median",
        fmtDuration(w24.engaged_duration_median_sec),
        fmtDuration(w30.engaged_duration_median_sec),
        fmtDuration(wAll.engaged_duration_median_sec),
      ],
    ];

    const tbody = $("anonComparisonTable").querySelector("tbody");
    tbody.innerHTML = rows
      .map(
        (r) => `
      <tr>
        <td>${escapeHtml(r[0])}</td>
        <td class="num">${escapeHtml(r[1])}</td>
        <td class="num">${escapeHtml(r[2])}</td>
        <td class="num">${escapeHtml(r[3])}</td>
      </tr>`
      )
      .join("");
  }

  function renderAnonWindowCharts(adv) {
    if (anonWindow === "24h") {
      const curve = adv.concurrency_24h_curve || [];
      const w24 = adv.window_24h || {};
      const meanVal = Number(w24.concurrent_mean ?? 0);
      const medVal = Number(w24.concurrent_median ?? 0);

      $("concTrendTitle").textContent = "24h Concurrency Curve (15-min Slots, AMT)";
      $("concTrendSub").textContent = `Mean ${fmtNum(meanVal, 2)} · Median ${fmtNum(medVal, 2)} · Peak ${fmtInt(w24.concurrent_peak)}`;

      renderChart("chartAnonConcOrDaily", {
        type: "line",
        data: {
          labels: curve.map((p) => p.label),
          datasets: [
            {
              label: "Active Sessions",
              data: curve.map((p) => p.active_sessions),
              borderColor: "#38bdf8",
              backgroundColor: "rgba(56, 189, 248, 0.16)",
              borderWidth: 2,
              pointRadius: 0,
              pointHoverRadius: 4,
              fill: true,
              tension: 0.28,
            },
            {
              label: `24h Mean (${fmtNum(meanVal, 2)})`,
              data: curve.map(() => meanVal),
              borderColor: "#fbbf24",
              borderDash: [6, 4],
              borderWidth: 1.5,
              pointRadius: 0,
              fill: false,
            },
            {
              label: `24h Median (${fmtNum(medVal, 2)})`,
              data: curve.map(() => medVal),
              borderColor: "#34d399",
              borderDash: [3, 3],
              borderWidth: 1.5,
              pointRadius: 0,
              fill: false,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { ticks: { maxTicksLimit: 12 } },
            y: { beginAtZero: true },
          },
        },
      });

      const rolling = adv.rolling_24h_series || [];
      $("initTrendTitle").textContent = "Rolling 24h Chats Initiated & Hourly Concurrency";
      $("initTrendSub").textContent = "Bars show chats initiated per hour; lines track concurrency mean/median";

      renderChart("chartAnonInitTrend", {
        type: "bar",
        data: {
          labels: rolling.map((r) => r.label),
          datasets: [
            {
              type: "bar",
              label: "Chats Initiated",
              data: rolling.map((r) => r.chats_initiated),
              backgroundColor: "rgba(56, 189, 248, 0.45)",
              borderColor: "#38bdf8",
              borderWidth: 1,
              borderRadius: 4,
              yAxisID: "y",
            },
            {
              type: "line",
              label: "Concurrent Mean",
              data: rolling.map((r) => r.concurrent_mean),
              borderColor: "#fbbf24",
              backgroundColor: "#fbbf24",
              borderWidth: 2,
              pointRadius: 2,
              tension: 0.3,
              yAxisID: "y1",
            },
            {
              type: "line",
              label: "Concurrent Median",
              data: rolling.map((r) => r.concurrent_median),
              borderColor: "#34d399",
              backgroundColor: "#34d399",
              borderWidth: 2,
              pointRadius: 2,
              tension: 0.3,
              yAxisID: "y1",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            y: { beginAtZero: true, position: "left", title: { display: true, text: "Initiated" } },
            y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "Concurrent" } },
          },
        },
      });
    } else {
      const daily = adv.daily_series_30d || [];
      const w30 = adv.window_30d || {};

      $("concTrendTitle").textContent = "30-Day Concurrency Mean, Median & Peak";
      $("concTrendSub").textContent = `30d Mean ${fmtNum(w30.concurrent_mean, 2)} · Median ${fmtNum(w30.concurrent_median, 2)} · Peak ${fmtInt(w30.concurrent_peak)}`;

      renderChart("chartAnonConcOrDaily", {
        type: "line",
        data: {
          labels: daily.map((d) => d.label),
          datasets: [
            {
              label: "Concurrent Mean",
              data: daily.map((d) => d.concurrent_mean),
              borderColor: "#fbbf24",
              backgroundColor: "rgba(251, 191, 36, 0.12)",
              borderWidth: 2,
              pointRadius: 2,
              fill: true,
              tension: 0.3,
            },
            {
              label: "Concurrent Median",
              data: daily.map((d) => d.concurrent_median),
              borderColor: "#34d399",
              borderWidth: 2,
              pointRadius: 2,
              tension: 0.3,
            },
            {
              label: "Daily Peak",
              data: daily.map((d) => d.concurrent_peak),
              borderColor: "#38bdf8",
              borderDash: [5, 4],
              borderWidth: 1.7,
              pointRadius: 1.5,
              tension: 0.25,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { ticks: { maxTicksLimit: 10 } },
            y: { beginAtZero: true },
          },
        },
      });

      $("initTrendTitle").textContent = "30-Day Chats Initiated, Engaged & New Bot Users";
      $("initTrendSub").textContent = `30d Daily Mean ${fmtNum(w30.chats_initiated_daily_mean, 1)} · Median ${fmtNum(w30.chats_initiated_daily_median, 1)} chats/day`;

      renderChart("chartAnonInitTrend", {
        type: "bar",
        data: {
          labels: daily.map((d) => d.label),
          datasets: [
            {
              type: "bar",
              label: "Chats Initiated",
              data: daily.map((d) => d.chats_initiated),
              backgroundColor: "rgba(56, 189, 248, 0.55)",
              borderRadius: 4,
              yAxisID: "y",
            },
            {
              type: "bar",
              label: "Engaged (>0 msgs)",
              data: daily.map((d) => d.engaged_chats),
              backgroundColor: "rgba(52, 211, 153, 0.55)",
              borderRadius: 4,
              yAxisID: "y",
            },
            {
              type: "line",
              label: "New Bot Users",
              data: daily.map((d) => d.new_bot_users),
              borderColor: "#a78bfa",
              borderWidth: 2,
              pointRadius: 2,
              tension: 0.3,
              yAxisID: "y1",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { ticks: { maxTicksLimit: 10 } },
            y: { beginAtZero: true, position: "left" },
            y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false } },
          },
        },
      });
    }
  }

  function renderHourlyAndPeaks(adv, live) {
    const profile = adv.hourly_profile || [];
    const peaks = adv.peak_hours || {};

    $("peakHoursBadges").innerHTML = `
      <span class="pill pill-ice">Peak Initiated: ${escapeHtml(peaks.most_active_init_window || "—")}</span>
      <span class="pill pill-emerald">Peak Concurrency: ${escapeHtml(peaks.most_active_conc_window || "—")}</span>
    `;

    renderChart("chartHourlyProfile", {
      type: "bar",
      data: {
        labels: profile.map((h) => h.label),
        datasets: [
          {
            type: "bar",
            label: "Chats Initiated (30d)",
            data: profile.map((h) => h.initiated_30d),
            backgroundColor: "rgba(56, 189, 248, 0.52)",
            borderColor: "#38bdf8",
            borderWidth: 1,
            borderRadius: 4,
            yAxisID: "y",
          },
          {
            type: "line",
            label: "Mean Concurrent (30d)",
            data: profile.map((h) => h.avg_concurrent_30d),
            borderColor: "#fbbf24",
            backgroundColor: "#fbbf24",
            borderWidth: 2.2,
            pointRadius: 2.5,
            tension: 0.3,
            yAxisID: "y1",
          },
          {
            type: "line",
            label: "Median Concurrent (30d)",
            data: profile.map((h) => h.median_concurrent_30d),
            borderColor: "#34d399",
            borderWidth: 2,
            borderDash: [4, 3],
            pointRadius: 2,
            tension: 0.3,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { beginAtZero: true, position: "left", title: { display: true, text: "Initiated (30d)" } },
          y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "Concurrent Sessions" } },
        },
      },
    });

    const topInit = peaks.top_by_initiated_30d || [];
    const topConc = peaks.top_by_concurrency_30d || [];
    $("peakLeaderboard").innerHTML = `
      <div class="rank-col">
        <h4>Top Hours by Chats Initiated</h4>
        ${topInit
          .map(
            (item, idx) => `
          <div class="rank-item">
            <div class="r-left">
              <span class="rank-num">${idx + 1}</span>
              <span class="rank-window">${escapeHtml(item.window_label)}</span>
            </div>
            <span class="rank-val">${fmtInt(item.initiated_30d)} chats</span>
          </div>`
          )
          .join("")}
      </div>
      <div class="rank-col">
        <h4>Top Hours by Mean Concurrency</h4>
        ${topConc
          .map(
            (item, idx) => `
          <div class="rank-item">
            <div class="r-left">
              <span class="rank-num">${idx + 1}</span>
              <span class="rank-window">${escapeHtml(item.window_label)}</span>
            </div>
            <span class="rank-val">${fmtNum(item.avg_concurrent_30d, 2)} avg (${fmtNum(item.median_concurrent_30d, 1)} med)</span>
          </div>`
          )
          .join("")}
      </div>
    `;

    // Live Snapshots
    const snaps = live.snapshots_24h || [];
    const emptyNote = $("snapshotEmptyNote");
    if (snaps.length < 2) {
      emptyNote.classList.remove("hidden");
    } else {
      emptyNote.classList.add("hidden");
    }
    $("snapshotSub").textContent = `${fmtInt(live.snapshots_24h_count)} samples in last 24h · Queue mean ${fmtNum(live.queue_mean_24h, 2)} · WS mean ${fmtNum(live.ws_clients_mean_24h, 2)}`;

    renderChart("chartLiveSnapshots", {
      type: "line",
      data: {
        labels: snaps.length ? snaps.map((s) => s.time) : ["Now"],
        datasets: [
          {
            label: "Active Chats",
            data: snaps.length ? snaps.map((s) => s.active_chats) : [live.active_sessions_now || 0],
            borderColor: "#38bdf8",
            backgroundColor: "rgba(56, 189, 248, 0.14)",
            borderWidth: 2,
            pointRadius: snaps.length > 40 ? 0 : 2.5,
            fill: true,
            tension: 0.25,
          },
          {
            label: "Searching Queue",
            data: snaps.length ? snaps.map((s) => s.searching_users) : [0],
            borderColor: "#fbbf24",
            borderWidth: 2,
            pointRadius: snaps.length > 40 ? 0 : 2.5,
            tension: 0.25,
          },
          {
            label: "Web WS Clients",
            data: snaps.length ? snaps.map((s) => s.web_ws_clients) : [live.web_ws_clients || 0],
            borderColor: "#a78bfa",
            borderWidth: 1.8,
            pointRadius: snaps.length > 40 ? 0 : 2.5,
            tension: 0.25,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { ticks: { maxTicksLimit: 10 } },
          y: { beginAtZero: true },
        },
      },
    });
  }

  function renderHeatmap(adv) {
    const hm = adv.heatmap_7x24 || {};
    const days = hm.days || [];
    const hours = hm.hours || [];
    const matrix = hm.matrix || [];

    let maxVal = 0;
    for (const row of matrix) {
      for (const v of row) {
        if (v > maxVal) maxVal = v;
      }
    }

    let html = `<div class="hm-grid"><div></div>`;
    for (const h of hours) {
      html += `<div class="hm-hour-hdr">${escapeHtml(h)}</div>`;
    }

    days.forEach((day, dIdx) => {
      html += `<div class="hm-day-label">${escapeHtml(day)}</div>`;
      const row = matrix[dIdx] || [];
      hours.forEach((h, hIdx) => {
        const val = Number(row[hIdx] ?? 0);
        const ratio = maxVal > 0 ? val / maxVal : 0;
        let bg = "rgba(15, 23, 42, 0.72)";
        let color = "#64748b";
        if (val > 0) {
          if (ratio >= 0.8) {
            bg = "rgba(52, 211, 153, 0.72)";
            color = "#03150e";
          } else if (ratio >= 0.45) {
            bg = "rgba(56, 189, 248, 0.66)";
            color = "#f8fafc";
          } else {
            const alpha = (0.16 + ratio * 0.45).toFixed(2);
            bg = `rgba(2, 132, 199, ${alpha})`;
            color = "#e0f2fe";
          }
        }
        const title = `${day} ${h}:00–${h}:59 AMT · ${val} chats initiated`;
        html += `<div class="hm-cell" style="background:${bg};color:${color}" title="${escapeHtml(title)}">${val > 0 ? val : ""}</div>`;
      });
    });

    html += `</div>`;
    $("heatmapContainer").innerHTML = html;
  }

  function renderAiSection(ai) {
    const cards = [
      ["Total AI Prompts", fmtInt(ai.total_prompts), `24h: ${fmtInt(ai.prompts_24h)} · 30d: ${fmtInt(ai.prompts_30d)}`],
      ["Unique AI Sessions", fmtInt(ai.unique_sessions_total), `24h: ${fmtInt(ai.unique_sessions_24h)} · Synced DB: ${fmtInt(ai.synced_web_session_records)}`],
      ["Prompts / Session", `${fmtNum(ai.mean_prompts_per_session, 2)} mean`, `Median: ${fmtNum(ai.median_prompts_per_session, 1)} · Hit 5-cap: ${fmtInt(ai.limit_reached_sessions)}`],
      ["Signed-in vs Guest", `${fmtInt(ai.signed_in_prompts)} / ${fmtInt(ai.guest_prompts)}`, `Unique auth users: ${fmtInt(ai.unique_signed_in_users)}`],
      ["Gemini Reliability", `${fmtNum(ai.gemini_success_rate, 1)}%`, `Gemini: ${fmtInt(ai.gemini_success_count)} · Fallback: ${fmtInt(ai.fallback_count)}`],
      ["Response Latency", `${fmtNum(ai.median_latency_ms, 0)} ms med`, `Mean: ${fmtNum(ai.mean_latency_ms, 0)} ms`],
      ["RAG Sources / Prompt", fmtNum(ai.mean_sources_per_prompt, 2), `Avg output: ${fmtNum(ai.mean_answer_chars, 0)} chars`],
      ["Avg Prompt Length", `${fmtNum(ai.mean_prompt_chars, 0)} chars`, `Logged invocations across 6 RAG tools`],
    ];

    $("aiKpiStrip").innerHTML = cards
      .map(
        ([t, v, d]) => `
      <div class="mini-card">
        <div class="m-title">${escapeHtml(t)}</div>
        <div class="m-val">${escapeHtml(v)}</div>
        <div class="m-detail">${escapeHtml(d)}</div>
      </div>`
      )
      .join("");

    const daily = ai.daily_series_30d || [];
    renderChart("chartAiDaily30d", {
      type: "bar",
      data: {
        labels: daily.map((d) => d.label),
        datasets: [
          {
            type: "bar",
            label: "Prompts",
            data: daily.map((d) => d.prompts),
            backgroundColor: "rgba(167, 139, 250, 0.56)",
            borderColor: "#a78bfa",
            borderWidth: 1,
            borderRadius: 4,
          },
          {
            type: "line",
            label: "Sessions",
            data: daily.map((d) => d.sessions),
            borderColor: "#38bdf8",
            borderWidth: 2,
            pointRadius: 2,
            tension: 0.3,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: { x: { ticks: { maxTicksLimit: 10 } }, y: { beginAtZero: true } },
      },
    });

    const tools = ai.tool_breakdown || [];
    renderChart("chartAiTools", {
      type: "bar",
      data: {
        labels: tools.map((t) => t.label),
        datasets: [
          {
            label: "Invocations",
            data: tools.map((t) => t.count),
            backgroundColor: [
              "rgba(56, 189, 248, 0.68)",
              "rgba(52, 211, 153, 0.68)",
              "rgba(167, 139, 250, 0.68)",
              "rgba(251, 191, 36, 0.68)",
              "rgba(244, 114, 182, 0.68)",
              "rgba(96, 165, 250, 0.68)",
            ],
            borderRadius: 6,
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true } },
      },
    });

    const depth = ai.prompt_depth_distribution || [];
    renderChart("chartAiDepth", {
      type: "bar",
      data: {
        labels: depth.map((d) => d.label),
        datasets: [
          {
            label: "Prompts at Turn #",
            data: depth.map((d) => d.count),
            backgroundColor: "rgba(56, 189, 248, 0.62)",
            borderColor: "#7dd3fc",
            borderWidth: 1,
            borderRadius: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true } },
      },
    });

    const hourly = ai.hourly_30d || [];
    renderChart("chartAiHourly", {
      type: "line",
      data: {
        labels: hourly.map((h) => h.label),
        datasets: [
          {
            label: "Prompts (30d)",
            data: hourly.map((h) => h.prompts_30d),
            borderColor: "#a78bfa",
            backgroundColor: "rgba(167, 139, 250, 0.16)",
            borderWidth: 2,
            fill: true,
            tension: 0.3,
          },
          {
            label: "Prompts (24h)",
            data: hourly.map((h) => h.prompts_24h),
            borderColor: "#34d399",
            borderWidth: 2,
            tension: 0.3,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: { y: { beginAtZero: true } },
      },
    });
  }

  function renderBotSection(bot) {
    const cards = [
      ["Total Bot Users", fmtInt(bot.total_users), `Blocked: ${fmtInt(bot.blocked_bot_users)} · Banned: ${fmtInt(bot.banned_count)}`],
      ["Verified Students / Alumni", `${fmtInt(bot.verified_students)} / ${fmtInt(bot.verified_alumni)}`, `Gender verified: ${fmtInt(bot.gender_verified)} · Unverified: ${fmtInt(bot.unverified_count)}`],
      ["Secret Inbox Network", `${fmtInt(bot.total_inbox_messages)} msgs`, `Active links: ${fmtInt(bot.active_inboxes_count)} · Reply rate: ${fmtNum(bot.inbox_reply_rate, 1)}%`],
      ["Chat Invites & Safety", `${fmtInt(bot.used_chat_invites)} / ${fmtInt(bot.total_chat_invites)}`, `Pending reports: ${fmtInt(bot.pending_reports)} · Suggestions: ${fmtInt(bot.pending_suggestions)}`],
    ];

    $("botSummaryGrid").innerHTML = cards
      .map(
        ([t, v, d]) => `
      <div class="mini-card">
        <div class="m-title">${escapeHtml(t)}</div>
        <div class="m-val">${escapeHtml(v)}</div>
        <div class="m-detail">${escapeHtml(d)}</div>
      </div>`
      )
      .join("");

    renderChart("chartBotVerification", {
      type: "bar",
      data: {
        labels: [
          "Verified Students",
          "Verified Alumni",
          "Gender Verified",
          "Unverified",
          "Male",
          "Female",
          "Unset Gender",
          "Bot Blocked",
        ],
        datasets: [
          {
            label: "Users",
            data: [
              bot.verified_students || 0,
              bot.verified_alumni || 0,
              bot.gender_verified || 0,
              bot.unverified_count || 0,
              bot.male_count || 0,
              bot.female_count || 0,
              bot.unset_sex_count || 0,
              bot.blocked_bot_users || 0,
            ],
            backgroundColor: [
              "rgba(52, 211, 153, 0.7)",
              "rgba(56, 189, 248, 0.7)",
              "rgba(125, 211, 252, 0.7)",
              "rgba(251, 191, 36, 0.65)",
              "rgba(96, 165, 250, 0.68)",
              "rgba(244, 114, 182, 0.68)",
              "rgba(148, 163, 184, 0.55)",
              "rgba(248, 113, 113, 0.7)",
            ],
            borderRadius: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true } },
      },
    });

    renderChart("chartBotSafety", {
      type: "bar",
      data: {
        labels: [
          "Partner Likes",
          "Partner Dislikes",
          "Spam Flags",
          "Not-AUA Flags",
          "Inbox Msgs",
          "Inbox Replied",
          "Total Reports",
          "Suggestions",
        ],
        datasets: [
          {
            label: "Count",
            data: [
              bot.likes_count || 0,
              bot.dislikes_count || 0,
              bot.spam_count || 0,
              bot.not_aua_count || 0,
              bot.total_inbox_messages || 0,
              bot.replied_inbox_messages || 0,
              bot.total_reports || 0,
              bot.total_suggestions || 0,
            ],
            backgroundColor: [
              "rgba(52, 211, 153, 0.72)",
              "rgba(251, 191, 36, 0.68)",
              "rgba(248, 113, 113, 0.7)",
              "rgba(251, 146, 60, 0.7)",
              "rgba(56, 189, 248, 0.72)",
              "rgba(167, 139, 250, 0.72)",
              "rgba(239, 68, 68, 0.68)",
              "rgba(148, 163, 184, 0.62)",
            ],
            borderRadius: 6,
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true } },
      },
    });
  }

  function renderWebSection(web) {
    const users = web.users || {};
    const mods = web.modules || {};
    const hn = mods.have_need || {};
    const fb = mods.feedbacks || {};
    const ap = mods.anonymous_posts || {};
    const tg = mods.telegram_groups || {};
    const cc = mods.campus_chat || {};
    const an = web.analytics || {};

    const cards = [
      ["Web Users", fmtInt(users.total), `24h: +${fmtInt(users.new_24h)} · 30d: +${fmtInt(users.new_30d)} · Synced: ${fmtInt(users.synced_with_bot)}`],
      ["Have-Need Course Swaps", `${fmtInt(hn.active)} active`, `Total: ${fmtInt(hn.total)} · Pairs: ${fmtInt(hn.reciprocal_swap_pairs)} · Alerts: ${fmtInt(hn.email_alerts_enabled)}`],
      ["Campus Chat & Groups", fmtInt(cc.total_messages), `24h: ${fmtInt(cc.messages_24h)} · Reactions: ${fmtInt(cc.total_reactions)} · TG Groups: ${fmtInt(tg.total)}`],
      ["Anon Wall & Events", `${fmtInt(ap.visible)} posts`, `Hidden: ${fmtInt(ap.hidden)} (${fmtInt(ap.community_reports)} reports) · Web events: ${fmtInt(an.total_events)}`],
    ];

    $("webModulesGrid").innerHTML = cards
      .map(
        ([t, v, d]) => `
      <div class="mini-card">
        <div class="m-title">${escapeHtml(t)}</div>
        <div class="m-val">${escapeHtml(v)}</div>
        <div class="m-detail">${escapeHtml(d)}</div>
      </div>`
      )
      .join("");

    const syncInfo = fb.sync_status || {};
    const lastSyncStr = syncInfo.last_sync_at ? String(syncInfo.last_sync_at).slice(0, 19).replace("T", " ") : "running";
    $("jenzaSyncSub").textContent = `Total ${fmtInt(fb.total)} (${fmtInt(fb.local_submitted)} local + ${fmtInt(fb.jenzareviews_synced)} JenzaReviews) · ${fmtInt(fb.professors_indexed)} profs · Sync: ${lastSyncStr}`;

    const webDaily = an.daily_series_30d || [];
    renderChart("chartWebDaily30d", {
      type: "line",
      data: {
        labels: webDaily.map((d) => d.label),
        datasets: [
          {
            label: "Web Events",
            data: webDaily.map((d) => d.web_events),
            borderColor: "#38bdf8",
            backgroundColor: "rgba(56, 189, 248, 0.14)",
            borderWidth: 2,
            fill: true,
            tension: 0.3,
            yAxisID: "y",
          },
          {
            label: "Campus Chat Msgs",
            data: webDaily.map((d) => d.campus_chat_messages),
            borderColor: "#34d399",
            borderWidth: 2,
            tension: 0.3,
            yAxisID: "y",
          },
          {
            label: "New Web Accounts",
            data: webDaily.map((d) => d.new_web_users),
            borderColor: "#fbbf24",
            borderWidth: 2,
            tension: 0.3,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { ticks: { maxTicksLimit: 10 } },
          y: { beginAtZero: true, position: "left" },
          y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false } },
        },
      },
    });

    const ratings = fb.rating_distribution || [];
    renderChart("chartRatingDist", {
      type: "bar",
      data: {
        labels: ratings.map((r) => `${r.stars} ★`),
        datasets: [
          {
            label: "Reviews",
            data: ratings.map((r) => r.count),
            backgroundColor: [
              "rgba(248, 113, 113, 0.72)",
              "rgba(251, 146, 60, 0.72)",
              "rgba(251, 191, 36, 0.72)",
              "rgba(56, 189, 248, 0.72)",
              "rgba(52, 211, 153, 0.78)",
            ],
            borderRadius: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true } },
      },
    });

    const topEvents = an.top_events || [];
    $("topEventsTable").querySelector("tbody").innerHTML = topEvents.length
      ? topEvents
          .map(
            (ev) => `
          <tr>
            <td>${escapeHtml(ev.event)}</td>
            <td class="num">${fmtInt(ev.count)}</td>
            <td class="num">${fmtInt(ev.count_24h)}</td>
          </tr>`
          )
          .join("")
      : `<tr><td colspan="3">No tracked web events yet.</td></tr>`;

    const topSearches = an.top_searches || [];
    $("topSearchesTable").querySelector("tbody").innerHTML = topSearches.length
      ? topSearches
          .map(
            (s) => `
          <tr>
            <td>${escapeHtml(s.category)}</td>
            <td>${escapeHtml(s.query)}</td>
            <td class="num">${fmtInt(s.count)}</td>
          </tr>`
          )
          .join("")
      : `<tr><td colspan="3">No search queries recorded yet.</td></tr>`;

    const topTargets = fb.top_targets || [];
    $("topTargetsTable").querySelector("tbody").innerHTML = topTargets.length
      ? topTargets
          .map(
            (t) => `
          <tr>
            <td>${escapeHtml(t.name)}</td>
            <td class="num">${fmtInt(t.count)}</td>
          </tr>`
          )
          .join("")
      : `<tr><td colspan="2">No course reviews yet.</td></tr>`;
  }

  function renderAll(data) {
    dashboardPayload = data;
    $("lastUpdatedAmt").textContent = data.generated_at_amt || data.generated_at || "—";
    $("tzPill").textContent = data.timezone || "Asia/Yerevan (UTC+4)";
    $("loadingBanner").classList.add("hidden");
    $("errorBanner").classList.add("hidden");

    const adv = data.anonymous_chat_advanced || {};
    renderExecutiveKpis(data);
    renderAnonComparisonTable(adv);
    renderAnonWindowCharts(adv);
    renderHourlyAndPeaks(adv, data.live || {});
    renderHeatmap(adv);
    renderAiSection(data.ai_assistant || {});
    renderBotSection(data.bot_overview || {});
    renderWebSection(data.website || {});
  }

  async function fetchStata() {
    try {
      const headers = {};
      const token = localStorage.getItem("aua_hub_token");
      if (token) headers.Authorization = "Bearer " + token;

      const res = await fetch("/api/admin/stata", {
        headers,
        credentials: "same-origin",
      });
      if (res.status === 401 || res.status === 403) {
        window.location.reload();
        return;
      }
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      renderAll(data);
    } catch (err) {
      $("loadingBanner").classList.add("hidden");
      const errEl = $("errorBanner");
      errEl.textContent = `Telemetry fetch failed: ${err.message || err}`;
      errEl.classList.remove("hidden");
    }
  }

  function setupEvents() {
    $("anonWindowSwitch").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn || !btn.dataset.window) return;
      anonWindow = btn.dataset.window;
      for (const b of $("anonWindowSwitch").querySelectorAll(".seg-btn")) {
        b.classList.toggle("active", b === btn);
      }
      if (dashboardPayload) {
        renderAnonWindowCharts(dashboardPayload.anonymous_chat_advanced || {});
      }
    });

    $("refreshBtn").addEventListener("click", () => {
      fetchStata();
    });

    $("exportJsonBtn").addEventListener("click", () => {
      if (!dashboardPayload) return;
      const blob = new Blob([JSON.stringify(dashboardPayload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `aua_hub_stata_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    });

    $("logoutBtn").addEventListener("click", async () => {
      try {
        localStorage.removeItem("aua_hub_token");
        await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      } catch (e) {}
      window.location.href = "/stata";
    });

    const toggle = $("autoRefreshToggle");
    const syncTimer = () => {
      if (autoTimer) {
        clearInterval(autoTimer);
        autoTimer = null;
      }
      if (toggle.checked) {
        autoTimer = setInterval(fetchStata, 30000);
      }
    };
    toggle.addEventListener("change", syncTimer);
    syncTimer();
  }

  document.addEventListener("DOMContentLoaded", () => {
    initChartDefaults();
    setupEvents();
    fetchStata();
  });
})();

