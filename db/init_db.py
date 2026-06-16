"""Initialize the SQLite database from schema.sql."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "lol_model.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")


if __name__ == "__main__":
    init()
