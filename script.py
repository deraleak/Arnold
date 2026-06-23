"""
Project Arnold — Text Workout Logger
Backend: FastAPI + Ollama (Llama 2) + SQLite + rapidfuzz
Multi-user, session-based (no passwords), 2hr inactivity auto-logout
"""

import sqlite3
import json
import re
import datetime
import os
import secrets
import threading
import time

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from rapidfuzz import process, fuzz

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DB_PATH = "arnold.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama2"
SESSION_TIMEOUT = datetime.timedelta(hours=2)
COOKIE_NAME = "arnold_session"

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_login DATETIME
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS muscle_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            muscle_category TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            exercise TEXT NOT NULL,
            weight REAL,
            reps INTEGER,
            sets INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrations
    try:
        c.execute("ALTER TABLE workouts ADD COLUMN user_id INTEGER REFERENCES users(id)")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE exercises ADD COLUMN description TEXT")
    except sqlite3.OperationalError:
        pass

    default_categories = ["Chest", "Back", "Shoulders", "Arms", "Legs", "Core", "Grip"]
    for cat in default_categories:
        c.execute("INSERT OR IGNORE INTO muscle_categories (name) VALUES (?)", (cat,))

    default_exercises = [
        ("Bench Press", "Chest"), ("Incline Bench Press", "Chest"), ("Dumbbell Flyes", "Chest"),
        ("Pull Up", "Back"), ("Deadlift", "Back"), ("Barbell Row", "Back"), ("Lat Pulldown", "Back"),
        ("Overhead Press", "Shoulders"), ("Lateral Raise", "Shoulders"), ("Face Pull", "Shoulders"),
        ("Bicep Curl", "Arms"), ("Tricep Pushdown", "Arms"), ("Hammer Curl", "Arms"),
        ("Squat", "Legs"), ("Leg Press", "Legs"), ("Romanian Deadlift", "Legs"), ("Leg Curl", "Legs"),
        ("Plank", "Core"), ("Cable Crunch", "Core"), ("Ab Wheel", "Core"),
    ]
    for name, cat in default_exercises:
        c.execute("INSERT OR IGNORE INTO exercises (name, muscle_category) VALUES (?, ?)", (name, cat))

    conn.commit()
    conn.close()
    print("[DB] Initialized.")

# ─────────────────────────────────────────────
# SESSIONS
# ─────────────────────────────────────────────
active_sessions: dict = {}  # token -> {"user_id": int, "last_activity": datetime}

def _get_user_id(token: str | None) -> int | None:
    if not token:
        return None
    session = active_sessions.get(token)
    if not session:
        return None
    if datetime.datetime.now() - session["last_activity"] > SESSION_TIMEOUT:
        active_sessions.pop(token, None)
        return None
    session["last_activity"] = datetime.datetime.now()
    return session["user_id"]

def _token(request: Request) -> str | None:
    return request.headers.get("x-arnold-token") or None

def _session_cleanup():
    while True:
        time.sleep(300)
        now = datetime.datetime.now()
        expired = [t for t, s in list(active_sessions.items()) if now - s["last_activity"] > SESSION_TIMEOUT]
        for t in expired:
            active_sessions.pop(t, None)

# ─────────────────────────────────────────────
# CHAT LOG
# ─────────────────────────────────────────────
chat_log: list = []

def add_chat(role: str, message: str):
    chat_log.append({"role": role, "message": message, "time": datetime.datetime.now().strftime("%H:%M:%S")})
    print(f"[{role.upper()}] {message}")

# ─────────────────────────────────────────────
# FUZZY EXERCISE MATCHING
# ─────────────────────────────────────────────
def get_exercise_names():
    conn = get_db()
    rows = conn.execute("SELECT name FROM exercises").fetchall()
    conn.close()
    return [r["name"] for r in rows]

def fuzzy_match_exercise(input_name: str):
    names = get_exercise_names()
    if not names:
        return input_name
    result = process.extractOne(input_name, names, scorer=fuzz.WRatio)
    if result and result[1] >= 60:
        return result[0]
    return input_name

# ─────────────────────────────────────────────
# OLLAMA INTENT PARSING
# ─────────────────────────────────────────────
def ask_ollama(prompt: str) -> str:
    try:
        resp = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}, timeout=30)
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"ERROR: {e}"

def parse_intent(text: str) -> dict:
    exercise_list = ", ".join(get_exercise_names())
    prompt = f"""You are a workout logging assistant. Parse the user's command and return ONLY valid JSON.

Known exercises: {exercise_list}

Intents:
- log_set: user is logging a set. Extract: exercise, weight (lbs), reps, sets, confident (bool)
  Set confident=false if the exercise name is ambiguous or not clearly in the known list.
- create_exercise: user wants to create a new exercise
- create_muscle_category: user wants to create a new muscle category
- list_categories: user wants to see all muscle categories
- list_exercises: user wants to see all exercises
- edit_exercise: user wants to rename an exercise. Extract: old_name, new_name
- unknown: anything else

Return JSON only, no explanation. Examples:
{{"intent": "log_set", "exercise": "Deadlift", "weight": 315, "reps": 5, "sets": 3, "confident": true}}
{{"intent": "log_set", "exercise": "Bench", "weight": 135, "reps": 8, "sets": 1, "confident": false}}
{{"intent": "create_exercise"}}
{{"intent": "list_categories"}}
{{"intent": "edit_exercise", "old_name": "Bench", "new_name": "Bench Press"}}
{{"intent": "unknown"}}

User said: "{text}"
JSON:"""

    raw = ask_ollama(prompt)
    raw = re.sub(r"```json|```", "", raw).strip()
    match = re.search(r'\{.*?\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"intent": "unknown"}

# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────
dialogue_state: dict = {"active": False, "type": None, "step": None, "data": {}}
current_exercise_by_user: dict = {}  # user_id -> exercise name
_exerciseNames_cache: list = []  # cleared on exercise add/edit/delete

# Matches shorthand like "225x5", "135 x 8", "100 x 10 x 3", "225lbs x 5"
_SHORTHAND_RE = re.compile(
    r'^(\d+(?:\.\d+)?)\s*(?:lbs?)?\s*[x×*]\s*(\d+)(?:\s*[x×*]\s*(\d+))?$',
    re.IGNORECASE
)

def _parse_shorthand(text: str) -> dict | None:
    """Parse bare weight×reps entries that have no exercise name."""
    m = _SHORTHAND_RE.match(text.strip())
    if not m:
        return None
    weight = float(m.group(1))
    reps   = int(m.group(2))
    sets   = int(m.group(3)) if m.group(3) else 1
    return {"intent": "log_set", "weight": weight, "reps": reps, "sets": sets}

def handle_log_set(parsed: dict, user_id: int):
    exercise_raw = parsed.get("exercise", "")
    exercise = fuzzy_match_exercise(exercise_raw) if exercise_raw else None

    # Fall back to the last logged exercise if none was specified
    if not exercise or exercise_raw.lower() in ("", "unknown"):
        exercise = current_exercise_by_user.get(user_id)

    weight = parsed.get("weight")
    reps = parsed.get("reps")
    sets = parsed.get("sets", 1)

    if not exercise:
        add_chat("arnold", "No active exercise — specify one first, e.g. 'deadlift 225 x 5'.")
        return
    if not reps:
        add_chat("arnold", "I couldn't parse that set. Try: 'deadlift 225 3x5'")
        return

    conn = get_db()
    for _ in range(sets):
        conn.execute(
            "INSERT INTO workouts (user_id, exercise, weight, reps, sets) VALUES (?, ?, ?, ?, ?)",
            (user_id, exercise, weight, reps, sets)
        )
    conn.commit()
    conn.close()

    current_exercise_by_user[user_id] = exercise
    weight_str = f"{weight}lbs" if weight else "bodyweight"
    add_chat("arnold", f"Logged {sets} set{'s' if sets > 1 else ''} of {exercise} — {weight_str} x {reps} reps.")

def handle_list_categories():
    conn = get_db()
    rows = conn.execute("SELECT name FROM muscle_categories ORDER BY name").fetchall()
    conn.close()
    if rows:
        add_chat("arnold", f"Muscle categories: {', '.join(r['name'] for r in rows)}")
    else:
        add_chat("arnold", "No muscle categories found.")

def handle_list_exercises():
    conn = get_db()
    rows = conn.execute("SELECT name, muscle_category FROM exercises ORDER BY muscle_category, name").fetchall()
    conn.close()
    if rows:
        grouped: dict = {}
        for r in rows:
            grouped.setdefault(r["muscle_category"] or "Uncategorized", []).append(r["name"])
        add_chat("arnold", " | ".join(f"{cat}: {', '.join(exs)}" for cat, exs in grouped.items()))
    else:
        add_chat("arnold", "No exercises found.")

def handle_edit_exercise(parsed: dict):
    old = parsed.get("old_name", "")
    new = parsed.get("new_name", "")
    if not old or not new:
        add_chat("arnold", "I need both the old name and the new name.")
        return
    old_matched = fuzzy_match_exercise(old)
    conn = get_db()
    conn.execute("UPDATE exercises SET name = ? WHERE name = ?", (new, old_matched))
    conn.commit()
    conn.close()
    add_chat("arnold", f"Renamed '{old_matched}' to '{new}'.")

def start_create_exercise_dialogue():
    dialogue_state.update({"active": True, "type": "create_exercise", "step": "name", "data": {}})
    add_chat("arnold", "What is the name of the new exercise?")

def start_create_category_dialogue():
    dialogue_state.update({"active": True, "type": "create_muscle_category", "step": "name", "data": {}})
    add_chat("arnold", "What would you like to name the new muscle category?")

def handle_dialogue_input(text: str, user_id: int = None):
    d = dialogue_state
    if d["type"] == "confirm_log":
        answer = text.strip().lower()
        if answer in ("yes", "y", "yeah", "yep", "correct", "log it", "do it"):
            handle_log_set(d["data"], user_id)
        else:
            add_chat("arnold", "Cancelled. Tell me what to log instead.")
        dialogue_state.update({"active": False, "type": None, "step": None, "data": {}})
        return

    if d["type"] == "create_muscle_category":
        name = text.strip().title()
        conn = get_db()
        try:
            conn.execute("INSERT INTO muscle_categories (name) VALUES (?)", (name,))
            conn.commit()
            add_chat("arnold", f"Muscle category '{name}' created.")
        except sqlite3.IntegrityError:
            add_chat("arnold", f"Category '{name}' already exists.")
        finally:
            conn.close()
        dialogue_state.update({"active": False, "type": None, "step": None, "data": {}})

    elif d["type"] == "create_exercise":
        if d["step"] == "name":
            d["data"]["name"] = text.strip().title()
            d["step"] = "category"
            conn = get_db()
            cats = [r["name"] for r in conn.execute("SELECT name FROM muscle_categories ORDER BY name").fetchall()]
            conn.close()
            add_chat("arnold", f"Which muscle category? Options: {', '.join(cats)}")
        elif d["step"] == "category":
            name = d["data"]["name"]
            category = text.strip().title()
            conn = get_db()
            try:
                conn.execute("INSERT INTO exercises (name, muscle_category) VALUES (?, ?)", (name, category))
                conn.commit()
                add_chat("arnold", f"Exercise '{name}' added under '{category}'.")
            except sqlite3.IntegrityError:
                add_chat("arnold", f"Exercise '{name}' already exists.")
            finally:
                conn.close()
            dialogue_state.update({"active": False, "type": None, "step": None, "data": {}})

def process_input(text: str, user_id: int):
    add_chat("you", text)
    if dialogue_state["active"]:
        handle_dialogue_input(text, user_id)
        return

    # Shorthand: "225 x 5" or "135x8x3" — skip Ollama, use current exercise
    shorthand = _parse_shorthand(text)
    if shorthand:
        if user_id in current_exercise_by_user:
            shorthand["exercise"] = current_exercise_by_user[user_id]
            handle_log_set(shorthand, user_id)
        else:
            add_chat("arnold", "No active exercise — log one by name first, e.g. 'deadlift 225 x 5'.")
        return

    parsed = parse_intent(text)
    intent = parsed.get("intent", "unknown")
    if intent == "log_set":
        if not parsed.get("confident", True):
            exercise = parsed.get("exercise", "?")
            weight = parsed.get("weight", "?")
            reps = parsed.get("reps", "?")
            sets = parsed.get("sets", 1)
            add_chat("arnold", f"Did you mean: {exercise} — {weight}lbs x {reps} reps x {sets} set(s)? Reply yes to confirm or correct me.")
            dialogue_state.update({"active": True, "type": "confirm_log", "step": None, "data": parsed})
        else:
            handle_log_set(parsed, user_id)
    elif intent == "create_exercise":
        start_create_exercise_dialogue()
    elif intent == "create_muscle_category":
        start_create_category_dialogue()
    elif intent == "list_categories":
        handle_list_categories()
    elif intent == "list_exercises":
        handle_list_exercises()
    elif intent == "edit_exercise":
        handle_edit_exercise(parsed)
    else:
        add_chat("arnold", "I didn't understand that. Try: 'deadlift 225 3x5', 'create exercise', 'list exercises', etc.")

# ─────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────
app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return f.read()

# ── Users ──
@app.get("/api/users")
async def list_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, last_login FROM users ORDER BY COALESCE(last_login, '1900-01-01') DESC, name ASC"
    ).fetchall()
    conn.close()
    return JSONResponse([dict(r) for r in rows])

@app.post("/api/users")
async def create_user(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Name required"}, status_code=400)
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
        conn.commit()
        row = conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        uid = row["id"]
    except sqlite3.IntegrityError:
        conn.close()
        return JSONResponse({"error": "Name already taken"}, status_code=400)
    conn.close()
    return JSONResponse({"id": uid, "name": name})

@app.put("/api/users/{uid}")
async def update_user(uid: int, request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Name required"}, status_code=400)
    conn = get_db()
    try:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, uid))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return JSONResponse({"error": "Name already taken"}, status_code=400)
    conn.close()
    return JSONResponse({"ok": True})

# ── Session ──
@app.get("/api/session")
async def get_session(request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"user": None})
    conn = get_db()
    user = conn.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return JSONResponse({"user": dict(user) if user else None})

@app.post("/api/session/login")
async def session_login(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    conn = get_db()
    user = conn.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return JSONResponse({"error": "User not found"}, status_code=404)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
    conn.commit()
    conn.close()

    token = secrets.token_urlsafe(32)
    active_sessions[token] = {"user_id": user_id, "last_activity": datetime.datetime.now()}
    add_chat("arnold", f"Welcome back, {user['name']}.")
    # Return token so the client can store it and send as a header
    return JSONResponse({"id": user["id"], "name": user["name"], "token": token})

@app.post("/api/session/logout")
async def session_logout(request: Request):
    token = _token(request)
    if token:
        active_sessions.pop(token, None)
    return JSONResponse({"ok": True})

# ── Workouts ──
@app.get("/api/chat")
async def get_chat():
    return JSONResponse(chat_log)

@app.get("/api/workouts/today")
async def get_today_workouts(request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse([])
    conn = get_db()
    today = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT id, exercise, weight, reps, sets, timestamp FROM workouts WHERE user_id = ? AND DATE(timestamp) = ? ORDER BY timestamp DESC",
        (user_id, today)
    ).fetchall()
    conn.close()
    return JSONResponse([dict(r) for r in rows])

@app.post("/api/workout/manual")
async def manual_log(request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    body = await request.json()
    exercise = body.get("exercise", "").strip()
    weight = body.get("weight")
    reps = body.get("reps")
    sets_val = body.get("sets", 1)
    if not exercise or not reps:
        return JSONResponse({"error": "exercise and reps required"}, status_code=400)
    conn = get_db()
    for _ in range(sets_val):
        conn.execute(
            "INSERT INTO workouts (user_id, exercise, weight, reps, sets) VALUES (?,?,?,?,?)",
            (user_id, exercise, weight, reps, sets_val)
        )
    conn.commit()
    conn.close()
    current_exercise_by_user[user_id] = exercise
    return JSONResponse({"ok": True})

@app.put("/api/workout/{wid}")
async def update_workout(wid: int, request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    body = await request.json()
    exercise = body.get("exercise", "").strip()
    weight = body.get("weight")
    reps = body.get("reps")
    sets = body.get("sets", 1)
    if not exercise or not reps:
        return JSONResponse({"error": "exercise and reps required"}, status_code=400)
    conn = get_db()
    conn.execute(
        "UPDATE workouts SET exercise=?, weight=?, reps=?, sets=? WHERE id=? AND user_id=?",
        (exercise, weight, reps, sets, wid, user_id)
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

@app.delete("/api/workout/{wid}")
async def delete_workout(wid: int, request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    conn = get_db()
    conn.execute("DELETE FROM workouts WHERE id=? AND user_id=?", (wid, user_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

@app.get("/api/exercise/{name}/info")
async def get_exercise_info(name: str, request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    conn = get_db()
    row = conn.execute("SELECT name, description FROM exercises WHERE name=?", (name,)).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"name": name, "description": ""})
    return JSONResponse({"name": row["name"], "description": row["description"] or ""})

@app.put("/api/exercise/{name}/description")
async def update_exercise_description(name: str, request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    body = await request.json()
    description = body.get("description", "").strip()
    conn = get_db()
    conn.execute("UPDATE exercises SET description=? WHERE name=?", (description, name))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

@app.get("/api/exercises")
async def list_exercises_api():
    conn = get_db()
    rows = conn.execute("SELECT name, muscle_category, description FROM exercises ORDER BY muscle_category, name").fetchall()
    conn.close()
    return JSONResponse([dict(r) for r in rows])

@app.post("/api/exercises")
async def create_exercise_api(request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    body = await request.json()
    name = body.get("name", "").strip().title()
    category = body.get("category", "").strip().title() or None
    if not name:
        return JSONResponse({"error": "Name required"}, status_code=400)
    conn = get_db()
    try:
        conn.execute("INSERT INTO exercises (name, muscle_category) VALUES (?,?)", (name, category))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return JSONResponse({"error": "Exercise already exists"}, status_code=400)
    conn.close()
    _exerciseNames_cache.clear()
    return JSONResponse({"ok": True, "name": name})

@app.put("/api/exercises/{name}")
async def update_exercise_api(name: str, request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    body = await request.json()
    new_name    = body.get("name", "").strip().title() or name
    category    = body.get("category", "").strip().title() or None
    description = body.get("description", "").strip() or None
    conn = get_db()
    try:
        conn.execute(
            "UPDATE exercises SET name=?, muscle_category=?, description=? WHERE name=?",
            (new_name, category, description, name)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return JSONResponse({"error": "Name already taken"}, status_code=400)
    conn.close()
    _exerciseNames_cache.clear()
    return JSONResponse({"ok": True})

@app.delete("/api/exercises/{name}")
async def delete_exercise_api(name: str, request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    conn = get_db()
    conn.execute("DELETE FROM exercises WHERE name=?", (name,))
    conn.commit()
    conn.close()
    _exerciseNames_cache.clear()
    return JSONResponse({"ok": True})

@app.get("/api/categories")
async def list_categories_api():
    conn = get_db()
    rows = conn.execute("SELECT name FROM muscle_categories ORDER BY name").fetchall()
    conn.close()
    return JSONResponse([r["name"] for r in rows])

@app.get("/api/dialogue")
async def get_dialogue_state():
    return JSONResponse({"active": dialogue_state["active"]})

@app.post("/api/input")
async def post_input(request: Request):
    user_id = _get_user_id(_token(request))
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"ok": True})
    if not user_id:
        add_chat("arnold", "Please select a user first.")
        return JSONResponse({"ok": True})
    process_input(text, user_id)
    return JSONResponse({"ok": True})

@app.get("/api/exercise/current")
async def get_current_exercise(request: Request):
    user_id = _get_user_id(_token(request))
    exercise = current_exercise_by_user.get(user_id) if user_id else None
    return JSONResponse({"exercise": exercise})

@app.get("/api/exercise/{name}/history")
async def get_exercise_history(name: str, request: Request):
    user_id = _get_user_id(_token(request))
    if not user_id:
        return JSONResponse([])
    conn = get_db()
    rows = conn.execute(
        "SELECT DATE(timestamp) as date, weight, reps, sets FROM workouts WHERE user_id = ? AND exercise = ? ORDER BY timestamp DESC",
        (user_id, name)
    ).fetchall()
    conn.close()

    sessions: dict = {}
    for r in rows:
        sessions.setdefault(r["date"], []).append({"weight": r["weight"], "reps": r["reps"], "sets": r["sets"]})

    result = []
    for date, entries in sessions.items():
        volume = round(sum((e["weight"] or 0) * e["reps"] for e in entries))
        max_weight = max((e["weight"] or 0) for e in entries)
        # Best set = heaviest weight; among those, most reps
        best_reps = max(
            (e["reps"] for e in entries if (e["weight"] or 0) == max_weight),
            default=0
        )
        result.append({
            "date": date,
            "entries": entries,
            "volume": volume,
            "max_weight": max_weight,
            "max_set": {"weight": max_weight, "reps": best_reps},
            "is_pr": False,
        })

    # Mark weight PRs chronologically (result is newest-first; iterate reversed = oldest-first)
    running_max = 0
    for session in reversed(result):
        if session["max_weight"] > running_max:
            running_max = session["max_weight"]
            session["is_pr"] = True

    return JSONResponse(result)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()

    threading.Thread(target=_session_cleanup, daemon=True).start()
    print("[Server] Dashboard at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
