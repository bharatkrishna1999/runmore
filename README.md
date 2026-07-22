# runmore

Log a run by photographing your stopwatch lap screen. Gemini reads the laps,
you review them, and Groq returns a one-line coaching note. Runs are stored in
SQLite.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys
python app.py          # http://localhost:5000
```

Environment variables:

| Variable         | Purpose                                             |
| ---------------- | --------------------------------------------------- |
| `GEMINI_API_KEY` | Google Generative Language API key (lap OCR)        |
| `GROQ_API_KEY`   | Groq API key (coaching note)                         |
| `DB_PATH`        | SQLite file path (default: `runs.db` next to app)   |
| `PORT`           | Port to bind (default: `5000`)                       |
| `FLASK_DEBUG`    | `1` enables Flask debug locally; set `0` in prod     |

## Deploying to Render

This repo includes a [`render.yaml`](./render.yaml) blueprint.

1. Push to GitHub and create a new **Blueprint** on Render pointing at this repo,
   or create a **Web Service** manually with:
   - **Language / Runtime:** `Python 3` (make sure this is set — if Render
     misdetects the runtime it will fail with `gunicorn: command not found`)
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python -m gunicorn app:app --bind 0.0.0.0:$PORT`

   Invoking gunicorn as `python -m gunicorn` (rather than the bare `gunicorn`
   console script) makes the start command robust even if the interpreter's
   `bin/` directory is not on `PATH` at runtime.
2. Set the secret env vars `GEMINI_API_KEY` and `GROQ_API_KEY` in the Render
   dashboard (they are marked `sync: false` in the blueprint).
3. The blueprint mounts a 1 GB persistent disk at `/var/data` and points
   `DB_PATH` there so the SQLite database survives redeploys. Without a
   persistent disk, Render's filesystem is ephemeral and logged runs are lost
   on every deploy.
