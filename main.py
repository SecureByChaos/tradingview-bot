from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime
import sqlite3

app = FastAPI()

# Database setup
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

# Request schema
class TradeSignal(BaseModel):
    signal: str
    symbol: str
    entry: float
    sl: float
    target: float

@app.get("/")
def home():
    return {"status": "running"}

@app.post("/webhook")
async def webhook(data: TradeSignal):

    cursor.execute("""
    INSERT INTO trades
    (signal, symbol, entry, sl, target, time)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data.signal,
        data.symbol,
        data.entry,
        data.sl,
        data.target,
        datetime.now().isoformat()
    ))

    conn.commit()

    return {
        "success": True,
        "received": data
    }
