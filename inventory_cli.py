#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
シンプル在庫登録システム (SQLite / CLI)
-----------------------------------------
依存:
  - Python 3.9+
  - 外部ライブラリ不要 (標準ライブラリのみ)

機能:
  - 品目登録/更新/削除
  - 入庫/出庫の登録 (取引履歴を保持)
  - 在庫数の参照 (集計)
  - 品目一覧・検索
  - CSVインポート(品目) / エクスポート(現在庫)
  - しきい値(最小在庫)アラート

使い方(例):
  python inventory_cli.py init
  python inventory_cli.py add-item --sku A-001 --name "ネジ M3" --unit 袋 --min-qty 50
  python inventory_cli.py in  --sku A-001 --qty 120 --reason 仕入 --ref PO-2025-0001
  python inventory_cli.py out --sku A-001 --qty 30  --reason 出荷 --ref SO-2025-1001
  python inventory_cli.py stock --sku A-001
  python inventory_cli.py list
  python inventory_cli.py history --sku A-001 --limit 20
  python inventory_cli.py export-csv stocks.csv
  python inventory_cli.py import-items items.csv

CSVフォーマット:
  import-items: ヘッダ行あり -> sku,name,unit,min_qty
  export-csv:   現在庫 -> sku,name,unit,qty,min_qty,below_min(bool)

注意:
  - 出庫は在庫マイナスを禁止(デフォルト)。--allow-negative で許可可能。
"""

from __future__ import annotations
import argparse
import csv
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Tuple

DB_PATH = os.environ.get("INVENTORY_DB", "inventory.db")

# -----------------------------
# DB Utilities
# -----------------------------

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT 'pcs',
    min_qty     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock_moves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL,
    change_qty  INTEGER NOT NULL, -- 入庫:+, 出庫:-
    reason      TEXT,
    ref         TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);

-- 高速化用インデックス
CREATE INDEX IF NOT EXISTS idx_items_sku ON items(sku);
CREATE INDEX IF NOT EXISTS idx_moves_item_id_at ON stock_moves(item_id, at);
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.executescript(SCHEMA_SQL)


# -----------------------------
# Data Access Layer
# -----------------------------

@dataclass
class Item:
    id: int
    sku: str
    name: str
    unit: str
    min_qty: int


def get_item_by_sku(conn: sqlite3.Connection, sku: str) -> Optional[Item]:
    row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
    if not row:
        return None
    return Item(id=row["id"], sku=row["sku"], name=row["name"], unit=row["unit"], min_qty=row["min_qty"])


def upsert_item(conn: sqlite3.Connection, sku: str, name: str, unit: str, min_qty: int) -> Item:
    existing = get_item_by_sku(conn, sku)
    if existing:
        conn.execute(
            "UPDATE items SET name = ?, unit = ?, min_qty = ?, updated_at = datetime('now') WHERE sku = ?",
            (name, unit, min_qty, sku),
        )
        item = get_item_by_sku(conn, sku)
        assert item is not None
        return item
    else:
        cur = conn.execute(
            "INSERT INTO items (sku, name, unit, min_qty) VALUES (?, ?, ?, ?)",
            (sku, name, unit, min_qty),
        )
        item_id = cur.lastrowid
        return Item(id=item_id, sku=sku, name=name, unit=unit, min_qty=min_qty)


def delete_item(conn: sqlite3.Connection, sku: str) -> bool:
    with conn:
        res = conn.execute("DELETE FROM items WHERE sku = ?", (sku,))
        return res.rowcount > 0


def add_move(conn: sqlite3.Connection, item: Item, change_qty: int, reason: str = "", ref: str = "", at: Optional[str] = None) -> int:
    at = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO stock_moves (item_id, change_qty, reason, ref, at) VALUES (?, ?, ?, ?, ?)",
        (item.id, change_qty, reason, ref, at),
    )
    return cur.lastrowid


def get_stock(conn: sqlite3.Connection, item: Item) -> int:
    row = conn.execute("SELECT COALESCE(SUM(change_qty), 0) AS qty FROM stock_moves WHERE item_id = ?", (item.id,)).fetchone()
    return int(row["qty"]) if row else 0


def list_items_with_stock(conn: sqlite3.Connection) -> Iterable[Tuple[Item, int]]:
    rows = conn.execute(
        """
        SELECT i.*, COALESCE(SUM(m.change_qty), 0) AS qty
        FROM items i
        LEFT JOIN stock_moves m ON m.item_id = i.id
        GROUP BY i.id
        ORDER BY i.sku
        """
    ).fetchall()
    for r in rows:
        yield Item(id=r["id"], sku=r["sku"], name=r["name"], unit=r["unit"], min_qty=r["min_qty"]), int(r["qty"]) 


def iter_history(conn: sqlite3.Connection, item: Item, limit: int = 50) -> Iterable[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM stock_moves WHERE item_id = ? ORDER BY at DESC, id DESC LIMIT ?",
        (item.id, limit),
    ).fetchall()
    return rows

# -----------------------------
# Business Logic
# -----------------------------

def ensure_stock_for_out(conn: sqlite3.Connection, item: Item, qty: int, allow_negative: bool = False) -> None:
    current = get_stock(conn, item)
    if not allow_negative and current - qty < 0:
        raise ValueError(f"在庫不足: SKU={item.sku} 現在庫={current} 要求出庫={qty}")


def register_in(conn: sqlite3.Connection, sku: str, qty: int, reason: str = "", ref: str = "") -> int:
    if qty <= 0:
        raise ValueError("入庫数量は正の整数で指定してください")
    item = get_item_by_sku(conn, sku)
    if not item:
        raise KeyError(f"SKUが存在しません: {sku}")
    return add_move(conn, item, change_qty=qty, reason=reason or "入庫", ref=ref)


def register_out(conn: sqlite3.Connection, sku: str, qty: int, reason: str = "", ref: str = "", allow_negative: bool = False) -> int:
    if qty <= 0:
        raise ValueError("出庫数量は正の整数で指定してください")
    item = get_item_by_sku(conn, sku)
    if not item:
        raise KeyError(f"SKUが存在しません: {sku}")
    ensure_stock_for_out(conn, item, qty, allow_negative)
    return add_move(conn, item, change_qty=-qty, reason=reason or "出庫", ref=ref)


# -----------------------------
# CSV I/O
# -----------------------------

def import_items_csv(conn: sqlite3.Connection, path: str) -> int:
    """ヘッダ: sku,name,unit,min_qty"""
    count = 0
    with open(path, newline='', encoding='utf-8') as f, conn:
        reader = csv.DictReader(f)
        required = {"sku", "name", "unit", "min_qty"}
        if set(reader.fieldnames or []) < required:
            raise ValueError(f"CSVヘッダが不足しています。必要: {required}")
        for row in reader:
            sku = row["sku"].strip()
            name = row["name"].strip()
            unit = (row.get("unit") or "pcs").strip() or "pcs"
            try:
                min_qty = int(row.get("min_qty") or 0)
            except Exception:
                raise ValueError(f"min_qty が整数ではありません (sku={sku}): {row.get('min_qty')}")
            upsert_item(conn, sku, name, unit, min_qty)
            count += 1
    return count


def export_stocks_csv(conn: sqlite3.Connection, path: str) -> int:
    fields = ["sku", "name", "unit", "qty", "min_qty", "below_min"]
    count = 0
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item, qty in list_items_with_stock(conn):
            below = qty < item.min_qty
            writer.writerow({
                "sku": item.sku,
                "name": item.name,
                "unit": item.unit,
                "qty": qty,
                "min_qty": item.min_qty,
                "below_min": str(below).lower(),
            })
            count += 1
    return count

# -----------------------------
# CLI
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="在庫登録システム (SQLite/CLI)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="DB初期化")

    ap = sub.add_parser("add-item", help="品目の新規/更新登録")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--unit", default="pcs")
    ap.add_argument("--min-qty", type=int, default=0)

    dp = sub.add_parser("delete-item", help="品目削除")
    dp.add_argument("--sku", required=True)

    ip = sub.add_parser("in", help="入庫登録")
    ip.add_argument("--sku", required=True)
    ip.add_argument("--qty", required=True, type=int)
    ip.add_argument("--reason", default="入庫")
    ip.add_argument("--ref", default="")

    op = sub.add_parser("out", help="出庫登録")
    op.add_argument("--sku", required=True)
    op.add_argument("--qty", required=True, type=int)
    op.add_argument("--reason", default="出庫")
    op.add_argument("--ref", default="")
    op.add_argument("--allow-negative", action="store_true", help="在庫マイナスを許可")

    sp = sub.add_parser("stock", help="現在庫を表示")
    sp.add_argument("--sku", required=True)

    lp = sub.add_parser("list", help="品目一覧(現在庫つき)")

    hp = sub.add_parser("history", help="入出庫履歴を表示")
    hp.add_argument("--sku", required=True)
    hp.add_argument("--limit", type=int, default=50)

    exp = sub.add_parser("export-csv", help="現在庫をCSV出力")
    exp.add_argument("path")

    imp = sub.add_parser("import-items", help="品目CSVをインポート")
    imp.add_argument("path")

    return p


def cmd_init(args: argparse.Namespace) -> None:
    init_db()
    print(f"初期化完了: {DB_PATH}")


def cmd_add_item(args: argparse.Namespace) -> None:
    with closing(connect()) as conn, conn:
        item = upsert_item(conn, args.sku, args.name, args.unit, args.min_qty)
        print(f"登録/更新しました: SKU={item.sku} 名称={item.name} 単位={item.unit} 最小在庫={item.min_qty}")


def cmd_delete_item(args: argparse.Namespace) -> None:
    with closing(connect()) as conn, conn:
        ok = delete_item(conn, args.sku)
        if ok:
            print(f"削除しました: SKU={args.sku}")
        else:
            print(f"見つかりません: SKU={args.sku}")


def cmd_in(args: argparse.Namespace) -> None:
    with closing(connect()) as conn, conn:
        try:
            move_id = register_in(conn, args.sku, args.qty, args.reason, args.ref)
            item = get_item_by_sku(conn, args.sku)
            assert item is not None
            qty = get_stock(conn, item)
            print(f"入庫登録OK id={move_id} / SKU={args.sku} 現在庫={qty}")
        except Exception as e:
            print(f"エラー: {e}", file=sys.stderr)
            sys.exit(1)


def cmd_out(args: argparse.Namespace) -> None:
    with closing(connect()) as conn, conn:
        try:
            move_id = register_out(conn, args.sku, args.qty, args.reason, args.ref, args.allow_negative)
            item = get_item_by_sku(conn, args.sku)
            assert item is not None
            qty = get_stock(conn, item)
            warn = " *最小在庫割れ*" if qty < item.min_qty else ""
            print(f"出庫登録OK id={move_id} / SKU={args.sku} 現在庫={qty}{warn}")
        except Exception as e:
            print(f"エラー: {e}", file=sys.stderr)
            sys.exit(1)


def cmd_stock(args: argparse.Namespace) -> None:
    with closing(connect()) as conn:
        item = get_item_by_sku(conn, args.sku)
        if not item:
            print(f"SKUが見つかりません: {args.sku}", file=sys.stderr)
            sys.exit(1)
        qty = get_stock(conn, item)
        print(f"SKU={item.sku} 名称={item.name} 現在庫={qty} {item.unit} (最小在庫={item.min_qty})")


def cmd_list(args: argparse.Namespace) -> None:
    with closing(connect()) as conn:
        print("SKU, 名称, 現在庫, 単位, 最小在庫, 注意")
        for item, qty in list_items_with_stock(conn):
            alert = "LOW" if qty < item.min_qty else ""
            print(f"{item.sku}, {item.name}, {qty}, {item.unit}, {item.min_qty}, {alert}")


def cmd_history(args: argparse.Namespace) -> None:
    with closing(connect()) as conn:
        item = get_item_by_sku(conn, args.sku)
        if not item:
            print(f"SKUが見つかりません: {args.sku}", file=sys.stderr)
            sys.exit(1)
        print(f"履歴 (最新 {args.limit} 件): SKU={item.sku} {item.name}")
        for r in iter_history(conn, item, args.limit):
            sign = "+" if r["change_qty"] >= 0 else ""
            print(f"{r['at']}  {sign}{r['change_qty']}	{r['reason'] or ''}	{r['ref'] or ''}")


def cmd_export(args: argparse.Namespace) -> None:
    with closing(connect()) as conn:
        count = export_stocks_csv(conn, args.path)
        print(f"エクスポート完了: {args.path} (件数={count})")


def cmd_import(args: argparse.Namespace) -> None:
    with closing(connect()) as conn:
        try:
            count = import_items_csv(conn, args.path)
            print(f"インポート完了: {args.path} (登録/更新 {count} 件)")
        except Exception as e:
            print(f"エラー: {e}", file=sys.stderr)
            sys.exit(1)


CMD_TABLE = {
    "init": cmd_init,
    "add-item": cmd_add_item,
    "delete-item": cmd_delete_item,
    "in": cmd_in,
    "out": cmd_out,
    "stock": cmd_stock,
    "list": cmd_list,
    "history": cmd_history,
    "export-csv": cmd_export,
    "import-items": cmd_import,
}


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = CMD_TABLE.get(args.cmd)
    if not handler:
        parser.print_help()
        return 2
    handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
