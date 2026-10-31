"""Configuration loader: config.yaml (business rules) + .env (secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    cfg: dict[str, Any]
    llm_provider: str = "none"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    send_mode: str = "dry_run"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_email: str = ""
    test_recipient: str = ""
    db_path: Path = field(default_factory=lambda: ROOT / "outputs" / "outreach.db")
    data_csv: Path = field(default_factory=lambda: ROOT / "data" / "influencer_dataset_50.csv")
    out_dir: Path = field(default_factory=lambda: ROOT / "outputs")

    def section(self, name: str) -> dict[str, Any]:
        return self.cfg.get(name, {})


def load_settings(config_path: Path | None = None) -> Settings:
    config_path = config_path or ROOT / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    db_path = Path(os.getenv("DB_PATH", "outputs/outreach.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    return Settings(
        cfg=cfg,
        llm_provider=os.getenv("LLM_PROVIDER", "none").strip().lower() or "none",
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip(),
        send_mode=os.getenv("SEND_MODE", "dry_run").strip().lower() or "dry_run",
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587") or 587),
        smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_password=os.getenv("SMTP_APP_PASSWORD", "").strip(),
        from_email=os.getenv("FROM_EMAIL", "").strip(),
        test_recipient=os.getenv("TEST_RECIPIENT", "").strip(),
        db_path=db_path,
        out_dir=ROOT / "outputs",
        data_csv=ROOT / "data" / "influencer_dataset_50.csv",
    )
