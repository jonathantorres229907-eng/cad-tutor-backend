import sqlite3
from pathlib import Path
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "tokens.db"

app = FastAPI()

app.add_middleware(
  CORSMiddleware,
  allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)

def init_token_db():
  conn = sqlite3.connect(DB_PATH)
  try:
    cur = conn.cursor()
    cur.execute(
      """
      CREATE TABLE IF NOT EXISTS tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      );
      """
    )
    conn.commit()
  finally:
    cur.close()
    conn.close()

init_token_db()

@app.post("/upload")
async def upload(description: str = Form(...), software: str = Form(...), customer_email: str = Form(...)):
  try:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO tokens (token) VALUES (?)", ("test-token",))
    conn.commit()
    cur.close()
    conn.close()
  except Exception as e:
    return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

  return JSONResponse({"status": "ok", "description": description, "software": software, "customer_email": customer_email})