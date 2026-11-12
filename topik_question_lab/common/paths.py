"""Stable source resources and the configurable local data root."""
import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
MIGRATION_DIR = PACKAGE_ROOT / "maintenance/migrations"
TARGET_BANK_SCHEMA = PACKAGE_ROOT / "maintenance/target_bank.sql"


def application_root():
    return Path(os.getenv("TOPIK_LAB_ROOT", str(PROJECT_ROOT))).resolve()
