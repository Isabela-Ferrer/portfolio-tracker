"""Shared fixtures for the source-authority test suite.

Every test that touches the database repoints database.DB_PATH at a fresh
temporary file before doing anything else (acceptance criterion 28: repointing
DB_PATH is honoured and data/tracker.db is never touched by the test suite).
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest

import database as db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point database.DB_PATH at an empty temp file and initialise it."""
    db_path = tmp_path / "test_tracker.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init_db()
    db._migrate_db()
    yield db
