"""
FitCoach AI — 個人健康減重智能助理（Web 版・多人版）
- Flask 後端 + session-based auth（username + 4 位 PIN）
- SQLite，每位使用者獨立 profile / 打卡 / AI 建議
- Claude API + Tool Use（真正的 AI Agent，多步推理 + 工具呼叫）
- Zone 2 心率燃脂、睡眠恢復、體重/腰圍追蹤
"""
from __future__ import annotations

import os
import json
import hashlib
import sqlite3
import secrets
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, session, render_template
import anthropic

# 偵測是否使用 Postgres（DATABASE_URL 由 Render 自動注入）
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
if USE_PG:
    import psycopg2
    from psycopg2.extras import RealDictCursor

# ── 設定 ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "fitcoach.db"

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-fitcoach-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


# ── 資料庫抽象層 ──────────────────────────────────────────────────────────────
class PgWrapper:
    """讓 Postgres 連線提供類 sqlite3 介面（? placeholder、row_factory dict）。"""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executescript(self, sql):
        # PG 不支援 executescript，逐行執行
        cur = self._conn.cursor()
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()


@contextmanager
def db():
    if USE_PG:
        # Render/Heroku 用 postgres://，psycopg2 v3+ 要 postgresql://
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url)
        wrapper = PgWrapper(conn)
        try:
            yield wrapper
            wrapper.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            wrapper.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    pin_hash TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_profile (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    name TEXT, age INTEGER, gender TEXT,
    weight_kg REAL, height_cm REAL, target_weight_kg REAL,
    resting_hr INTEGER DEFAULT 60,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS daily_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    log_date TEXT NOT NULL,
    sleep_hours REAL, sleep_quality INTEGER, deep_sleep_hours REAL,
    exercise_type TEXT, exercise_minutes INTEGER,
    avg_heart_rate INTEGER, max_heart_rate INTEGER, calories_burned INTEGER,
    weight_kg REAL, waist_cm REAL, water_ml INTEGER,
    energy_level INTEGER, soreness INTEGER, notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, log_date)
);
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rec_date TEXT, content TEXT, tool_calls_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    pin_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_profile (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    name TEXT, age INTEGER, gender TEXT,
    weight_kg REAL, height_cm REAL, target_weight_kg REAL,
    resting_hr INTEGER DEFAULT 60,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS daily_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    log_date TEXT NOT NULL,
    sleep_hours REAL, sleep_quality INTEGER, deep_sleep_hours REAL,
    exercise_type TEXT, exercise_minutes INTEGER,
    avg_heart_rate INTEGER, max_heart_rate INTEGER, calories_burned INTEGER,
    weight_kg REAL, waist_cm REAL, water_ml INTEGER,
    energy_level INTEGER, soreness INTEGER, notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, log_date)
);
CREATE TABLE IF NOT EXISTS recommendations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rec_date TEXT, content TEXT, tool_calls_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db():
    with db() as conn:
        conn.executescript(PG_SCHEMA if USE_PG else SQLITE_SCHEMA)


# ── 認證 ──────────────────────────────────────────────────────────────────────
def hash_pin(username: str, pin: str) -> str:
    """username + pin + salt → sha256；簡單但足夠 demo 用。"""
    salt = "fitcoach_v1"
    return hashlib.sha256(f"{salt}|{username.lower()}|{pin}".encode()).hexdigest()


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "unauthenticated"}), 401
        return fn(*args, **kwargs)

    return wrapper


def current_uid() -> int:
    return session["user_id"]


# ── 領域邏輯 ──────────────────────────────────────────────────────────────────
def zone2_range(age: int, resting_hr: int = 60) -> tuple[int, int]:
    """Karvonen 公式：(HRmax - HRrest) × 60~70% + HRrest"""
    max_hr = 220 - age
    hrr = max_hr - resting_hr
    return int(resting_hr + hrr * 0.60), int(resting_hr + hrr * 0.70)


def bmi(weight_kg: float, height_cm: float) -> float:
    h = height_cm / 100
    return round(weight_kg / (h * h), 1)


def get_profile(uid: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM user_profile WHERE user_id=?", (uid,)
        ).fetchone()
        return dict(row) if row else None


def get_recent_logs(uid: int, days: int = 7) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_logs WHERE user_id=? ORDER BY log_date DESC LIMIT ?",
            (uid, days),
        ).fetchall()
        return [dict(r) for r in rows]


def compute_stats(uid: int) -> dict:
    logs = get_recent_logs(uid, 30)
    if not logs:
        return {
            "has_today_log": False,
            "days_since_last_log": None,
            "weight_change_7d": None,
            "waist_change_7d": None,
            "consecutive_rest_days": 0,
            "log_streak": 0,
            "weekly_zone2_minutes": 0,
        }

    today = date.today().isoformat()
    last_log_date = logs[0]["log_date"]
    days_since = (date.today() - date.fromisoformat(last_log_date)).days

    streak = 0
    expected = date.today()
    log_dates = {l["log_date"] for l in logs}
    while expected.isoformat() in log_dates:
        streak += 1
        expected -= timedelta(days=1)

    rest_streak = 0
    for l in logs:
        if l.get("exercise_type") in ("rest", None) or not l.get("exercise_minutes"):
            rest_streak += 1
        else:
            break

    week_ago = (date.today() - timedelta(days=7)).isoformat()
    weekly_z2 = sum(
        (l.get("exercise_minutes") or 0)
        for l in logs
        if l["log_date"] >= week_ago and l.get("exercise_type") == "zone2"
    )

    def delta(field):
        recent = [l for l in logs if l.get(field) is not None][:7]
        if len(recent) < 2:
            return None
        return round(recent[0][field] - recent[-1][field], 1)

    return {
        "has_today_log": today in log_dates,
        "days_since_last_log": days_since,
        "weight_change_7d": delta("weight_kg"),
        "waist_change_7d": delta("waist_cm"),
        "consecutive_rest_days": rest_streak,
        "log_streak": streak,
        "weekly_zone2_minutes": weekly_z2,
    }


# ── Claude AI Agent：Tool Use（per-user scope） ───────────────────────────────
AGENT_TOOLS = [
    {
        "name": "get_user_profile",
        "description": "取得目前使用者的個人基本資料（年齡、體重、目標、靜止心率等）。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_logs",
        "description": "取得目前使用者最近 N 天的健康打卡記錄（睡眠、運動、心率、體重、腰圍、主觀感受）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "要查幾天（1-30）", "default": 7}
            },
        },
    },
    {
        "name": "calculate_zone2_hr",
        "description": "用 Karvonen 公式計算 Zone 2 燃脂目標心率區間。",
        "input_schema": {
            "type": "object",
            "properties": {
                "age": {"type": "integer"},
                "resting_hr": {"type": "integer", "default": 60},
            },
            "required": ["age"],
        },
    },
    {
        "name": "get_weight_waist_trend",
        "description": "回傳目前使用者最近 N 天的體重與腰圍變化序列。",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
        },
    },
]


def make_tool_runner(uid: int):
    """產生 closure，所有工具呼叫都鎖在這位 user 身上。"""

    def run_tool(name: str, args: dict) -> str:
        if name == "get_user_profile":
            return json.dumps(get_profile(uid) or {}, ensure_ascii=False)
        if name == "get_recent_logs":
            return json.dumps(get_recent_logs(uid, args.get("days", 7)), ensure_ascii=False)
        if name == "calculate_zone2_hr":
            low, high = zone2_range(args["age"], args.get("resting_hr", 60))
            return json.dumps({"low": low, "high": high, "unit": "bpm"})
        if name == "get_weight_waist_trend":
            logs = get_recent_logs(uid, args.get("days", 30))
            series = [
                {
                    "date": l["log_date"],
                    "weight_kg": l.get("weight_kg"),
                    "waist_cm": l.get("waist_cm"),
                }
                for l in logs
                if l.get("weight_kg") or l.get("waist_cm")
            ]
            return json.dumps(series, ensure_ascii=False)
        return json.dumps({"error": f"unknown tool {name}"})

    return run_tool


SYSTEM_PROMPT = """你是 FitCoach，一位融合運動科學與行為心理學的個人健身教練。
你的專長：Zone 2 有氧燃脂訓練、HIIT 配比、睡眠恢復優化、減重平台期突破。

工作流程：
1. 先用工具取得使用者最新資料與最近的健康記錄。
2. 根據資料推理：累積疲勞、睡眠負債、運動週期、體重/腰圍趨勢。
3. 輸出今日個人化建議，繁體中文，親切專業，可執行。

最新運動科學共識（你必須遵守）：
- Zone 2（60-70% HRmax）每週累積 150-180 分鐘，是粒線體生合成與脂肪氧化最有效的區間。
- 睡眠 < 6 小時時，當天應降強度為 Zone 2 或休息，避免皮質醇升高拖累減重。
- 連續 3 天以上重訓無休息 → 強制安排主動恢復日。
- 體重平台期（7 天 < 0.2kg 變化）→ 建議重新評估熱量缺口或加入 HIIT 變化刺激。
- 腰圍是內臟脂肪的更好指標，比體重更值得重視。

輸出格式（Markdown）：
### 📊 昨日/近期分析
### 🏃 今日運動處方
（明確：類型、時長、目標心率區間 bpm）
### 😴 恢復建議
### 📈 趨勢洞察
### ⚡ 今日一句話

不要超過 500 字。"""


def run_agent(uid: int, user_message: str, max_iterations: int = 6) -> dict:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_message}]
    tool_calls_log = []
    run_tool = make_tool_runner(uid)

    for _ in range(max_iterations):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=AGENT_TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input)
                    tool_calls_log.append(
                        {"tool": block.name, "input": block.input, "output_preview": result[:200]}
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(b.text for b in resp.content if b.type == "text")
        return {"content": text, "tool_calls": tool_calls_log}

    return {"content": "（Agent 迭代次數已達上限）", "tool_calls": tool_calls_log}


# ── 路由：頁面 ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return {"ok": True, "model": MODEL}


# ── 路由：認證 ────────────────────────────────────────────────────────────────
@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    if "user_id" not in session:
        return jsonify({"error": "unauthenticated"}), 401
    return jsonify({"user_id": session["user_id"], "username": session["username"]})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """新使用者首次輸入 → 註冊；既有使用者 → 驗證 PIN。"""
    d = request.json or {}
    username = (d.get("username") or "").strip()
    pin = (d.get("pin") or "").strip()

    if not (2 <= len(username) <= 20):
        return jsonify({"error": "名稱長度需 2-20 字"}), 400
    if not (pin.isdigit() and len(pin) == 4):
        return jsonify({"error": "PIN 必須是 4 位數字"}), 400

    pin_h = hash_pin(username, pin)
    with db() as conn:
        row = conn.execute(
            "SELECT id, pin_hash FROM users WHERE username=?", (username,)
        ).fetchone()
        if row:
            if row["pin_hash"] != pin_h:
                return jsonify({"error": "PIN 錯誤"}), 401
            uid = row["id"]
            created = False
        else:
            # 用 RETURNING id 在 PG/SQLite 3.35+ 皆可
            cur = conn.execute(
                "INSERT INTO users (username, pin_hash) VALUES (?, ?) RETURNING id",
                (username, pin_h),
            )
            uid = cur.fetchone()["id"]
            created = True

    session.permanent = True
    session["user_id"] = uid
    session["username"] = username
    return jsonify({"ok": True, "username": username, "created": created})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


# ── 路由：資料 API（均需登入） ─────────────────────────────────────────────────
@app.route("/api/profile", methods=["GET"])
@require_auth
def api_get_profile():
    p = get_profile(current_uid())
    if p and p.get("age"):
        low, high = zone2_range(p["age"], p.get("resting_hr") or 60)
        p["zone2_low"] = low
        p["zone2_high"] = high
        if p.get("weight_kg") and p.get("height_cm"):
            p["bmi"] = bmi(p["weight_kg"], p["height_cm"])
    return jsonify(p or {})


@app.route("/api/profile", methods=["POST"])
@require_auth
def api_save_profile():
    d = request.json or {}
    uid = current_uid()
    with db() as conn:
        conn.execute(
            """INSERT INTO user_profile
               (user_id, name, age, gender, weight_kg, height_cm, target_weight_kg, resting_hr)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 name=excluded.name, age=excluded.age, gender=excluded.gender,
                 weight_kg=excluded.weight_kg, height_cm=excluded.height_cm,
                 target_weight_kg=excluded.target_weight_kg, resting_hr=excluded.resting_hr""",
            (
                uid,
                d.get("name"),
                d.get("age"),
                d.get("gender"),
                d.get("weight_kg"),
                d.get("height_cm"),
                d.get("target_weight_kg"),
                d.get("resting_hr", 60),
            ),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/logs", methods=["GET"])
@require_auth
def api_get_logs():
    days = int(request.args.get("days", 30))
    return jsonify(get_recent_logs(current_uid(), days))


@app.route("/api/logs", methods=["POST"])
@require_auth
def api_save_log():
    d = request.json or {}
    d.setdefault("log_date", date.today().isoformat())
    uid = current_uid()
    fields = [
        "sleep_hours", "sleep_quality", "deep_sleep_hours",
        "exercise_type", "exercise_minutes", "avg_heart_rate", "max_heart_rate",
        "calories_burned", "weight_kg", "waist_cm", "water_ml",
        "energy_level", "soreness", "notes",
    ]
    cols = ["user_id", "log_date"] + fields
    vals = [uid, d["log_date"]] + [d.get(f) for f in fields]
    placeholders = ",".join(["?"] * len(cols))
    col_csv = ",".join(cols)
    update_csv = ",".join(f"{f}=excluded.{f}" for f in fields)
    with db() as conn:
        conn.execute(
            f"""INSERT INTO daily_logs ({col_csv}) VALUES ({placeholders})
                ON CONFLICT(user_id, log_date) DO UPDATE SET {update_csv}""",
            vals,
        )
        if d.get("weight_kg"):
            conn.execute(
                "UPDATE user_profile SET weight_kg=? WHERE user_id=?",
                (d["weight_kg"], uid),
            )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/stats", methods=["GET"])
@require_auth
def api_stats():
    return jsonify(compute_stats(current_uid()))


@app.route("/api/recommend", methods=["POST"])
@require_auth
def api_recommend():
    uid = current_uid()
    profile = get_profile(uid)
    if not profile:
        return jsonify({"error": "請先設定個人資料"}), 400

    today = date.today().isoformat()
    user_msg = (
        f"今天是 {today}。請主動查詢我的個人資料與近期打卡記錄，"
        f"分析我的狀態，產出今日個人化健康減重建議。"
    )

    try:
        result = run_agent(uid, user_msg)
    except anthropic.AuthenticationError:
        return jsonify({"error": "ANTHROPIC_API_KEY 未設定或無效"}), 500
    except Exception as e:
        return jsonify({"error": f"Agent 執行失敗：{e}"}), 500

    with db() as conn:
        conn.execute(
            "INSERT INTO recommendations (user_id, rec_date, content, tool_calls_json) VALUES (?,?,?,?)",
            (uid, today, result["content"], json.dumps(result["tool_calls"], ensure_ascii=False)),
        )
        conn.commit()

    return jsonify(result)


@app.route("/api/recommend/latest", methods=["GET"])
@require_auth
def api_latest_recommend():
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM recommendations WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (current_uid(),),
        ).fetchone()
        return jsonify(dict(row) if row else {})


# ── 啟動 ──────────────────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
