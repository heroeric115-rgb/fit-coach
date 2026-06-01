// FitCoach AI — 前端邏輯

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let trendChart = null;
let cachedProfile = null;

// ── Tab 切換 ─────────────────────────────────────────────
function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
}
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});

// ── 通用 fetch ───────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── 個人資料 ─────────────────────────────────────────────
async function loadProfile() {
  const p = await api("/api/profile");
  cachedProfile = p;
  if (!p || !p.name) {
    $("#profileBadge").textContent = "⚠ 尚未設定資料";
    return null;
  }
  $("#profileBadge").textContent = `👤 ${p.name}・目標 ${p.target_weight_kg} kg`;

  // 填入表單
  const form = $("#profileForm");
  ["name", "age", "gender", "weight_kg", "height_cm", "target_weight_kg", "resting_hr"].forEach(
    (k) => {
      if (form[k] && p[k] != null) form[k].value = p[k];
    }
  );

  // 摘要卡
  const summary = $("#profileSummary");
  summary.innerHTML = `
    <div class="kv"><span>Zone 2 目標心率</span><b>${p.zone2_low}–${p.zone2_high} bpm</b></div>
    <div class="kv"><span>BMI</span><b>${p.bmi ?? "—"}</b></div>
    <div class="kv"><span>距離目標</span><b>${(p.weight_kg - p.target_weight_kg).toFixed(1)} kg</b></div>
  `;
  return p;
}

$("#profileForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const data = Object.fromEntries(fd.entries());
  ["age", "resting_hr"].forEach((k) => (data[k] = parseInt(data[k]) || null));
  ["weight_kg", "height_cm", "target_weight_kg"].forEach(
    (k) => (data[k] = parseFloat(data[k]) || null)
  );
  await api("/api/profile", { method: "POST", body: JSON.stringify(data) });
  alert("✅ 已儲存");
  await loadProfile();
  await loadStats();
});

// ── 今日打卡 ─────────────────────────────────────────────
$("#todayLabel").textContent = new Date().toLocaleDateString("zh-TW");

$("#logForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const d = Object.fromEntries(fd.entries());
  // 整理型別
  const ints = ["sleep_quality", "exercise_minutes", "avg_heart_rate", "max_heart_rate",
                "calories_burned", "water_ml", "energy_level", "soreness"];
  const floats = ["sleep_hours", "deep_sleep_hours", "weight_kg", "waist_cm"];
  ints.forEach((k) => (d[k] = d[k] === "" ? null : parseInt(d[k])));
  floats.forEach((k) => (d[k] = d[k] === "" ? null : parseFloat(d[k])));
  if (!d.notes) d.notes = null;

  await api("/api/logs", { method: "POST", body: JSON.stringify(d) });
  alert("✅ 已打卡！正在重新分析…");
  await Promise.all([loadStats(), loadHistory(), loadTrend()]);
  await loadAdvice(true); // 重新請 AI 分析
  switchTab("today");
});

// ── 統計 + Nudges ────────────────────────────────────────
async function loadStats() {
  const s = await api("/api/stats");

  // 今日打卡狀態
  const today = new Date().toLocaleDateString("zh-TW");
  $("#todayStatus").innerHTML = s.has_today_log
    ? `<div class="status ok">✅ 今日（${today}）已打卡</div>
       <div class="status muted">連續打卡 ${s.log_streak} 天</div>`
    : `<div class="status warn">⚠ 今日（${today}）尚未打卡</div>
       <div class="status muted">${
         s.days_since_last_log == null ? "尚無記錄" : `距離上次打卡 ${s.days_since_last_log} 天`
       }</div>`;

  $("#statZone2").innerHTML = `${s.weekly_zone2_minutes} <span class="unit">min</span>`;
  $("#statZone2").parentElement.classList.toggle("good", s.weekly_zone2_minutes >= 150);

  const wd = s.weight_change_7d;
  $("#statWeightDelta").textContent = wd == null ? "—" : `${wd > 0 ? "+" : ""}${wd} kg`;
  $("#statWeightDelta").className = "stat-value " + (wd == null ? "" : wd < 0 ? "good" : wd > 0 ? "bad" : "");
  $("#statWeightSub").textContent =
    wd == null ? "需至少 2 筆體重" : wd < -0.2 ? "穩定下降中 👍" : wd > 0.2 ? "本週上升" : "平台期";

  const xd = s.waist_change_7d;
  $("#statWaistDelta").textContent = xd == null ? "—" : `${xd > 0 ? "+" : ""}${xd} cm`;
  $("#statWaistDelta").className = "stat-value " + (xd == null ? "" : xd < 0 ? "good" : xd > 0 ? "bad" : "");

  renderNudges(s);
}

function renderNudges(s) {
  const nudges = [];
  if (!cachedProfile || !cachedProfile.name) {
    nudges.push({ level: "info", text: "👋 第一次使用？先到「設定」填個人資料，AI 才能給你建議。" });
  }
  if (!s.has_today_log && s.days_since_last_log !== null) {
    nudges.push({
      level: "warn",
      text: `📌 你已經 ${s.days_since_last_log + 1} 天沒打卡了！記錄才能讓 AI 看見趨勢。`,
    });
  }
  if (s.consecutive_rest_days >= 3) {
    nudges.push({
      level: "warn",
      text: `🛌 你連續休息 ${s.consecutive_rest_days} 天了，今天來個 30 分鐘 Zone 2 喚醒身體吧。`,
    });
  }
  if (s.weekly_zone2_minutes > 0 && s.weekly_zone2_minutes < 150) {
    nudges.push({
      level: "info",
      text: `💚 本週 Zone 2 累積 ${s.weekly_zone2_minutes} 分鐘，距離 150 min 燃脂門檻還差 ${150 - s.weekly_zone2_minutes} 分鐘。`,
    });
  }
  if (s.log_streak >= 7) {
    nudges.push({ level: "ok", text: `🔥 連續打卡 ${s.log_streak} 天！習慣正在養成。` });
  }

  $("#nudges").innerHTML = nudges
    .map((n) => `<div class="nudge nudge-${n.level}">${n.text}</div>`)
    .join("");
}

// ── 歷史 ─────────────────────────────────────────────────
async function loadHistory() {
  const logs = await api("/api/logs?days=30");
  const tbody = $("#historyTable tbody");
  if (!logs.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty">尚無打卡記錄</td></tr>`;
    return;
  }
  const typeLabel = { zone2: "Zone 2", hiit: "HIIT", strength: "重訓", rest: "休息", other: "其他" };
  tbody.innerHTML = logs
    .map(
      (l) => `
    <tr>
      <td>${l.log_date}</td>
      <td>${l.sleep_hours ?? "—"} h</td>
      <td>${l.sleep_quality ?? "—"}/10</td>
      <td>${typeLabel[l.exercise_type] || "—"}</td>
      <td>${l.exercise_minutes ?? "—"}</td>
      <td>${l.avg_heart_rate ?? "—"}</td>
      <td>${l.weight_kg?.toFixed(1) ?? "—"}</td>
      <td>${l.waist_cm?.toFixed(1) ?? "—"}</td>
      <td>${l.energy_level ?? "—"}/10</td>
    </tr>`
    )
    .join("");
}

// ── 趨勢圖 ───────────────────────────────────────────────
async function loadTrend() {
  const logs = (await api("/api/logs?days=30")).slice().reverse();
  const labels = logs.map((l) => l.log_date.slice(5));
  const weights = logs.map((l) => l.weight_kg);
  const waists = logs.map((l) => l.waist_cm);

  if (trendChart) trendChart.destroy();
  trendChart = new Chart($("#trendChart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "體重 (kg)",
          data: weights,
          yAxisID: "y",
          borderColor: "#ff6b6b",
          backgroundColor: "rgba(255,107,107,0.1)",
          tension: 0.3,
          spanGaps: true,
        },
        {
          label: "腰圍 (cm)",
          data: waists,
          yAxisID: "y1",
          borderColor: "#4dabf7",
          backgroundColor: "rgba(77,171,247,0.1)",
          tension: 0.3,
          spanGaps: true,
        },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { position: "left", title: { display: true, text: "體重 kg" } },
        y1: { position: "right", title: { display: true, text: "腰圍 cm" }, grid: { drawOnChartArea: false } },
      },
    },
  });
}

// ── AI 建議 ──────────────────────────────────────────────
async function loadAdvice(forceNew = false) {
  const adviceEl = $("#adviceContent");
  const traceEl = $("#agentTrace");
  const traceContent = $("#agentTraceContent");

  // 先嘗試載入今日已有建議
  if (!forceNew) {
    const latest = await api("/api/recommend/latest");
    if (latest && latest.rec_date === new Date().toISOString().slice(0, 10)) {
      adviceEl.innerHTML = marked.parse(latest.content);
      if (latest.tool_calls_json) {
        traceEl.hidden = false;
        traceContent.textContent = JSON.stringify(JSON.parse(latest.tool_calls_json), null, 2);
      }
      return;
    }
  }

  // 沒有就請 AI 產生
  adviceEl.innerHTML = `<div class="placeholder">
    <div class="spinner"></div>
    <p>🧠 AI 教練分析中…<br/><small>Claude 正在呼叫工具查詢你的資料</small></p>
  </div>`;

  try {
    const r = await api("/api/recommend", { method: "POST" });
    adviceEl.innerHTML = marked.parse(r.content);
    traceEl.hidden = false;
    traceContent.textContent = JSON.stringify(r.tool_calls, null, 2);
  } catch (e) {
    adviceEl.innerHTML = `<div class="error">❌ ${e.message}<br/><small>請先在「設定」填寫個人資料，並至少打卡一天。</small></div>`;
  }
}

$("#refreshAdviceBtn").addEventListener("click", () => loadAdvice(true));

// ── 初始化 ───────────────────────────────────────────────
window.switchTab = switchTab;

(async function init() {
  await loadProfile();
  await loadStats();
  await loadHistory();
  await loadTrend();
  if (cachedProfile && cachedProfile.name) {
    await loadAdvice(false);
  } else {
    $("#adviceContent").innerHTML = `<div class="placeholder">
      <p>👋 嗨！我是你的 AI 健身教練。<br/>請先到「設定」分頁填寫個人資料，我才能為你量身打造每日訓練。</p>
      <button class="btn primary" onclick="switchTab('profile')">前往設定</button>
    </div>`;
  }
})();
