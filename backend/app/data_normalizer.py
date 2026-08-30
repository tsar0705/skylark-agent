"""
Normalizes raw data pulled from monday.com into clean pandas DataFrames,
and tracks data-quality issues so the agent can surface caveats to the user
instead of silently presenting incomplete numbers as if they were complete.

Rules here were derived by inspecting the actual source files
(Work_Order_Tracker_Data.xlsx / Deal_funnel_Data.xlsx) supplied with the
assignment. Known issues handled:

  - Blank strings instead of real nulls ("" vs None)
  - Duplicate embedded header rows (someone pasted the header row again
    partway through the sheet, e.g. a Deal Stage cell literally containing
    the text "Deal Stage")
  - Inconsistent casing / spelling in status fields ("BIlled" vs "Billed")
  - Quantity fields mixing a number with a unit ("5360 HA" vs "3000")
  - Money fields as plain numbers but occasionally arriving as strings
    with commas / currency symbols once round-tripped through monday.com
  - Sector / stage naming that needs canonicalizing for grouping
"""
import re
from dataclasses import dataclass, field

import pandas as pd

# ----------------------------------------------------------------------
# Canonicalization tables
# ----------------------------------------------------------------------

STATUS_CASING_FIXES = {
    "billed": "Billed",
    "bopen": "Open",
}

SECTOR_CANONICAL = {
    "mining": "Mining",
    "powerline": "Powerline",
    "renewables": "Renewables",
    "railways": "Railways",
    "construction": "Construction",
    "others": "Others",
    "tender": "Tender",
    "dsp": "DSP",
    "security and surveillance": "Security and Surveillance",
    "aviation": "Aviation",
    "manufacturing": "Manufacturing",
}

QUANTITY_PATTERN = re.compile(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z%]*)")


@dataclass
class DataQualityReport:
    """Accumulates issues found while normalizing one dataset."""

    dataset_name: str
    total_rows_seen: int = 0
    rows_dropped_as_header_dupes: int = 0
    null_counts: dict = field(default_factory=dict)
    coercion_failures: dict = field(default_factory=dict)  # column -> count

    def null_pct(self, column: str, total_rows: int) -> float:
        if total_rows == 0:
            return 0.0
        return round(100 * self.null_counts.get(column, 0) / total_rows, 1)

    def summary_lines(self, total_rows: int, key_columns: list[str]) -> list[str]:
        lines = []
        if self.rows_dropped_as_header_dupes:
            lines.append(
                f"Dropped {self.rows_dropped_as_header_dupes} duplicate/header-like "
                f"row(s) found embedded in the {self.dataset_name} data."
            )
        for col in key_columns:
            pct = self.null_pct(col, total_rows)
            if pct >= 15:
                lines.append(
                    f"'{col}' is missing in {pct}% of {self.dataset_name} rows - "
                    f"treat aggregates on this field as directional, not exact."
                )
        for col, count in self.coercion_failures.items():
            if count:
                lines.append(
                    f"{count} value(s) in '{col}' ({self.dataset_name}) could not be "
                    f"parsed as expected and were treated as missing."
                )
        return lines


def _blank_to_none(v):
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


def _looks_like_header_row(row: dict, header_values: set[str]) -> bool:
    """True if >= 2 cells in a row literally equal their own column name -
    a strong signal a header row got pasted into the data by mistake."""
    hits = 0
    for k, v in row.items():
        if isinstance(v, str) and v.strip() == k.strip():
            hits += 1
    return hits >= 2


def _parse_money(v, report: DataQualityReport, col: str):
    v = _blank_to_none(v)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        report.coercion_failures[col] = report.coercion_failures.get(col, 0) + 1
        return None


def _parse_quantity(v, report: DataQualityReport, col: str):
    """Splits a mixed 'number + unit' quantity field, e.g. '5360 HA' -> (5360.0, 'HA')."""
    v = _blank_to_none(v)
    if v is None:
        return None, None
    if isinstance(v, (int, float)):
        return float(v), None
    m = QUANTITY_PATTERN.match(str(v).strip())
    if not m:
        report.coercion_failures[col] = report.coercion_failures.get(col, 0) + 1
        return None, None
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2) or None
    return num, unit


def _parse_date(v):
    v = _blank_to_none(v)
    if v is None:
        return pd.NaT
    return pd.to_datetime(v, errors="coerce", dayfirst=False)


def _canon_sector(v):
    v = _blank_to_none(v)
    if v is None:
        return None
    key = str(v).strip().lower()
    return SECTOR_CANONICAL.get(key, str(v).strip())


def _canon_status(v):
    v = _blank_to_none(v)
    if v is None:
        return None
    s = str(v).strip()
    return STATUS_CASING_FIXES.get(s.lower(), s)


# ----------------------------------------------------------------------
# Work Orders
# ----------------------------------------------------------------------

WORK_ORDER_MONEY_COLS = [
    "Amount in Rupees (Excl of GST) (Masked)",
    "Amount in Rupees (Incl of GST) (Masked)",
    "Billed Value in Rupees (Excl of GST.) (Masked)",
    "Billed Value in Rupees (Incl of GST.) (Masked)",
    "Collected Amount in Rupees (Incl of GST.) (Masked)",
    "Amount to be billed in Rs. (Exl. of GST) (Masked)",
    "Amount to be billed in Rs. (Incl. of GST) (Masked)",
    "Amount Receivable (Masked)",
]

WORK_ORDER_DATE_COLS = [
    "Data Delivery Date",
    "Date of PO/LOI",
    "Probable Start Date",
    "Probable End Date",
    "Last invoice date",
    "Collection Date",
]

WORK_ORDER_KEY_COLUMNS_FOR_CAVEATS = [
    "Sector",
    "Execution Status",
    "Amount in Rupees (Incl of GST) (Masked)",
    "Invoice Status",
    "WO Status (billed)",
]


def normalize_work_orders(raw_items: list[dict]) -> tuple[pd.DataFrame, DataQualityReport]:
    report = DataQualityReport(dataset_name="work orders")
    report.total_rows_seen = len(raw_items)

    header_names = set(raw_items[0].keys()) if raw_items else set()
    cleaned_rows = []
    for row in raw_items:
        row = {k: _blank_to_none(v) for k, v in row.items()}
        if _looks_like_header_row(row, header_names):
            report.rows_dropped_as_header_dupes += 1
            continue
        cleaned_rows.append(row)

    df = pd.DataFrame(cleaned_rows)
    if df.empty:
        return df, report

    for col in WORK_ORDER_MONEY_COLS:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _parse_money(v, report, col))

    for col in WORK_ORDER_DATE_COLS:
        if col in df.columns:
            df[col] = df[col].apply(_parse_date)

    if "Sector" in df.columns:
        df["Sector"] = df["Sector"].apply(_canon_sector)
    for col in ["Execution Status", "Invoice Status", "Billing Status", "WO Status (billed)", "Collection status"]:
        if col in df.columns:
            df[col] = df[col].apply(_canon_status)

    if "Quantity by Ops" in df.columns:
        parsed = df["Quantity by Ops"].apply(lambda v: _parse_quantity(v, report, "Quantity by Ops"))
        df["Quantity by Ops (numeric)"] = parsed.apply(lambda p: p[0])
        df["Quantity by Ops (unit)"] = parsed.apply(lambda p: p[1])

    for col in WORK_ORDER_KEY_COLUMNS_FOR_CAVEATS:
        if col in df.columns:
            report.null_counts[col] = int(df[col].isna().sum())

    return df, report


# ----------------------------------------------------------------------
# Deals
# ----------------------------------------------------------------------

DEAL_DATE_COLS = ["Close Date (A)", "Tentative Close Date", "Created Date"]
DEAL_MONEY_COLS = ["Masked Deal value"]

DEAL_KEY_COLUMNS_FOR_CAVEATS = [
    "Deal Status",
    "Closure Probability",
    "Masked Deal value",
    "Sector/service",
    "Tentative Close Date",
]


def normalize_deals(raw_items: list[dict]) -> tuple[pd.DataFrame, DataQualityReport]:
    report = DataQualityReport(dataset_name="deals")
    report.total_rows_seen = len(raw_items)

    header_names = set(raw_items[0].keys()) if raw_items else set()
    cleaned_rows = []
    for row in raw_items:
        row = {k: _blank_to_none(v) for k, v in row.items()}
        if _looks_like_header_row(row, header_names):
            report.rows_dropped_as_header_dupes += 1
            continue
        if row.get("Deal Name") is None and row.get("Client Code") is None:
            # fully blank spacer row
            report.rows_dropped_as_header_dupes += 1
            continue
        cleaned_rows.append(row)

    df = pd.DataFrame(cleaned_rows)
    if df.empty:
        return df, report

    for col in DEAL_MONEY_COLS:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: _parse_money(v, report, col))

    for col in DEAL_DATE_COLS:
        if col in df.columns:
            df[col] = df[col].apply(_parse_date)

    if "Sector/service" in df.columns:
        df["Sector/service"] = df["Sector/service"].apply(_canon_sector)
    for col in ["Deal Status", "Closure Probability", "Deal Stage"]:
        if col in df.columns:
            df[col] = df[col].apply(_canon_status)

    for col in DEAL_KEY_COLUMNS_FOR_CAVEATS:
        if col in df.columns:
            report.null_counts[col] = int(df[col].isna().sum())

    return df, report
