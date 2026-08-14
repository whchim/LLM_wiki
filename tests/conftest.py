import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
