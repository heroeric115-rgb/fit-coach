# FitCoach AI — 個人健康減重智能助理

期末作業專案：以 Claude AI Agent 為核心的健康減重 web app，**支援多人帳號**並部署到 Render。

🔗 **線上 demo**：https://fit-coach-hcih.onrender.com

## 功能

- **多人帳號系統**：使用者名稱 + 4 位 PIN，每人資料完全隔離
- **AI Agent (Tool Use)**：Claude 自主呼叫 4 個工具（個人資料、近期打卡、體重趨勢、Zone 2 心率計算），多步推理後產出個人化建議
- **每日打卡**：睡眠、運動類型 / 時長 / 心率、體重、**腰圍**、主觀感受
- **主動 Nudges**：偵測未打卡、連續休息天數、本週 Zone 2 不足等狀況自動提醒
- **趨勢圖表**：體重 / 腰圍雙軸折線圖（Chart.js）
- **Zone 2 燃脂科學**：Karvonen 公式計算個人化心率區間
- **Agent 推理過程可視化**：每次建議都附 tool calls trace，可展開檢視

## 技術

| 層 | 技術 |
| --- | --- |
| 後端 | Flask 3 + gunicorn |
| AI | Claude Haiku 4.5 + Tool Use API |
| 資料庫 | PostgreSQL（production）/ SQLite（本地 dev fallback） |
| 認證 | Flask session cookie + 4 位 PIN（SHA256 雜湊） |
| 前端 | Vanilla JS + Chart.js + marked |
| 部署 | Render.com（web service + 免費 PostgreSQL） |

## 本機執行

```bash
# 1. 建立 venv 並安裝
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 設定環境變數
export ANTHROPIC_API_KEY='sk-ant-...'
export SECRET_KEY='隨便一串長文字'
# DATABASE_URL 不設 → 自動退回 SQLite（檔案在 data/fitcoach.db）

# 3. 啟動
python app.py
# 開 http://localhost:8000
```

## 部署到 Render（免費，不需信用卡）

### Step 1：建 PostgreSQL

1. Render dashboard → **New +** → **PostgreSQL**
2. Plan 選 **Free**，Region 選離你近的（亞洲推 Singapore）
3. Create → 等狀態變 **Available**（約 30 秒）
4. 點進去找 **Connections** → 複製 **Internal Database URL**（要以 `postgresql://` 開頭）

### Step 2：建 Web Service

1. **New +** → **Web Service** → 連 GitHub repo
2. Render 自動讀 `render.yaml`，build / start 指令自動填好
3. 在 **Environment** 加 3 個變數：
   - `ANTHROPIC_API_KEY` = `sk-ant-...`
   - `SECRET_KEY` = 一串長隨機字（用 `openssl rand -hex 32` 產）
   - `DATABASE_URL` = 剛才複製的 PostgreSQL Internal URL
4. **Create Web Service**
5. 約 2-3 分鐘部署完成

### Step 3：驗證

開 `https://你的網址/debug/db`，看到 `"use_pg": true` 就成功了。

### 注意事項

- **免費 Web Service**：閒置 15 分鐘 sleep，喚醒約 30 秒
- **免費 PostgreSQL**：90 天到期；交完作業前都 OK，之後可升級或匯出到 Neon / Supabase
- **SECRET_KEY**：用於 session cookie 簽章；正式環境**務必**設成隨機字串，不要用預設值

## 專案結構

```
fitcoach/
├── app.py                  # Flask 主程式：路由、Agent loop、DB 抽象層、Tool 定義
├── templates/
│   └── index.html         # 單頁 dashboard + 登入 modal
├── static/
│   ├── app.js            # 前端邏輯（auth、打卡、AI 建議、Chart）
│   └── styles.css        # 樣式
├── data/
│   └── fitcoach.db       # SQLite（本地 dev 自動產生；production 用 Postgres）
├── requirements.txt       # flask, anthropic, gunicorn, psycopg2-binary
├── render.yaml           # Render 部署設定
├── Procfile              # 備用：Heroku/Railway 也能用
└── runtime.txt           # Python 3.11.9
```

## 架構說明（給期末報告用）

### 1. 多人架構

```
users (id, username, pin_hash)
  ├── user_profile (user_id PK, name, age, weight_kg, height_cm, target, resting_hr)
  ├── daily_logs (user_id FK, log_date, sleep_*, exercise_*, weight_kg, waist_cm, …)
  └── recommendations (user_id FK, rec_date, content, tool_calls_json)
```

- 每張資料表（除 users）都有 `user_id` 外鍵，`ON DELETE CASCADE`
- `daily_logs` 用 `UNIQUE(user_id, log_date)` 確保每人每日只一筆，再次打卡會 UPSERT
- 所有 API endpoint 用 `@require_auth` decorator 強制檢查 session
- Agent 工具也鎖在當前 user：`make_tool_runner(uid)` 用 closure 確保 Claude 只看得到登入者的資料

### 2. DB 抽象層

`app.py` 偵測 `DATABASE_URL` 環境變數：
- 設定且以 `postgres://` / `postgresql://` 開頭 → 用 psycopg2 接 PostgreSQL
- 否則 → 退回 SQLite（本地開發無痛）

透過 `PgWrapper` 把 psycopg2 包成類 sqlite3 介面（`?` placeholder 自動翻成 `%s`、回傳 RealDictCursor），讓所有 CRUD 程式碼**兩邊共用**。

### 3. AI Agent 設計（Tool Use Loop）

FitCoach 採用 **Claude Tool Use API** 實作真正的 multi-step agent：

1. 使用者進入頁面或按「重新分析」→ 觸發 `/api/recommend`
2. 後端呼叫 Claude，附上 system prompt（運動科學顧問身分 + 最新運動科學共識）和工具定義
3. Claude **自主決定**呼叫哪些工具、查幾天：
   - `get_user_profile` — 取年齡、目標、體重、靜止心率
   - `get_recent_logs(days)` — 查 N 天打卡
   - `get_weight_waist_trend(days)` — 取體重 / 腰圍序列
   - `calculate_zone2_hr(age, resting_hr)` — Karvonen 公式
4. 後端執行工具回傳結果（鎖在當前 user_id），Claude 收到後繼續推理或再呼叫其他工具
5. 直到 `stop_reason="end_turn"`，最終輸出 Markdown 建議
6. 前端用 marked 渲染，並把整個工具呼叫過程存進 `recommendations.tool_calls_json` 供展開檢視

**典型 trace**（實測）：
```
get_user_profile → get_recent_logs(days=14) → get_weight_waist_trend(days=30) → calculate_zone2_hr
```
四個工具，一次 API 任務內全自動完成，無需人工指定順序。

## 健康減重科學依據（給報告用）

- **Zone 2 訓練**（60-70% HRmax）：粒線體生合成最強區間，脂肪氧化效率最高（Inigo San-Millan, 2023）
- **每週 150–180 min Zone 2**：心血管基礎與燃脂的最佳累積量
- **睡眠 < 6 h**：皮質醇升高 37%，干擾瘦素 / 飢餓素平衡，當天應降強度
- **腰圍 > 體重**：腰圍直接反映內臟脂肪（visceral fat），是心血管風險更佳的指標

## 試用方式

1. 開 https://fit-coach-hcih.onrender.com
2. 取個名字（如 `Eric`）+ 4 位 PIN（自選）→ 進入
3. 「設定」分頁填基本資料 → AI 才能算 Zone 2 心率
4. 「打卡」分頁記今天的睡眠、運動、體重、腰圍
5. 回「今日」看 AI 給的個人化建議
6. 朋友各自登入（不同名字 + PIN）→ 資料互不干擾
