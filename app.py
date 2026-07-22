import os
import re
import json
import base64
import sqlite3
from datetime import date, datetime

from flask import Flask, request, render_template, redirect, url_for
import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

GEMINI_MODEL = "gemini-2.5-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"
LAP_DISTANCE_M = 370
OPENING_BLOCK_LAPS = 7

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "runs.db")


# ---------- storage ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            total_laps INTEGER,
            distance_km REAL,
            overall_kmh REAL,
            opening_laps INTEGER,
            opening_kmh REAL,
            bonk INTEGER,
            note TEXT,
            coaching_note TEXT,
            laps_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_recent_runs(limit=10):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def save_run(stats, bonk, note, coaching_note, laps):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO runs
            (run_date, total_laps, distance_km, overall_kmh, opening_laps,
             opening_kmh, bonk, note, coaching_note, laps_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date.today().isoformat(),
            stats["total_laps"],
            stats["distance_km"],
            stats["overall_kmh"],
            stats["opening_laps"],
            stats["opening_kmh"],
            bonk,
            note,
            coaching_note,
            json.dumps(laps),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


# ---------- lap time math ----------

def parse_lap_time_to_seconds(time_str):
    """Turn '1:52.30', '0:45', or '45.3' into seconds as a float."""
    parts = [p for p in time_str.strip().split(":") if p != ""]
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(parts[0])


def compute_stats(laps, opening_block_size=OPENING_BLOCK_LAPS):
    total_laps = len(laps)
    distance_km = total_laps * LAP_DISTANCE_M / 1000

    lap_seconds = [parse_lap_time_to_seconds(l["time"]) for l in laps]
    total_seconds = sum(lap_seconds)
    overall_kmh = round((distance_km / (total_seconds / 3600)), 2) if total_seconds else 0

    block = lap_seconds[:opening_block_size]
    block_laps = len(block)
    block_distance_km = block_laps * LAP_DISTANCE_M / 1000
    block_seconds = sum(block)
    opening_kmh = round((block_distance_km / (block_seconds / 3600)), 2) if block_seconds else 0

    return {
        "total_laps": total_laps,
        "distance_km": round(distance_km, 2),
        "overall_kmh": overall_kmh,
        "opening_laps": block_laps,
        "opening_kmh": opening_kmh,
    }


# ---------- Gemini: image to lap JSON ----------

GEMINI_PROMPT = (
    "You are given a photo of a phone stopwatch lap screen. Read every visible "
    "lap row. The time shown for each row is that lap's own duration, not a "
    "cumulative total since the start. Return only a JSON array, nothing else, "
    "no markdown fences, no explanation before or after it. Each item must look "
    "like {\"lap\": 1, \"time\": \"1:52.30\"}. If a row is blurry or unreadable, "
    "skip it rather than guessing a number."
)


def image_file_to_parts(file_storage):
    raw = file_storage.read()
    mime = file_storage.mimetype or "image/jpeg"
    b64_data = base64.b64encode(raw).decode("utf-8")
    return mime, b64_data


def call_gemini_vision(file_storage):
    mime, b64_data = image_file_to_parts(file_storage)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": GEMINI_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": b64_data}},
                ]
            }
        ]
    }
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return parse_json_array(text)


def parse_json_array(text):
    cleaned = text.strip()
    cleaned = re.sub(r"^```(json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        return json.loads(match.group(0)) if match else []


def extract_laps_from_images(image_files):
    all_laps = []
    for file_storage in image_files:
        try:
            all_laps.extend(call_gemini_vision(file_storage))
        except Exception:
            continue

    seen = set()
    merged = []
    for entry in sorted(all_laps, key=lambda x: x.get("lap", 0)):
        lap_num = entry.get("lap")
        if lap_num is None or lap_num in seen:
            continue
        seen.add(lap_num)
        merged.append(entry)
    return merged


# ---------- Groq: stats to coaching line ----------

GROQ_SYSTEM_PROMPT = (
    "You are a terse running coach. You are given today's run stats and a "
    "short history of recent runs. Reply with exactly one short, specific, "
    "actionable sentence of coaching for the next session. No greeting, no "
    "praise, no filler, no disclaimers."
)


def call_groq_coaching(stats, recent_rows, bonk, note):
    history_lines = [
        f"{row['run_date']}: {row['total_laps']} laps, overall {row['overall_kmh']} km/h, "
        f"opening {row['opening_laps']} laps at {row['opening_kmh']} km/h"
        for row in recent_rows
    ]
    history_text = "\n".join(history_lines) if history_lines else "No prior runs logged yet."

    user_content = (
        f"Today: {stats['total_laps']} laps, {stats['distance_km']} km, "
        f"overall {stats['overall_kmh']} km/h, opening {stats['opening_laps']} laps "
        f"at {stats['opening_kmh']} km/h. Bonk reported: {bool(bonk)}. "
        f"Note: {note or 'none'}.\n\nRecent history:\n{history_text}"
    )

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": GROQ_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 120,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ---------- routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extract", methods=["POST"])
def extract():
    files = request.files.getlist("images")
    files = [f for f in files if f and f.filename]
    if not files:
        return redirect(url_for("index"))
    laps = extract_laps_from_images(files)
    return render_template("review.html", laps=laps)


@app.route("/confirm", methods=["POST"])
def confirm():
    count = int(request.form.get("count", 0))
    laps = []
    for i in range(count):
        lap_num = request.form.get(f"lap_{i}")
        time_val = request.form.get(f"time_{i}")
        if lap_num and time_val:
            laps.append({"lap": int(lap_num), "time": time_val})

    bonk = 1 if request.form.get("bonk") == "1" else 0
    note = request.form.get("note", "")

    stats = compute_stats(laps)
    recent_rows = get_recent_runs(limit=10)
    coaching_note = call_groq_coaching(stats, recent_rows, bonk, note)
    save_run(stats, bonk, note, coaching_note, laps)

    return render_template("result.html", stats=stats, coaching_note=coaching_note)


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
