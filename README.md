# FitCoach AI — 個人健康減重智能助理

期末作業專案：以 Claude AI Agent 為核心的健康減重 web app。

## 功能

- **AI Agent (Tool Use)**：Claude 自主呼叫工具查詢個人資料、近期打卡、體重趨勢、計算 Zone 2 心率，多步推理後產出個人化建議
- **每日打卡**：睡眠、運動、心率、體重、**腰圍**、主觀感受
- **主動 Nudges**：偵測未打卡、連續休息、本週 Zone 2 不足等狀況主動提醒
- **趨勢圖表**：體重 / 腰圍雙軸折線圖（Chart.js）
- **Zone 2 燃脂科學**：Karvonen 公式計算個人化心率區間
- **Agent 推理過程可視化**：每次建議都附 tool calls trace

## 技術

| 層 | 技術 |
| --- | --- |
| 後端 | Flask 3 |
| AI | Claude Haiku 4.5 + Tool Use |
| 資料庫 | SQLite |
| 前端 | Vanilla JS + Chart.js + marked |
| 部署 | Render.com（gunicorn） |

## 本機執行

```bash
# 1. 建立 venv 並安裝
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 設定 API key
export ANTHROPIC_API_KEY='sk-ant-...'

# 3. 啟動
python app.py
# 開 http://localhost:8000
```

## 部署到 Render（免費，不需信用卡）

### Step 1：準備 git repo

```bash
cd /Users/eric/Desktop/CLAUDE/fitcoach
git init
git add .
git commit -m "FitCoach AI initial"
```

把專案推到 GitHub（在 github.com 新建 repo 後）：

```bash
git remote add origin git@github.com:你的帳號/fitcoach.git
git branch -M main
git push -u origin main
```

### Step 2：Render 部署

1. 到 https://render.com 註冊（用 GitHub 登入最快）
2. 點 **New +** → **Web Service**
3. 選擇剛推的 fitcoach repo
4. Render 會自動讀取 `render.yaml`，設定都填好了
5. 在 **Environment** 加入環境變數：
   - `ANTHROPIC_API_KEY` = `sk-ant-...`（你的 key）
6. 點 **Create Web Service**
7. 約 2-3 分鐘部署完成，會給你一個 `https://fitcoach-ai-xxxx.onrender.com` 網址

### 注意事項

- **免費方案**：閒置 15 分鐘後會 sleep，下次喚醒約需 30 秒
- **資料庫**：免費方案磁碟為 ephemeral（redeploy 會清空 DB），demo 足夠
- **想保留資料**：升級到付費方案 + 加 persistent disk，或改用 PostgreSQL（render 也有免費 Postgres）

## 專案結構

```
fitcoach/
├── app.py                  # Flask 主程式：路由、Agent loop、DB、Tool定義
├── templates/
│   └── index.html         # 單頁 dashboard
├── static/
│   ├── app.js            # 前端邏輯
│   └── styles.css        # 樣式
├── data/
│   └── fitcoach.db       # SQLite（自動產生）
├── requirements.txt
├── render.yaml           # Render 部署設定
├── Procfile              # 備用：Heroku/Railway 也能用
└── runtime.txt           # Python 版本
```

## AI Agent 設計（給報告用）

FitCoach 採用 **Claude Tool Use API** 實作真正的 multi-step agent：

1. 使用者進入頁面或按「重新分析」→ 觸發 `/api/recommend`
2. Claude 收到 system prompt（身分：運動科學顧問）+ user message（要分析今日狀況）
3. Claude **自主決定**呼叫哪些工具：
   - `get_user_profile` — 取得年齡、目標、體重
   - `get_recent_logs(days)` — 查 N 天打卡
   - `get_weight_waist_trend(days)` — 取趨勢序列
   - `calculate_zone2_hr(age, resting_hr)` — Karvonen 公式
4. 後端執行工具回傳結果，Claude 繼續迭代直到 `stop_reason="end_turn"`
5. 最終輸出 Markdown 建議，前端用 marked 渲染

**每一次推理過程都記錄在 `recommendations.tool_calls_json`**，前端展開可看 — 這是 agent 的「可解釋性」demo 重點。

## 健康減重科學依據（給報告用）

- **Zone 2 訓練**（60-70% HRmax）：粒線體生合成最強區間，脂肪氧化效率最高（Inigo San-Millan 2023）
- **每週 150-180 min Zone 2**：心血管基礎與燃脂的最佳累積量
- **睡眠 < 6h**：皮質醇升高 37%，干擾瘦素/飢餓素平衡，當天應降強度
- **腰圍 > 體重**：腰圍直接反映內臟脂肪（visceral fat），是心血管風險更佳的指標
