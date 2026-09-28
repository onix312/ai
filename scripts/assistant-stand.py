"""Create an isolated synthetic PrintFlow/agent stand; optionally run focused checks.

The script never reads the user's working database or sends printer commands.
It sets APPDATA and PRINTFLOW_ASSISTANT_DB before importing project modules.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", action="store_true", help="run focused assistant tests on the stand")
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[1]
    original_appdata = Path(os.environ.get("APPDATA") or Path.home()).resolve()
    root = Path(tempfile.mkdtemp(prefix="printflow-assistant-stand-", dir=tempfile.gettempdir())).resolve()
    if root == original_appdata or original_appdata in root.parents:
        raise RuntimeError("Стенд оказался внутри рабочего каталога данных")

    env = os.environ.copy()
    env["APPDATA"] = str(root)
    env["PRINTFLOW_ASSISTANT_DB"] = str(root / "agent" / "assistant.db")
    env["PRINTFLOW_URL"] = "http://127.0.0.1:0"
    env["PRINTFLOW_MODEL_URL"] = "http://127.0.0.1:0"
    # The first imports must see the isolated paths. This process only creates
    # synthetic databases; the optional tests run in a child with the same env.
    os.environ.update({key: env[key] for key in (
        "APPDATA", "PRINTFLOW_ASSISTANT_DB", "PRINTFLOW_URL", "PRINTFLOW_MODEL_URL")})
    sys.path.insert(0, str(project))

    from connector.printflow.config import DB_FILE
    from connector.printflow.db import Database, backup_database_file
    from agent.store import Store

    panel = Database()
    agent = Store()
    panel_path = Path(panel.path).resolve()
    agent_path = Path(agent.path).resolve()
    if root not in panel_path.parents or root not in agent_path.parents:
        raise RuntimeError("База стенда вышла за пределы временной папки")

    backup = root / "backup" / "panel.sqlite3"
    backup.parent.mkdir(parents=True)
    backup_database_file(panel_path, backup)
    restored = root / "restored" / "panel.sqlite3"
    restored.parent.mkdir(parents=True)
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as target:
        source.backup(target)
    with sqlite3.connect(restored) as restored_db:
        integrity = restored_db.execute("PRAGMA integrity_check").fetchone()[0]
        table_count = restored_db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    panel.close()
    agent.close()
    if integrity != "ok" or table_count < 1 or panel_path != DB_FILE.resolve():
        raise RuntimeError("Восстановленная копия стенда не прошла проверку")

    report = {"kind": "synthetic-stand", "root": str(root), "panel_db": str(panel_path),
              "agent_db": str(agent_path), "backup": str(backup), "restored": str(restored),
              "integrity": integrity, "tables": table_count, "production_read": False,
              "printer_commands": False}
    (root / "stand.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.tests:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "connector.tests.test_assistant_brain",
             "connector.tests.test_agent_brain", "-q"],
            cwd=project, env=env, check=False)
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
