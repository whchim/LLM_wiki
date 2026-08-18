"""pytest 公共配置：测试隔离目录不依赖系统 %TEMP%。

背景：内置 tmp_path 默认落在系统临时目录（%TEMP%\\pytest-of-<user>），
在受限沙箱 / CI / 只读系统盘等环境下会被 ACL 拒绝（PermissionError），
导致整套测试无法运行。此处将 tmp_path 覆盖重定向到工作区内
tests/_isolated/ 下，任何环境均可读写。
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "streamlit_app"))

ISOLATED = ROOT / "tests" / "_isolated"


@pytest.fixture(scope="session")
def _isolated_root() -> Path:
    """工作区内隔离根目录：session 开始时整体重建，保证干净起点。"""
    if ISOLATED.exists():
        shutil.rmtree(ISOLATED)
    ISOLATED.mkdir(parents=True)
    yield ISOLATED
    # 结束后保留目录便于调试；如需自动清理，放开下一行：
    # shutil.rmtree(ISOLATED)


@pytest.fixture
def tmp_path(_isolated_root: Path, request) -> Path:
    """覆盖内置 tmp_path：每个测试函数一个独立子目录，保证相互隔离。"""
    name = request.node.name.replace("/", "_").replace("\\", "_")
    p = _isolated_root / name
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)
    return p


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "vault"))
