from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_app_import_accepts_db_path_without_directory(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["DB_PATH"] = "jobs.db"
    env["PYTHONPATH"] = str(repo_root)

    result = subprocess.run(
        [sys.executable, "-c", "import app; print('import ok')"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "import ok" in result.stdout
