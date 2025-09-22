import os
import subprocess
import sys
import re

from pathlib import Path
PY = sys.executable

def run_ok(*args, env=None):
    r = subprocess.run([PY, "inventory_cli.py", *args],
                       text=True, capture_output=True, env=env)
    assert r.returncode == 0, f"failed: {args}\nstdout={r.stdout}\nstderr={r.stderr}"
    return r.stdout

def run_ng(*args, env=None):
    r = subprocess.run([PY, "inventory_cli.py", *args],
                       text=True, capture_output=True, env=env)
    assert r.returncode != 0, f"should fail: {args}"
    return r.stderr or r.stdout

def fresh_env(tmp_path: Path):
    env = os.environ.copy()
    env["INVENTORY_DB"] = str(tmp_path / "test.db")  # テストごとにDB分離
    return env

def test_negative_out_is_blocked(tmp_path: Path):
    env = fresh_env(tmp_path)
    run_ok("init", env=env)
    run_ok("add-item", "--sku", "X-1", "--name", "テスト品", "--unit", "個", "--min-qty", "0", env=env)
    err = run_ng("out", "--sku", "X-1", "--qty", "1", env=env)  # 在庫0で出庫→失敗
    assert "在庫不足" in err

def test_allow_negative_flag(tmp_path: Path):
    env = fresh_env(tmp_path)
    run_ok("init", env=env)
    run_ok("add-item", "--sku", "X-2", "--name", "テスト品", "--unit", "個", "--min-qty", "0", env=env)
    run_ok("out", "--sku", "X-2", "--qty", "2", "--allow-negative", env=env)  # 許可ならOK
    out = run_ok("stock", "--sku", "X-2", env=env)
    assert "現在庫=-2" in out

def test_min_qty_alert_on_list(tmp_path: Path):
    env = fresh_env(tmp_path)
    run_ok("init", env=env)
    run_ok("add-item", "--sku", "A-LOW", "--name", "下限テスト", "--unit", "袋", "--min-qty", "10", env=env)
    run_ok("in", "--sku", "A-LOW", "--qty", "5", env=env)  # 下限未満
    listing = run_ok("list", env=env)
    assert re.search(r"A-LOW.*LOW", listing)  # LOW表示

def test_history_order_and_sign(tmp_path: Path):
    env = fresh_env(tmp_path)
    run_ok("init", env=env)
    run_ok("add-item", "--sku", "H-1", "--name", "履歴品", "--unit", "個", "--min-qty", "0", env=env)
    run_ok("in",  "--sku", "H-1", "--qty", "3", env=env)
    run_ok("out", "--sku", "H-1", "--qty", "1", env=env)
    hist = run_ok("history", "--sku", "H-1", "--limit", "5", env=env)
    assert "\n-1" in hist and "\n+3" in hist

def test_csv_export_and_import(tmp_path: Path):
    env = fresh_env(tmp_path)
    run_ok("init", env=env)
    items_csv = tmp_path / "items.csv"
    items_csv.write_text("sku,name,unit,min_qty\nC-1,CSV品,箱,2\n", encoding="utf-8")
    run_ok("import-items", str(items_csv), env=env)
    run_ok("in", "--sku", "C-1", "--qty", "5", env=env)
    out_csv = tmp_path / "stocks.csv"
    run_ok("export-csv", str(out_csv), env=env)
    data = out_csv.read_text(encoding="utf-8")
    assert "C-1" in data and "false" in data  # 5>=2 → below_min=false
