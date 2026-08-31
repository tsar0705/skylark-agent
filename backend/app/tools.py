"""
BI tools and monday.com data store for the Skylark Drones agent.

The agent uses structured JSON analysis requests rather than asking the LLM
to generate arbitrary Python code.

This version also provides a deterministic `leadership_summary` tool.
That tool calculates the complete leadership-report dataset locally from
the normalized pandas DataFrames, reducing Groq tool-call usage and avoiding
fragile multi-step reasoning.
"""

import json
import math
import time
from dataclasses import dataclass

import pandas as pd

from .config import settings
from .monday_client import MondayClient
from .data_normalizer import (
    normalize_work_orders,
    normalize_deals,
    DataQualityReport,
)


# ============================================================================
# Loaded data
# ============================================================================

@dataclass
class LoadedData:
    work_orders: pd.DataFrame
    deals: pd.DataFrame
    wo_report: DataQualityReport
    deal_report: DataQualityReport
    loaded_at: float


class DataStore:
    """Loads both boards from monday.com, normalizes them and caches them."""

    def __init__(self, client: MondayClient | None = None):
        self.client = client or MondayClient()
        self._loaded: LoadedData | None = None

    def load(self, force_refresh: bool = False) -> LoadedData:
        if (
            not force_refresh
            and self._loaded
            and (
                time.time() - self._loaded.loaded_at
                < settings.DATA_CACHE_TTL_SECONDS
            )
        ):
            return self._loaded

        wo_raw = self.client.get_board_items(
            settings.MONDAY_WORK_ORDERS_BOARD_ID,
            use_cache=not force_refresh,
        )

        deal_raw = self.client.get_board_items(
            settings.MONDAY_DEALS_BOARD_ID,
            use_cache=not force_refresh,
        )

        wo_df, wo_report = normalize_work_orders(wo_raw)
        deal_df, deal_report = normalize_deals(deal_raw)

        self._loaded = LoadedData(
            work_orders=wo_df,
            deals=deal_df,
            wo_report=wo_report,
            deal_report=deal_report,
            loaded_at=time.time(),
        )

        return self._loaded


# ============================================================================
# Tool schemas
# ============================================================================

TOOL_SCHEMAS = [
    {
        "name": "get_schema",
        "description": (
            "Get exact column names, data types, sample values and row counts "
            "for the work_orders and/or deals datasets. Use this when you are "
            "not certain about an exact column name. Do not invent column names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {
                    "type": "string",
                    "enum": ["work_orders", "deals", "both"],
                    "description": "Dataset whose schema should be returned.",
                }
            },
            "required": ["dataset"],
            "additionalProperties": False,
        },
    },

    {
        "name": "run_analysis",
        "description": (
            "Run a structured business-data analysis against the cleaned "
            "monday.com DataFrames.\n\n"
            "IMPORTANT: provide ONLY JSON fields defined by this schema. "
            "Never provide Python code, pandas code, expressions, SQL, or "
            "free-form executable code.\n\n"
            "Supported operations: count, sum, mean, group_sum, group_count, "
            "filter, top, distinct.\n\n"
            "Filters are optional and are applied before the operation.\n"
            "Text matching for equals/contains is case-insensitive and "
            "whitespace-insensitive.\n"
            "Use not_null when you only want populated values.\n"
            "For date filters, values may be YYYY-MM-DD or 'today'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {
                    "type": "string",
                    "enum": ["work_orders", "deals"],
                    "description": "Dataset to analyze.",
                },
                "operation": {
                    "type": "string",
                    "enum": [
                        "count",
                        "sum",
                        "mean",
                        "group_sum",
                        "group_count",
                        "filter",
                        "top",
                        "distinct",
                    ],
                    "description": "Analysis operation.",
                },
                "column": {
                    "type": "string",
                    "description": (
                        "Numeric/value column used by sum, mean, group_sum "
                        "or top."
                    ),
                },
                "group_by": {
                    "type": "string",
                    "description": (
                        "Column used to group results for group_sum or "
                        "group_count."
                    ),
                },
                "filters": {
                    "type": "array",
                    "description": "Optional filters applied before analysis.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {
                                "type": "string",
                                "description": "Exact column name.",
                            },
                            "operator": {
                                "type": "string",
                                "enum": [
                                    "equals",
                                    "not_equals",
                                    "contains",
                                    "not_contains",
                                    "greater_than",
                                    "less_than",
                                    "greater_or_equal",
                                    "less_or_equal",
                                    "is_null",
                                    "not_null",
                                ],
                            },
                            "value": {
                                "description": (
                                    "Comparison value. Use strings for text "
                                    "and ISO dates for dates."
                                ),
                            },
                        },
                        "required": ["column", "operator"],
                        "additionalProperties": False,
                    },
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of rows/groups to return.",
                },
                "order_by": {
                    "type": "string",
                    "description": (
                        "Column to sort by. For group_sum this can be the "
                        "calculated value column."
                    ),
                },
                "descending": {
                    "type": "boolean",
                    "description": "Sort descending when true.",
                },
            },
            "required": ["dataset", "operation"],
            "additionalProperties": False,
        },
    },

    {
        "name": "get_execution_attention",
        "description": (
            "Return work orders requiring execution or billing attention. "
            "Use this for questions about work orders that are Stuck, "
            "Pause / struck, have Billing Status = Stuck, or have "
            "Billing Status = Update Required. "
            "This is deterministic and should be preferred over a multi-filter "
            "run_analysis query for execution-attention questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of matching work orders to return."
                }
            },
            "additionalProperties": False,
        },
    },

    {
        "name": "get_data_quality_notes",
        "description": (
            "Get a summary of known data quality issues including missing "
            "values, duplicate/junk rows and parsing problems. Use this when "
            "a requested field has meaningful missing data or when preparing "
            "a leadership update."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },

    {
        "name": "leadership_summary",
        "description": (
            "Return a complete deterministic leadership-summary dataset "
            "computed directly from the normalized monday.com DataFrames. "
            "Use this for leadership updates instead of making many separate "
            "run_analysis calls. It includes pipeline value, won value, "
            "active work orders, billed/collected values, sector performance, "
            "pipeline stages, execution risks, receivables and data coverage. "
            "Do not invent dates, terminology, sectors, statuses or metrics "
            "that are not present in the returned result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


# ============================================================================
# Errors
# ============================================================================

class ToolExecutionError(Exception):
    pass


# ============================================================================
# Utility helpers
# ============================================================================

def _json_default(value):
    """Convert pandas/numpy values into JSON-safe values."""

    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.strftime("%Y-%m-%d")

    try:
        import numpy as np

        if isinstance(value, np.integer):
            return int(value)

        if isinstance(value, np.floating):
            if np.isnan(value) or np.isinf(value):
                return None
            return float(value)

        if isinstance(value, np.bool_):
            return bool(value)

    except ImportError:
        pass

    if value is pd.NA:
        return None

    if value is None:
        return None

    try:
        if isinstance(value, float) and (
            math.isnan(value) or math.isinf(value)
        ):
            return None
    except Exception:
        pass

    return str(value)


def _json_result(value):
    """Convert a result to a clean JSON string."""

    if isinstance(value, pd.DataFrame):
        value = value.head(200).to_dict(orient="records")

    elif isinstance(value, pd.Series):
        value = value.head(200).to_dict()

    return json.dumps(
        {"result": value},
        default=_json_default,
        allow_nan=False,
    )


def _require_column(df: pd.DataFrame, column: str):
    """Ensure a requested column exists."""

    if not column:
        raise ToolExecutionError(
            "A column name is required for this operation."
        )

    if column not in df.columns:
        similar = [
            str(c)
            for c in df.columns
            if column.lower() in str(c).lower()
            or str(c).lower() in column.lower()
        ][:10]

        message = f"Column not found: {column!r}."

        if similar:
            message += f" Similar columns: {similar}"

        raise ToolExecutionError(message)


def _parse_date(value):
    """Parse a date filter value."""

    if value is None:
        return pd.NaT

    if isinstance(value, str):
        if value.strip().lower() == "today":
            return pd.Timestamp.today().normalize()

    parsed = pd.to_datetime(value, errors="coerce")

    if pd.isna(parsed):
        raise ToolExecutionError(
            f"Could not parse date value: {value!r}"
        )

    return parsed


def _normalise_text(value):
    if value is None:
        return ""

    return str(value).strip().casefold()


def _clean_numeric(series: pd.Series) -> pd.Series:
    """
    Safely convert messy numeric columns into numbers.

    Handles:
        123
        "123"
        "1,234"
        "₹1,234"
        "$1,234"
        "(123)"
        blanks
        None
    """

    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")

    cleaned = (
        series
        .astype("string")
        .str.strip()
        .str.replace(",", "", regex=False)
        .str.replace("₹", "", regex=False)
        .str.replace("$", "", regex=False)
        .str.replace("€", "", regex=False)
        .str.replace("£", "", regex=False)
        .str.replace("%", "", regex=False)
    )

    # Accounting-style negatives: (1234) -> -1234
    cleaned = cleaned.str.replace(
        r"^\((.*)\)$",
        r"-\1",
        regex=True,
    )

    return pd.to_numeric(cleaned, errors="coerce")


def _clean_group_values(series: pd.Series) -> pd.Series:
    """
    Clean grouping fields without destroying their original display value.
    """

    result = series.astype("string").str.strip()

    result = result.replace("", pd.NA)

    return result


def _apply_filters(
    df: pd.DataFrame,
    filters: list[dict] | None,
) -> pd.DataFrame:
    """Apply structured filters safely."""

    if not filters:
        return df.copy()

    result = df.copy()

    for f in filters:
        column = f.get("column")
        operator = f.get("operator")
        value = f.get("value")

        _require_column(result, column)

        series = result[column]

        # Null checks
        if operator == "is_null":
            result = result[series.isna()]
            continue

        if operator == "not_null":
            result = result[series.notna()]
            continue

        # Datetime columns
        if pd.api.types.is_datetime64_any_dtype(series):
            parsed_value = _parse_date(value)

            if operator == "equals":
                result = result[
                    series.dt.normalize() == parsed_value.normalize()
                ]

            elif operator == "not_equals":
                result = result[
                    series.dt.normalize() != parsed_value.normalize()
                ]

            elif operator == "greater_than":
                result = result[series > parsed_value]

            elif operator == "less_than":
                result = result[series < parsed_value]

            elif operator == "greater_or_equal":
                result = result[series >= parsed_value]

            elif operator == "less_or_equal":
                result = result[series <= parsed_value]

            else:
                raise ToolExecutionError(
                    f"Unsupported date operator: {operator!r}"
                )

            continue

        # Text columns
        if (
            pd.api.types.is_object_dtype(series)
            or pd.api.types.is_string_dtype(series)
            or isinstance(series.dtype, pd.CategoricalDtype)
        ):
            normalized = (
                series
                .astype("string")
                .str.strip()
                .str.casefold()
            )

            normalized_value = _normalise_text(value)

            if operator == "equals":
                result = result[
                    normalized == normalized_value
                ]

            elif operator == "not_equals":
                result = result[
                    normalized != normalized_value
                ]

            elif operator == "contains":
                result = result[
                    normalized.str.contains(
                        normalized_value,
                        na=False,
                        regex=False,
                    )
                ]

            elif operator == "not_contains":
                result = result[
                    ~normalized.str.contains(
                        normalized_value,
                        na=False,
                        regex=False,
                    )
                ]

            elif operator in {
                "greater_than",
                "less_than",
                "greater_or_equal",
                "less_or_equal",
            }:
                numeric_series = _clean_numeric(series)

                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    raise ToolExecutionError(
                        f"Expected a numeric value for column "
                        f"{column!r}, got {value!r}."
                    )

                result = _numeric_compare(
                    result,
                    numeric_series,
                    operator,
                    numeric_value,
                )

            else:
                raise ToolExecutionError(
                    f"Unsupported operator {operator!r} "
                    f"for column {column!r}."
                )

            continue

        # Numeric columns
        numeric_series = _clean_numeric(series)

        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            raise ToolExecutionError(
                f"Expected a numeric value for column "
                f"{column!r}, got {value!r}."
            )

        result = _numeric_compare(
            result,
            numeric_series,
            operator,
            numeric_value,
        )

    return result


def _numeric_compare(
    df: pd.DataFrame,
    series: pd.Series,
    operator: str,
    value: float,
) -> pd.DataFrame:

    if operator == "equals":
        return df[series == value]

    if operator == "not_equals":
        return df[series != value]

    if operator == "greater_than":
        return df[series > value]

    if operator == "less_than":
        return df[series < value]

    if operator == "greater_or_equal":
        return df[series >= value]

    if operator == "less_or_equal":
        return df[series <= value]

    raise ToolExecutionError(
        f"Unsupported numeric operator: {operator!r}"
    )


def _find_column(df: pd.DataFrame, name: str) -> str | None:
    """Find a column using exact then case-insensitive matching."""

    if name in df.columns:
        return name

    wanted = name.strip().casefold()

    for col in df.columns:
        if str(col).strip().casefold() == wanted:
            return str(col)

    return None


def _safe_sum(df: pd.DataFrame, column: str) -> dict:
    """Return sum plus coverage information."""

    actual = _find_column(df, column)

    if actual is None:
        return {
            "column": column,
            "sum": None,
            "populated_rows": 0,
            "total_rows": len(df),
            "missing_or_non_numeric_rows": len(df),
        }

    numeric = _clean_numeric(df[actual])

    populated = int(numeric.notna().sum())

    return {
        "column": actual,
        "sum": (
            float(numeric.sum())
            if populated
            else 0.0
        ),
        "populated_rows": populated,
        "total_rows": int(len(df)),
        "missing_or_non_numeric_rows": int(
            len(df) - populated
        ),
    }


def _status_series(df: pd.DataFrame, column: str) -> pd.Series:
    actual = _find_column(df, column)

    if actual is None:
        return pd.Series(
            [""] * len(df),
            index=df.index,
            dtype="string",
        )

    return (
        df[actual]
        .astype("string")
        .str.strip()
        .str.casefold()
    )


# ============================================================================
# Tool runner
# ============================================================================

class ToolRunner:

    def __init__(self, store: DataStore):
        self.store = store

    def execute(self, tool_name: str, tool_input: dict) -> str:

        if not isinstance(tool_input, dict):
            raise ToolExecutionError(
                "Tool arguments must be a JSON object."
            )

        data = self.store.load()

        if tool_name == "get_schema":
            return self._get_schema(
                tool_input.get("dataset", "both"),
                data,
            )

        if tool_name == "run_analysis":
            return self._run_analysis(
                tool_input,
                data,
            )

        if tool_name == "get_execution_attention":
            return self._get_execution_attention(
                tool_input,
                data,
            )

        if tool_name == "get_data_quality_notes":
            return self._get_data_quality_notes(data)

        if tool_name == "leadership_summary":
            return self._leadership_summary(data)

        raise ToolExecutionError(
            f"Unknown tool: {tool_name}"
        )

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _get_schema(
        self,
        dataset: str,
        data: LoadedData,
    ) -> str:

        if dataset not in {"work_orders", "deals", "both"}:
            raise ToolExecutionError(
                "dataset must be work_orders, deals or both."
            )

        output = {}

        targets = []

        if dataset in {"work_orders", "both"}:
            targets.append(
                ("work_orders", data.work_orders)
            )

        if dataset in {"deals", "both"}:
            targets.append(
                ("deals", data.deals)
            )

        for name, df in targets:

            columns = {}

            for column in df.columns:

                sample = (
                    df[column]
                    .dropna()
                    .head(3)
                    .tolist()
                )

                columns[str(column)] = {
                    "dtype": str(df[column].dtype),
                    "example_values": [
                        str(x)
                        for x in sample
                    ],
                }

            output[name] = {
                "row_count": len(df),
                "columns": columns,
            }

        return json.dumps(
            output,
            default=_json_default,
        )

    # ------------------------------------------------------------------
    # Structured analysis
    # ------------------------------------------------------------------

    def _run_analysis(
        self,
        request: dict,
        data: LoadedData,
    ) -> str:

        dataset = request.get("dataset")
        operation = request.get("operation")

        if dataset not in {"work_orders", "deals"}:
            raise ToolExecutionError(
                "dataset must be 'work_orders' or 'deals'."
            )

        valid_operations = {
            "count",
            "sum",
            "mean",
            "group_sum",
            "group_count",
            "filter",
            "top",
            "distinct",
        }

        if operation not in valid_operations:
            raise ToolExecutionError(
                f"Unsupported operation: {operation!r}"
            )

        df = (
            data.work_orders
            if dataset == "work_orders"
            else data.deals
        )

        df = df.copy()

        df = _apply_filters(
            df,
            request.get("filters"),
        )

        column = request.get("column")
        group_by = request.get("group_by")
        limit = request.get("limit")

        if limit is None:
            limit = 50

        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 50

        descending = request.get("descending", True)

        # COUNT
        if operation == "count":

            result = {
                "dataset": dataset,
                "operation": "count",
                "count": int(len(df)),
            }

            return _json_result(result)

        # SUM
        if operation == "sum":

            _require_column(df, column)

            numeric = _clean_numeric(df[column])

            populated = int(numeric.notna().sum())

            total = (
                float(numeric.sum())
                if populated
                else 0.0
            )

            result = {
                "dataset": dataset,
                "operation": "sum",
                "column": column,
                "sum": total,
                "populated_rows": populated,
                "missing_or_non_numeric_rows": int(
                    len(df) - populated
                ),
                "total_rows_after_filters": int(len(df)),
            }

            return _json_result(result)

        # MEAN
        if operation == "mean":

            _require_column(df, column)

            numeric = _clean_numeric(df[column])

            populated = int(numeric.notna().sum())

            result = {
                "dataset": dataset,
                "operation": "mean",
                "column": column,
                "mean": (
                    None
                    if populated == 0
                    else float(numeric.mean())
                ),
                "populated_rows": populated,
                "total_rows_after_filters": int(len(df)),
            }

            return _json_result(result)

        # GROUP SUM
        if operation == "group_sum":

            _require_column(df, column)
            _require_column(df, group_by)

            numeric = _clean_numeric(df[column])

            working = pd.DataFrame(
                {
                    "_group": _clean_group_values(
                        df[group_by]
                    ),
                    "_value": numeric,
                }
            )

            working = working[
                working["_group"].notna()
            ].copy()

            grouped = (
                working
                .groupby(
                    "_group",
                    dropna=False,
                    sort=False,
                )["_value"]
                .agg(
                    value=lambda s: s.sum(min_count=1),
                    populated_rows="count",
                    total_rows="size",
                )
                .reset_index()
            )

            grouped = grouped.rename(
                columns={
                    "_group": group_by,
                }
            )

            grouped["missing_or_non_numeric_rows"] = (
                grouped["total_rows"]
                - grouped["populated_rows"]
            )

            grouped["_sort_value"] = (
                grouped["value"]
                .fillna(float("-inf"))
            )

            grouped = grouped.sort_values(
                "_sort_value",
                ascending=not descending,
                kind="stable",
            ).head(limit)

            grouped = grouped.drop(
                columns=["_sort_value"]
            )

            grouped["value"] = grouped["value"].apply(
                lambda x: (
                    None
                    if pd.isna(x)
                    else float(x)
                )
            )

            grouped["populated_rows"] = (
                grouped["populated_rows"]
                .astype(int)
            )

            grouped["total_rows"] = (
                grouped["total_rows"]
                .astype(int)
            )

            grouped["missing_or_non_numeric_rows"] = (
                grouped["missing_or_non_numeric_rows"]
                .astype(int)
            )

            result = {
                "dataset": dataset,
                "operation": "group_sum",
                "value_column": column,
                "group_by": group_by,
                "groups": grouped.to_dict(
                    orient="records"
                ),
                "filtered_rows": int(len(df)),
                "groups_returned": int(len(grouped)),
            }

            return _json_result(result)

        # GROUP COUNT
        if operation == "group_count":

            _require_column(df, group_by)

            working = pd.DataFrame(
                {
                    "_group": _clean_group_values(
                        df[group_by]
                    )
                }
            )

            working = working[
                working["_group"].notna()
            ]

            grouped = (
                working
                .groupby(
                    "_group",
                    dropna=False,
                    sort=False,
                )
                .size()
                .reset_index(name="count")
            )

            grouped = grouped.rename(
                columns={
                    "_group": group_by
                }
            )

            grouped = grouped.sort_values(
                "count",
                ascending=not descending,
                kind="stable",
            ).head(limit)

            grouped["count"] = (
                grouped["count"]
                .astype(int)
            )

            result = {
                "dataset": dataset,
                "operation": "group_count",
                "group_by": group_by,
                "groups": grouped.to_dict(
                    orient="records"
                ),
                "filtered_rows": int(len(df)),
            }

            return _json_result(result)

        # FILTER
        if operation == "filter":

            output = df.head(limit).copy()

            result = {
                "dataset": dataset,
                "operation": "filter",
                "matching_rows": int(len(df)),
                "rows": output.to_dict(
                    orient="records"
                ),
            }

            return _json_result(result)

        # TOP
        if operation == "top":

            _require_column(df, column)

            if request.get("order_by"):
                order_column = request["order_by"]
                _require_column(df, order_column)

                order_values = _clean_numeric(
                    df[order_column]
                )

                working = df.copy()
                working["_analysis_sort"] = order_values

            else:
                numeric = _clean_numeric(
                    df[column]
                )

                working = df.copy()
                working["_analysis_sort"] = numeric

            working = working.sort_values(
                "_analysis_sort",
                ascending=not descending,
                na_position="last",
                kind="stable",
            ).head(limit)

            working = working.drop(
                columns=["_analysis_sort"]
            )

            result = {
                "dataset": dataset,
                "operation": "top",
                "column": column,
                "rows": working.to_dict(
                    orient="records"
                ),
                "matching_rows": int(len(df)),
            }

            return _json_result(result)

        # DISTINCT
        if operation == "distinct":

            _require_column(df, column)

            values = (
                df[column]
                .dropna()
                .astype("string")
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .drop_duplicates()
                .head(limit)
                .tolist()
            )

            result = {
                "dataset": dataset,
                "operation": "distinct",
                "column": column,
                "values": values,
                "count": len(values),
            }

            return _json_result(result)

        raise ToolExecutionError(
            f"Operation {operation!r} was not implemented."
        )

    # ------------------------------------------------------------------
    # Execution attention
    # ------------------------------------------------------------------

    def _get_execution_attention(
        self,
        request: dict,
        data: LoadedData,
    ) -> str:
        """
        Deterministically find work orders requiring execution/billing attention.

        Important terminology:
        - Execution Status = "Stuck" means execution is stuck.
        - Execution Status = "Pause / struck" means execution is paused/struck.
        - Billing Status = "Stuck" means the billing status is stuck.
        - Billing Status = "Update Required" means a billing status update
          is required.
        - "Not Started" is reported separately and is NOT relabeled as stuck.
        - A row is an attention item when any of the four conditions above
          is true.
        """

        wo = data.work_orders.copy()

        execution_col = _find_column(wo, "Execution Status")
        billing_col = _find_column(wo, "Billing Status")

        if execution_col is None and billing_col is None:
            raise ToolExecutionError(
                "Neither 'Execution Status' nor 'Billing Status' exists "
                "in the work_orders dataset."
            )

        execution = _status_series(
            wo,
            "Execution Status",
        )

        billing = _status_series(
            wo,
            "Billing Status",
        )

        execution_stuck_mask = execution == "stuck"
        pause_mask = execution == "pause / struck"
        billing_stuck_mask = billing == "stuck"
        billing_update_mask = billing == "update required"
        not_started_mask = execution == "not started"

        attention_mask = (
            execution_stuck_mask
            | pause_mask
            | billing_stuck_mask
            | billing_update_mask
        )

        matching = wo.loc[attention_mask].copy()

        try:
            limit = int(request.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50

        limit = max(1, min(limit, 100))

        # Return compact, useful fields when they exist.
        preferred_columns = [
            "Work Order",
            "Work Order ID",
            "Client",
            "Customer",
            "Sector",
            "Execution Status",
            "Billing Status",
            "Invoice Status",
            "WO Status (billed)",
            "Amount Receivable (Masked)",
            "Probable Start Date",
            "Probable End Date",
        ]

        selected = []
        for name in preferred_columns:
            actual = _find_column(matching, name)
            if actual and actual not in selected:
                selected.append(actual)

        if not selected:
            selected = list(matching.columns[:12])

        rows = matching[selected].head(limit).to_dict(
            orient="records"
        )

        result = {
            "dataset": "work_orders",
            "definition": {
                "execution_stuck": 'Execution Status equals "Stuck"',
                "pause_or_struck": (
                    'Execution Status equals "Pause / struck"'
                ),
                "billing_stuck": 'Billing Status equals "Stuck"',
                "billing_update_required": (
                    'Billing Status equals "Update Required"'
                ),
                "not_started_is_not_stuck": True,
            },
            "counts": {
                "execution_stuck": int(execution_stuck_mask.sum()),
                "pause_or_struck": int(pause_mask.sum()),
                "billing_stuck": int(billing_stuck_mask.sum()),
                "billing_update_required": int(
                    billing_update_mask.sum()
                ),
                "not_started": int(not_started_mask.sum()),
                "total_attention_items": int(attention_mask.sum()),
            },
            "rows_returned": int(len(rows)),
            "matching_rows": rows,
        }

        return _json_result(result)

    # ------------------------------------------------------------------
    # Leadership summary
    # ------------------------------------------------------------------

    def _leadership_summary(
        self,
        data: LoadedData,
    ) -> str:
        """
        Deterministically calculate the metrics required by the leadership
        report.

        No LLM reasoning is required to calculate these numbers.
        """

        wo = data.work_orders.copy()
        deals = data.deals.copy()

        # --------------------------------------------------------------
        # Deals
        # --------------------------------------------------------------

        deal_status = _status_series(
            deals,
            "Deal Status",
        )

        sector_col = _find_column(
            deals,
            "Sector/service",
        )

        stage_col = _find_column(
            deals,
            "Deal Stage",
        )

        deal_value_col = _find_column(
            deals,
            "Masked Deal value",
        )

        if sector_col:
            deal_sector = _clean_group_values(
                deals[sector_col]
            )
        else:
            deal_sector = pd.Series(
                pd.NA,
                index=deals.index,
                dtype="string",
            )

        deal_values = (
            _clean_numeric(deals[deal_value_col])
            if deal_value_col
            else pd.Series(
                float("nan"),
                index=deals.index,
            )
        )

        open_mask = deal_status == "open"
        won_mask = deal_status == "won"

        open_deals = deals[open_mask].copy()
        won_deals = deals[won_mask].copy()

        open_values = deal_values[open_mask]
        won_values = deal_values[won_mask]

        open_populated = int(
            open_values.notna().sum()
        )

        won_populated = int(
            won_values.notna().sum()
        )

        open_pipeline_value = (
            float(open_values.sum())
            if open_populated
            else 0.0
        )

        won_value = (
            float(won_values.sum())
            if won_populated
            else 0.0
        )

        # Stage distribution
        stage_distribution = []

        if stage_col:
            stage_series = _clean_group_values(
                deals.loc[open_mask, stage_col]
            )

            stage_distribution = (
                stage_series
                .dropna()
                .value_counts()
                .rename_axis("Deal Stage")
                .reset_index(name="count")
                .to_dict(orient="records")
            )

        # --------------------------------------------------------------
        # Work orders
        # --------------------------------------------------------------

        execution_col = _find_column(
            wo,
            "Execution Status",
        )

        billing_col = _find_column(
            wo,
            "Billing Status",
        )

        wo_sector_col = _find_column(
            wo,
            "Sector",
        )

        billed_col = _find_column(
            wo,
            "Billed Value in Rupees (Incl of GST.) (Masked)",
        )

        collected_col = _find_column(
            wo,
            "Collected Amount in Rupees (Incl of GST.) (Masked)",
        )

        receivable_col = _find_column(
            wo,
            "Amount Receivable (Masked)",
        )

        execution = _status_series(
            wo,
            "Execution Status",
        )

        billing = _status_series(
            wo,
            "Billing Status",
        )

        completed_mask = execution == "completed"

        active_mask = ~completed_mask

        active_work_orders = int(
            active_mask.sum()
        )

        billed_info = _safe_sum(
            wo,
            billed_col
            or "Billed Value in Rupees (Incl of GST.) (Masked)",
        )

        collected_info = _safe_sum(
            wo,
            collected_col
            or "Collected Amount in Rupees (Incl of GST.) (Masked)",
        )

        receivable_info = _safe_sum(
            wo,
            receivable_col
            or "Amount Receivable (Masked)",
        )

        # --------------------------------------------------------------
        # Execution watch
        # --------------------------------------------------------------

        execution_attention_statuses = {
            "stuck",
            "pause / struck",
            "not started",
        }

        execution_attention = []

        if execution_col:
            execution_counts = (
                execution
                .value_counts()
                .to_dict()
            )

            for status, count in execution_counts.items():
                if status in execution_attention_statuses:
                    execution_attention.append(
                        {
                            "status": status,
                            "count": int(count),
                        }
                    )

        billing_stuck = int((billing == "stuck").sum())
        billing_update_required = int(
            (billing == "update required").sum()
        )

        # --------------------------------------------------------------
        # Sector performance
        # --------------------------------------------------------------

        sector_names = set()

        if sector_col:
            sector_names.update(
                str(x)
                for x in deal_sector.dropna().unique()
            )

        if wo_sector_col:
            wo_sector = _clean_group_values(
                wo[wo_sector_col]
            )

            sector_names.update(
                str(x)
                for x in wo_sector.dropna().unique()
            )
        else:
            wo_sector = pd.Series(
                pd.NA,
                index=wo.index,
                dtype="string",
            )

        sector_performance = []

        for sector in sorted(
            sector_names,
            key=lambda x: x.casefold(),
        ):

            deal_sector_normalized = (
                deal_sector.astype("string")
                .str.strip()
                .str.casefold()
            )

            wo_sector_normalized = (
                wo_sector.astype("string")
                .str.strip()
                .str.casefold()
            )

            sector_norm = sector.strip().casefold()

            sector_open_mask = (
                open_mask
                & (
                    deal_sector_normalized
                    == sector_norm
                )
            )

            sector_won_mask = (
                won_mask
                & (
                    deal_sector_normalized
                    == sector_norm
                )
            )

            sector_wo_mask = (
                wo_sector_normalized
                == sector_norm
            )

            sector_open_value = deal_values[
                sector_open_mask
            ]

            sector_won_value = deal_values[
                sector_won_mask
            ]

            sector_performance.append(
                {
                    "Sector": sector,
                    "Open Pipeline Value": float(
                        sector_open_value.sum()
                    )
                    if sector_open_value.notna().any()
                    else 0.0,
                    "Open Pipeline Populated Rows": int(
                        sector_open_value.notna().sum()
                    ),
                    "Open Pipeline Total Rows": int(
                        sector_open_mask.sum()
                    ),
                    "Won Value": float(
                        sector_won_value.sum()
                    )
                    if sector_won_value.notna().any()
                    else 0.0,
                    "Won Populated Rows": int(
                        sector_won_value.notna().sum()
                    ),
                    "Won Total Rows": int(
                        sector_won_mask.sum()
                    ),
                    "Active Work Orders": int(
                        (sector_wo_mask & active_mask).sum()
                    ),
                }
            )

        # --------------------------------------------------------------
        # Highest-value open deals
        # --------------------------------------------------------------

        highest_value_open_deals = []

        if open_mask.any() and deal_value_col:

            candidate = deals.loc[
                open_mask
                & deal_values.notna()
            ].copy()

            candidate["_value"] = deal_values.loc[
                candidate.index
            ]

            candidate = candidate.sort_values(
                "_value",
                ascending=False,
                kind="stable",
            ).head(10)

            name_col = _find_column(
                deals,
                "Deal Name",
            )

            for idx, row in candidate.iterrows():

                highest_value_open_deals.append(
                    {
                        "name": (
                            str(row[name_col])
                            if name_col
                            and pd.notna(row[name_col])
                            else f"Deal row {idx}"
                        ),
                        "value": float(
                            candidate.loc[idx, "_value"]
                        ),
                        "sector": (
                            str(row[sector_col])
                            if sector_col
                            and pd.notna(row[sector_col])
                            else None
                        ),
                        "stage": (
                            str(row[stage_col])
                            if stage_col
                            and pd.notna(row[stage_col])
                            else None
                        ),
                    }
                )

        # --------------------------------------------------------------
        # Data coverage
        # --------------------------------------------------------------

        def coverage_info(
            df: pd.DataFrame,
            column: str,
        ) -> dict:

            actual = _find_column(
                df,
                column,
            )

            if actual is None:
                return {
                    "column": column,
                    "exists": False,
                    "total_rows": int(len(df)),
                    "populated_rows": 0,
                    "missing_rows": int(len(df)),
                    "missing_percent": 100.0,
                }

            populated = int(
                df[actual].notna().sum()
            )

            total = int(len(df))

            missing = total - populated

            return {
                "column": actual,
                "exists": True,
                "total_rows": total,
                "populated_rows": populated,
                "missing_rows": missing,
                "missing_percent": (
                    round(
                        missing / total * 100,
                        1,
                    )
                    if total
                    else 0.0
                ),
            }

        coverage = {
            "work_orders": {
                "row_count": int(len(wo)),
                "Sector": coverage_info(
                    wo,
                    "Sector",
                ),
                "Execution Status": coverage_info(
                    wo,
                    "Execution Status",
                ),
                "Billing Status": coverage_info(
                    wo,
                    "Billing Status",
                ),
                "Amount Receivable (Masked)": coverage_info(
                    wo,
                    "Amount Receivable (Masked)",
                ),
            },
            "deals": {
                "row_count": int(len(deals)),
                "Sector/service": coverage_info(
                    deals,
                    "Sector/service",
                ),
                "Deal Status": coverage_info(
                    deals,
                    "Deal Status",
                ),
                "Masked Deal value": coverage_info(
                    deals,
                    "Masked Deal value",
                ),
                "Deal Stage": coverage_info(
                    deals,
                    "Deal Stage",
                ),
            },
        }

        # --------------------------------------------------------------
        # Final deterministic result
        # --------------------------------------------------------------

        result = {
            "scope": {
                "work_orders_rows": int(len(wo)),
                "deals_rows": int(len(deals)),
                "open_deal_definition": (
                    'Deal Status equals "Open"'
                ),
                "won_deal_definition": (
                    'Deal Status equals "Won"'
                ),
                "active_work_order_definition": (
                    'Execution Status is not "Completed"'
                ),
                "period": (
                    "All available records; no date filter applied."
                ),
            },

            "headline_metrics": {
                "open_pipeline_value": {
                    "value": open_pipeline_value,
                    "populated_rows": open_populated,
                    "total_rows": int(open_mask.sum()),
                    "missing_or_non_numeric_rows": (
                        int(open_mask.sum())
                        - open_populated
                    ),
                },
                "won_deal_value": {
                    "value": won_value,
                    "populated_rows": won_populated,
                    "total_rows": int(won_mask.sum()),
                    "missing_or_non_numeric_rows": (
                        int(won_mask.sum())
                        - won_populated
                    ),
                    "period": (
                        "All available won records; "
                        "no date filter applied."
                    ),
                },
                "active_work_orders": active_work_orders,
                "billed_value": billed_info,
                "collected_value": collected_info,
                "receivable_value": receivable_info,
            },

            "sector_performance": sector_performance,

            "pipeline_health": {
                "open_deal_count": int(open_mask.sum()),
                "stage_distribution": stage_distribution,
                "highest_value_open_deals": (
                    highest_value_open_deals
                ),
            },

            "execution_collections_watch": {
                "execution_attention": execution_attention,
                "billing_stuck": billing_stuck,
                "billing_update_required": (
                    billing_update_required
                ),
                "receivable": receivable_info,
            },

            "data_coverage": coverage,

            "data_quality": {
                "work_orders": data.wo_report.summary_lines(
                    len(wo),
                    [
                        "Sector",
                        "Execution Status",
                        "Amount in Rupees (Incl of GST) (Masked)",
                        "Invoice Status",
                        "WO Status (billed)",
                        "Amount Receivable (Masked)",
                    ],
                ),
                "deals": data.deal_report.summary_lines(
                    len(deals),
                    [
                        "Deal Status",
                        "Closure Probability",
                        "Masked Deal value",
                        "Sector/service",
                        "Tentative Close Date",
                    ],
                ),
            },
        }

        return json.dumps(
            result,
            default=_json_default,
            allow_nan=False,
        )

    # ------------------------------------------------------------------
    # Data quality
    # ------------------------------------------------------------------

    def _get_data_quality_notes(
        self,
        data: LoadedData,
    ) -> str:

        wo_notes = data.wo_report.summary_lines(
            len(data.work_orders),
            [
                "Sector",
                "Execution Status",
                "Amount in Rupees (Incl of GST) (Masked)",
                "Invoice Status",
                "WO Status (billed)",
                "Amount Receivable (Masked)",
            ],
        )

        deal_notes = data.deal_report.summary_lines(
            len(data.deals),
            [
                "Deal Status",
                "Closure Probability",
                "Masked Deal value",
                "Sector/service",
                "Tentative Close Date",
            ],
        )

        return json.dumps(
            {
                "work_orders_row_count": len(
                    data.work_orders
                ),
                "deals_row_count": len(
                    data.deals
                ),
                "work_orders_caveats": wo_notes,
                "deals_caveats": deal_notes,
            },
            default=_json_default,
        )