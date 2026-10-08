from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

# Only used if the policy file has no parseable reference date.
FALLBACK_REFERENCE_DATE = date(2026, 9, 30)


def _read_csv(name: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / name)
    # Blank cells come back as NaN, and NaN is truthy, so checks like
    # `if row["manager_id"]:` or date parsing silently go wrong. Use None instead.
    return df.astype(object).where(df.notna(), None)


def load_employees() -> pd.DataFrame:
    return _read_csv("employees.csv")


def load_budgets() -> pd.DataFrame:
    return _read_csv("department_budgets.csv")


def load_software_catalog() -> pd.DataFrame:
    return _read_csv("software_catalog.csv")


def load_vendors() -> pd.DataFrame:
    return _read_csv("vendors.csv")


def load_purchase_history() -> pd.DataFrame:
    return _read_csv("purchase_history.csv")


def records(df: pd.DataFrame) -> list[dict]:
    return df.to_dict(orient="records")


def load_requests() -> list[dict]:
    return json.loads((DATA_DIR / "requests.json").read_text(encoding="utf-8"))


def get_request(request_id: str) -> dict:
    for request in load_requests():
        if request["request_id"] == request_id:
            return request
    raise KeyError(f"Unknown request_id: {request_id}")


def load_policy_text() -> str:
    return (DATA_DIR / "procurement_policy.md").read_text(encoding="utf-8")


def policy_reference_date() -> date:
    """Snapshot date that all date-based checks must use (never date.today())."""
    match = re.search(r"reference date:\**\s*(\d{4}-\d{2}-\d{2})", load_policy_text(), re.IGNORECASE)
    if match:
        return date.fromisoformat(match.group(1))
    return FALLBACK_REFERENCE_DATE


def policy_sections() -> dict[int, str]:
    """Split the policy markdown into {section_number: text}."""
    sections: dict[int, str] = {}
    current: int | None = None
    lines: list[str] = []
    for line in load_policy_text().splitlines():
        heading = re.match(r"^##\s+(\d+)\.\s", line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = int(heading.group(1))
            lines = [line]
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections
