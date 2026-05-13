from fastapi import FastAPI, Request
from datetime import datetime
import sqlite3

app = FastAPI()

conn = sqlite3.connect("trades.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal TEXT,
    symbol TEXT,
    entry REAL,
    sl REAL,
    target REAL,
    time TEXT
)
""")

conn.commit()

@app.get("/")
def home():
    return {"status": "running"}

@app.post("/webhook")
async def webhook(request: Request):

    data = await request.json()

    cursor.execute("""
    INSERT INTO trades
    (signal, symbol, entry, sl, target, time)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data.get("signal"),
        data.get("symbol"),
        data.get("entry"),
        data.get("sl"),
        data.get("target"),
        datetime.now().isoformat()
    ))

    conn.commit()

    return {
        "success": True,
        "data": data
    }
