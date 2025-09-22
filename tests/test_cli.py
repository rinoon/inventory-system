mkdir tests -Force | Out-Null
@'
import os, subprocess, sys
from pathlib import Path
PY = sys.executable

def run(*args, env=None):
    r = subprocess.run([PY, "inventory_cli.py", *args],
                       text=True, capture_output=True, env=env)
    if r.returncode != 0:
        raise AssertionError(f"cmd failed: {args}\n---stdout---\n{r.stdout}\n---stderr---\n{r.stderr}")
    return r.stdout

def test_basic_flow(tmp_path: Path):
    env = os.environ.copy()
    env["INVENTORY_DB"] = str(tmp_path / "test.db")
    run("init", env=env)
    run("add-item", "--sku", "A-001", "--name", "ネジ M3", "--unit", "袋", "--min-qty", "50", env=env)
    run("in",  "--sku", "A-001", "--qty", "120", "--reason", "仕入", env=env)
    run("out", "--sku", "A-001", "--qty", "30",  "--reason", "出荷", env=env)
    out = run("stock", "--sku", "A-001", env=env)
    assert "現在庫=90" in out
    out = run("list", env=env)
    assert "A-001" in out
'@ | Set-Content tests\test_cli.py -Encoding UTF8
