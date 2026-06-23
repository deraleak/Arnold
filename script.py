"""
Project Arnold — Voice Workout Logger
Backend: FastAPI + Ollama (Llama 2) + faster-whisper + SQLite + rapidfuzz
Push-to-talk via Enter key (ElevenLabs TTS ready to reintegrate)
"""

import sqlite3
import json
import re
import threading
import time
import datetime
import os

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel
from rapidfuzz import process, fuzz

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DB_PATH = "arnold.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama2"
WHISPER_MODEL_SIZE = "small"  # tiny / small / medium / large

# ElevenLabs — fill in when ready to reintegrate TTS
ELEVENLABS_API_KEY = ""
ELEVENLABS_VOICE_ID = ""  # Arnold voice clone ID from ElevenLabs

# ─────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

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
            exercise TEXT NOT NULL,
            weight REAL,
            reps INTEGER,
            sets INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Seed default muscle categories if empty
    default_categories = ["Chest", "Back", "Shoulders", "Arms", "Legs", "Core"]
    for cat in default_categories:
        c.execute("INSERT OR IGNORE INTO muscle_categories (name) VALUES (?)", (cat,))

    # Seed default exercises if empty
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
# CHAT LOG (in-memory, served to dashboard)
# ─────────────────────────────────────────────
chat_log = []

def add_chat(role: str, message: str):
    chat_log.append({
        "role": role,
        "message": message,
        "time": datetime.datetime.now().strftime("%H:%M:%S")
    })
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
        resp = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False
        }, timeout=30)
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"ERROR: {e}"

def parse_intent(transcript: str) -> dict:
    """Ask Llama to classify the intent and extract structured data."""
    exercise_list = ", ".join(get_exercise_names())
    prompt = f"""You are a workout logging assistant. Parse the user's voice command and return ONLY valid JSON.

Known exercises: {exercise_list}

Intents:
- log_set: user is logging a set. Extract: exercise, weight (lbs), reps, sets
- create_exercise: user wants to create a new exercise
- create_muscle_category: user wants to create a new muscle category
- list_categories: user wants to see all muscle categories
- list_exercises: user wants to see all exercises
- edit_exercise: user wants to rename an exercise. Extract: old_name, new_name
- unknown: anything else

Return JSON only, no explanation. Examples:
{{"intent": "log_set", "exercise": "Deadlift", "weight": 315, "reps": 5, "sets": 3}}
{{"intent": "create_exercise"}}
{{"intent": "list_categories"}}
{{"intent": "edit_exercise", "old_name": "Bench", "new_name": "Bench Press"}}
{{"intent": "unknown"}}

User said: "{transcript}"
JSON:"""

    raw = ask_ollama(prompt)

    # Strip markdown fences if present
    raw = re.sub(r"```json|```", "", raw).strip()

    # Extract first JSON object from response
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

# Holds state for multi-turn dialogues (create exercise / create category)
dialogue_state = {"active": False, "type": None, "step": None, "data": {}}

def handle_log_set(parsed: dict):
    exercise_raw = parsed.get("exercise", "Unknown")
    exercise = fuzzy_match_exercise(exercise_raw)
    weight = parsed.get("weight")
    reps = parsed.get("reps")
    sets = parsed.get("sets", 1)

    if not exercise or not reps:
        add_chat("arnold", "I couldn't parse that set. Try: 'Log set deadlift 225, 3 sets of 5'")
        return

    conn = get_db()
    for _ in range(sets):
        conn.execute(
            "INSERT INTO workouts (exercise, weight, reps, sets) VALUES (?, ?, ?, ?)",
            (exercise, weight, reps, sets)
        )
    conn.commit()
    conn.close()

    weight_str = f"{weight}lbs" if weight else "bodyweight"
    add_chat("arnold", f"Logged {sets} set{'s' if sets > 1 else ''} of {exercise} — {weight_str} x {reps} reps.")

def handle_list_categories():
    conn = get_db()
    rows = conn.execute("SELECT name FROM muscle_categories ORDER BY name").fetchall()
    conn.close()
    if rows:
        cats = ", ".join(r["name"] for r in rows)
        add_chat("arnold", f"Muscle categories: {cats}")
    else:
        add_chat("arnold", "No muscle categories found. Try 'create muscle category'.")

def handle_list_exercises():
    conn = get_db()
    rows = conn.execute("SELECT name, muscle_category FROM exercises ORDER BY muscle_category, name").fetchall()
    conn.close()
    if rows:
        grouped = {}
        for r in rows:
            grouped.setdefault(r["muscle_category"] or "Uncategorized", []).append(r["name"])
        lines = []
        for cat, exs in grouped.items():
            lines.append(f"{cat}: {', '.join(exs)}")
        add_chat("arnold", " | ".join(lines))
    else:
        add_chat("arnold", "No exercises found. Try 'create exercise'.")

def handle_edit_exercise(parsed: dict):
    old = parsed.get("old_name", "")
    new = parsed.get("new_name", "")
    if not old or not new:
        add_chat("arnold", "I need both the old name and the new name to edit an exercise.")
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

def handle_dialogue_input(text: str):
    """Handle typed text input during an active dialogue."""
    d = dialogue_state

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
            # Show available categories
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

# ─────────────────────────────────────────────
# VOICE PIPELINE
# ─────────────────────────────────────────────
print("[Whisper] Loading model...")
whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
print("[Whisper] Ready.")

def record_and_transcribe() -> str:
    """Record audio from mic until Enter is released, then transcribe."""
    import pyaudio
    import wave
    import tempfile

    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000

    p = pyaudio.PyAudio()
    stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)

    print("[MIC] Recording... (release Enter to stop)")
    frames = []
    try:
        while True:
            data = stream.read(CHUNK, exception_on_overflow=False)
            frames.append(data)
            if not _recording:
                break
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

    # Save to temp wav
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    wf = wave.open(tmp_path, 'wb')
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(p.get_sample_size(FORMAT))
    wf.setframerate(RATE)
    wf.writeframes(b''.join(frames))
    wf.close()

    segments, _ = whisper_model.transcribe(tmp_path, beam_size=5)
    os.unlink(tmp_path)
    return " ".join(seg.text for seg in segments).strip()

_recording = False

def process_transcript(transcript: str):
    add_chat("you", transcript)

    if dialogue_state["active"]:
        handle_dialogue_input(transcript)
        return

    parsed = parse_intent(transcript)
    intent = parsed.get("intent", "unknown")

    if intent == "log_set":
        handle_log_set(parsed)
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
        add_chat("arnold", "I didn't understand that. Try: 'log set', 'create exercise', 'list exercises', etc.")

# ─────────────────────────────────────────────
# PUSH-TO-TALK LOOP (runs in background thread)
# ─────────────────────────────────────────────
def push_to_talk_loop():
    global _recording
    import keyboard  # pip install keyboard

    print("\n[Arnold] Ready. Hold SPACE to record, release to process.\n")
    add_chat("arnold", "Arnold online. Hold SPACE to speak.")

    while True:
        keyboard.wait("space")
        _recording = True
        transcript = record_and_transcribe()
        _recording = False
        if transcript:
            process_transcript(transcript)

# ─────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────
app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/chat")
async def get_chat():
    return JSONResponse(chat_log)

@app.get("/api/workouts/today")
async def get_today_workouts():
    conn = get_db()
    today = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT exercise, weight, reps, sets, timestamp FROM workouts WHERE DATE(timestamp) = ? ORDER BY timestamp DESC",
        (today,)
    ).fetchall()
    conn.close()
    return JSONResponse([dict(r) for r in rows])

@app.get("/api/dialogue")
async def get_dialogue_state():
    return JSONResponse({"active": dialogue_state["active"]})

@app.post("/api/dialogue/input")
async def post_dialogue_input(request: Request):
    body = await request.json()
    text = body.get("text", "").strip()
    if text and dialogue_state["active"]:
        handle_dialogue_input(text)
    return JSONResponse({"ok": True})

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()

    # Start push-to-talk in background
    ptt_thread = threading.Thread(target=push_to_talk_loop, daemon=True)
    ptt_thread.start()

    # Start FastAPI server
    print("[Server] Dashboard at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
