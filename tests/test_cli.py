import os
import subprocess
import sys
from pathlib import Path

PY = sys.executable

def run(*args, env=None):
    """inventory_cli.py をサブプロセスで実行し、失敗なら詳細付きで失敗させる"""
    result = subprocess.run([PY, "inventory_cli.py", *args],
                            text=True, capture_output=True, env=env)
    if result.returncode != 0:
        raise AssertionError(
            f"cmd failed: {args}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout

def test_basic_flow(tmp_path: Path):
    # テスト専用のDBに切り替え（リポジトリ内のDBに影響させない）
    env = os.environ.copy()
    env["INVENTORY_DB"] = str(tmp_path / "test.db")

    # 初期化
    run("init", env=env)

    # 品目登録
    run("add-item", "--sku", "A-001", "--name", "ネジ M3", "--unit", "袋", "--min-qty", "50", env=env)

    # 入庫 → 出庫
    run("in",  "--sku", "A-001", "--qty", "120", "--reason", "仕入", env=env)
    run("out", "--sku", "A-001", "--qty", "30",  "--reason", "出荷", env=env)

    # 現在庫が 90 になっていることを確認
    out = run("stock", "--sku", "A-001", env=env)
    assert "現在庫=90" in out

    # 一覧にもSKUが出る
    out = run("list", env=env)
    assert "A-001" in out
