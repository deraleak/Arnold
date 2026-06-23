# Project Arnold

A local, self-hosted workout logging web app with a terminal aesthetic. Log sets via natural language, track progress over time, and manage your exercise library — all running on your own machine with no external services.

![Stack](https://img.shields.io/badge/stack-FastAPI%20%2B%20SQLite%20%2B%20Ollama-00ff41?style=flat-square&labelColor=000)

---

## Features

- **Natural language logging** — type `deadlift 225 x 5` or just `225 x 5` and Arnold figures it out
- **Shorthand entry** — once an exercise is active, bare `weight x reps` entries skip the LLM entirely
- **LLM confidence check** — if the intent is ambiguous, Arnold asks for confirmation before logging
- **Manual log form** — bypass the LLM completely with a dropdown + fields form
- **Multi-user** — user selection screen, token-based sessions, 2hr inactivity auto-logout
- **Exercise Details overlay** — animated SVG trend chart, PR detection, session history table, per-exercise descriptions
- **Exercise List manager** — add, rename, re-categorize, describe, or delete exercises from a dedicated overlay
- **Inline edit & delete** — edit or remove any set logged today directly from the right panel
- **Matrix rain UI** — terminal green-on-black aesthetic, Share Tech Mono font

---

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Uvicorn |
| Database | SQLite (local file `arnold.db`) |
| NLP / Intent parsing | Ollama (Llama 2, local) |
| Fuzzy exercise matching | rapidfuzz |
| Frontend | Vanilla JS + CSS Grid, single HTML file |

---

## Prerequisites

Install these once on any machine:

1. **Python 3.11+** — https://python.org (check "Add to PATH")
2. **Git** — https://git-scm.com
3. **Ollama** — https://ollama.com, then pull the model:
   ```
   ollama pull llama2
   ```

---

## Setup (new machine)

```bash
git clone https://github.com/deraleak/Arnold.git
cd Arnold
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

---

## Running

Double-click `start_arnold.bat` — it will:
1. Start Ollama if not already running
2. Launch the Arnold server
3. Open `http://localhost:8000` in your browser

To stop: close the Arnold terminal window.

---

## Project Structure

```
Arnold/
├── script.py          # FastAPI backend — all endpoints, intent parsing, DB logic
├── dashboard.html     # Single-page frontend — all UI in one file
├── requirements.txt   # Python dependencies
├── start_arnold.bat   # One-click launcher (Windows)
├── arnold.db          # SQLite database — local only, not in git
└── .venv/             # Virtual environment — local only, not in git
```

---

## Muscle Categories

Chest · Back · Shoulders · Arms · Legs · Core · Grip

---

## Notes

- `arnold.db` is gitignored — workout data stays local to each machine
- The `.venv` folder is gitignored — recreate it with `pip install -r requirements.txt` on each machine
- Ollama runs entirely locally — no data leaves your machine
