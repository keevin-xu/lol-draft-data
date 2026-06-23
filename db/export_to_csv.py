"""
Export a SQLite DB to one CSV per table — ready to import into Google Sheets
(import each CSV as a separate sheet/tab).

CSVs are UTF-8 with a header row, written to data/exports/<db-name>/.

Run:
  python db/export_to_csv.py                       # exports match_data.db
  python db/export_to_csv.py --db db/lol_model.db  # any SQLite file
  python db/export_to_csv.py --tables games patches
  python db/export_to_csv.py --out ~/Desktop/sheets
"""

import argparse
import csv
import sqlite3
from pathlib import Path
from typing import List, Optional

from loguru import logger

_ROOT = Path(__file__).parent.parent
DEFAULT_DB = _ROOT / "db" / "match_data.db"

# Google Sheets caps a single spreadsheet at 10,000,000 cells.
GSHEETS_CELL_LIMIT = 10_000_000


def list_tables(conn: sqlite3.Connection) -> List[str]:
    """User tables only (skips sqlite_* internal tables), in name order."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def export_table(conn: sqlite3.Connection, table: str, out_dir: Path) -> tuple:
    """Write one table to <out_dir>/<table>.csv. Returns (rows, cols)."""
    cur = conn.execute(f'SELECT * FROM "{table}"')
    headers = [d[0] for d in cur.description]
    out_path = out_dir / f"{table}.csv"

    n_rows = 0
    # newline="" is required so csv writes \r\n correctly across platforms.
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in cur:
            writer.writerow(["" if v is None else v for v in row])
            n_rows += 1

    cells = n_rows * len(headers)
    flag = "  ⚠ exceeds Google Sheets 10M-cell limit" if cells > GSHEETS_CELL_LIMIT else ""
    logger.info(
        f"  {table:14} → {out_path.name:22} "
        f"{n_rows:>8,} rows × {len(headers)} cols ({cells:,} cells){flag}"
    )
    return n_rows, len(headers)


def main(
    db_path: Path,
    out_dir: Optional[Path] = None,
    tables: Optional[List[str]] = None,
) -> None:
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    out_dir = out_dir or (_ROOT / "data" / "exports" / db_path.stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        available = list_tables(conn)
        targets = tables or available
        unknown = [t for t in targets if t not in available]
        if unknown:
            raise SystemExit(
                f"Unknown table(s): {unknown}. Available: {available}"
            )

        logger.info(f"Exporting {len(targets)} table(s) from {db_path.name} → {out_dir}")
        total_cells = 0
        for t in targets:
            rows, cols = export_table(conn, t, out_dir)
            total_cells += rows * cols
    finally:
        conn.close()

    logger.info(f"Done. {len(targets)} CSV(s) in {out_dir}")
    if total_cells > GSHEETS_CELL_LIMIT:
        logger.warning(
            f"Combined size is {total_cells:,} cells — over Google Sheets' "
            f"{GSHEETS_CELL_LIMIT:,}-cell-per-spreadsheet limit. Put the largest "
            "table (likely draft_picks) in its own spreadsheet, or keep that one "
            "in SQLite/BigQuery."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite file to export (default: {DEFAULT_DB.name})")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output dir (default: data/exports/<db-name>/)")
    parser.add_argument("--tables", nargs="+", default=None,
                        help="Subset of tables (default: all)")
    args = parser.parse_args()
    main(db_path=args.db, out_dir=args.out, tables=args.tables)
