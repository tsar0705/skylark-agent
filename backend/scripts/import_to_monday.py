"""
One-time setup script: creates two monday.com boards ("Work Orders" and
"Deals") and imports the raw source data into them as items.

IMPORTANT:
    The source data is intentionally NOT cleaned during import.

    All source fields are imported as TEXT. This is deliberate because the
    assignment requires the raw/messy data to remain available to the agent.

    Examples of values that must be preserved:
        5360 HA
        2057 Acr
        2 location
        45days
        NA
        #VALUE!
        4,875,000.000
        Close Date

    The agent performs normalization later when reading data from monday.com.

The importer also handles monday.com HTTP 429 rate limits using retries and
exponential backoff.

Usage:
    cd backend

    python -m scripts.import_to_monday ^
        --work-orders ..\sample_data\Work_Order_Tracker_Data.xlsx ^
        --deals ..\sample_data\Deal_funnel_Data.xlsx

After completion, the script prints the two board IDs. Add them to
backend/.env as:

    MONDAY_WORK_ORDERS_BOARD_ID=<work-orders-board-id>
    MONDAY_DEALS_BOARD_ID=<deals-board-id>
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import openpyxl

# Allow importing app.config when this script is executed with:
# python -m scripts.import_to_monday
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402


# ---------------------------------------------------------------------------
# monday.com configuration
# ---------------------------------------------------------------------------

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_API_VERSION = "2026-07"

# Wait between column creation requests.
COLUMN_DELAY_SECONDS = 0.5

# Wait between item creation requests.
#
# This is intentionally conservative because monday.com can rate-limit
# repeated mutation requests.
ITEM_DELAY_SECONDS = 1.0

# Retry configuration for HTTP 429 responses.
MAX_RETRIES = 6
INITIAL_RETRY_DELAY = 10
MAX_RETRY_DELAY = 60


# ---------------------------------------------------------------------------
# Source column metadata
# ---------------------------------------------------------------------------
#
# These mappings describe the semantic meaning of the source columns.
#
# They are NOT used to create monday.com status/dropdown/number/date columns.
# Everything is intentionally created as TEXT so that messy source values
# are never rejected during import.
# ---------------------------------------------------------------------------

WORK_ORDER_COLUMN_TYPES = {
    "Execution Status": "status",
    "WO Status (billed)": "status",
    "Invoice Status": "status",
    "Collection status": "status",
    "Billing Status": "status",
    "Sector": "dropdown",
    "Type of Work": "dropdown",
    "Nature of Work": "dropdown",
    "Document Type": "dropdown",
    "Data Delivery Date": "date",
    "Date of PO/LOI": "date",
    "Probable Start Date": "date",
    "Probable End Date": "date",
    "Last invoice date": "date",
    "Collection Date": "date",
    "Amount in Rupees (Excl of GST) (Masked)": "numbers",
    "Amount in Rupees (Incl of GST) (Masked)": "numbers",
    "Billed Value in Rupees (Excl of GST.) (Masked)": "numbers",
    "Billed Value in Rupees (Incl of GST.) (Masked)": "numbers",
    "Collected Amount in Rupees (Incl of GST.) (Masked)": "numbers",
    "Amount to be billed in Rs. (Exl. of GST) (Masked)": "numbers",
    "Amount to be billed in Rs. (Incl. of GST) (Masked)": "numbers",
    "Amount Receivable (Masked)": "numbers",
    "Quantities as per PO": "numbers",
    "Quantity billed (till date)": "numbers",
    "Balance in quantity": "numbers",
}


DEAL_COLUMN_TYPES = {
    "Deal Status": "status",
    "Deal Stage": "dropdown",
    "Closure Probability": "dropdown",
    "Sector/service": "dropdown",
    "Product deal": "dropdown",
    "Close Date (A)": "date",
    "Tentative Close Date": "date",
    "Created Date": "date",
    "Masked Deal value": "numbers",
}


# ---------------------------------------------------------------------------
# GraphQL helper
# ---------------------------------------------------------------------------

def gql(query: str, variables: dict) -> dict:
    """
    Execute a monday.com GraphQL request.

    HTTP 429 responses are retried with exponential backoff.

    GraphQL errors are raised as RuntimeError.
    Other HTTP errors are raised immediately.
    """

    headers = {
        "Authorization": settings.MONDAY_API_KEY,
        "Content-Type": "application/json",
        "API-Version": MONDAY_API_VERSION,
    }

    for attempt in range(MAX_RETRIES):

        try:
            response = httpx.post(
                MONDAY_API_URL,
                json={
                    "query": query,
                    "variables": variables,
                },
                headers=headers,
                timeout=30.0,
            )

        except httpx.RequestError as exc:

            if attempt == MAX_RETRIES - 1:
                raise

            wait = min(
                INITIAL_RETRY_DELAY * (2 ** attempt),
                MAX_RETRY_DELAY,
            )

            print(
                f"  Network error: {exc}"
            )

            print(
                f"  Retrying in {int(wait)}s "
                f"({attempt + 1}/{MAX_RETRIES})..."
            )

            time.sleep(wait)
            continue

        # ---------------------------------------------------------------
        # Rate limit
        # ---------------------------------------------------------------

        if response.status_code == 429:

            if attempt == MAX_RETRIES - 1:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")

            if retry_after:

                try:
                    wait = max(float(retry_after), 1)

                except ValueError:
                    wait = min(
                        INITIAL_RETRY_DELAY * (2 ** attempt),
                        MAX_RETRY_DELAY,
                    )

            else:
                wait = min(
                    INITIAL_RETRY_DELAY * (2 ** attempt),
                    MAX_RETRY_DELAY,
                )

            print(
                f"  Rate limited (429). "
                f"Waiting {int(wait)}s before retry..."
            )

            time.sleep(wait)
            continue

        # ---------------------------------------------------------------
        # Other HTTP errors
        # ---------------------------------------------------------------

        response.raise_for_status()

        body = response.json()

        # ---------------------------------------------------------------
        # GraphQL errors
        # ---------------------------------------------------------------

        if "errors" in body:
            raise RuntimeError(
                f"monday.com error: {body['errors']}"
            )

        return body["data"]

    raise RuntimeError(
        "monday.com request failed after all retry attempts."
    )


# ---------------------------------------------------------------------------
# Excel reader
# ---------------------------------------------------------------------------

def read_sheet(path: str) -> tuple[list[str], list[list]]:
    """
    Read the first worksheet from an Excel file.

    No cleaning or normalization is performed.
    """

    workbook = openpyxl.load_workbook(
        path,
        data_only=True,
    )

    worksheet = workbook.worksheets[0]

    rows = [
        list(row)
        for row in worksheet.iter_rows(values_only=True)
    ]

    if not rows:
        raise ValueError(
            f"No rows found in Excel file: {path}"
        )

    # Find the first row that looks like a real header.
    #
    # The original dataset can contain unusual/duplicate header-like rows,
    # so preserve the existing detection strategy.
    header_idx = 0

    for i, row in enumerate(rows[:5]):

        non_empty = [
            cell
            for cell in row
            if cell not in (None, "")
        ]

        if len(non_empty) >= max(3, len(row) // 2):
            header_idx = i
            break

    header = [
        str(cell).strip()
        if cell is not None
        else f"col_{i}"
        for i, cell in enumerate(rows[header_idx])
    ]

    data_rows = rows[header_idx + 1:]

    return header, data_rows


# ---------------------------------------------------------------------------
# monday.com board/column/item creation
# ---------------------------------------------------------------------------

def create_board(name: str) -> str:
    """Create a monday.com public board and return its ID."""

    query = """
    mutation ($name: String!) {
        create_board(
            board_name: $name,
            board_kind: public
        ) {
            id
        }
    }
    """

    data = gql(
        query,
        {
            "name": name,
        },
    )

    return data["create_board"]["id"]


def create_column(
    board_id: str,
    title: str,
    col_type: str,
) -> str:
    """Create a monday.com column and return its ID."""

    query = """
    mutation (
        $boardId: ID!,
        $title: String!,
        $colType: ColumnType!
    ) {
        create_column(
            board_id: $boardId,
            title: $title,
            column_type: $colType
        ) {
            id
        }
    }
    """

    data = gql(
        query,
        {
            "boardId": board_id,
            "title": title,
            "colType": col_type,
        },
    )

    return data["create_column"]["id"]


def create_item(
    board_id: str,
    item_name: str,
    column_values: dict,
) -> str:
    """Create a monday.com board item and return its ID."""

    query = """
    mutation (
        $boardId: ID!,
        $itemName: String!,
        $columnValues: JSON!
    ) {
        create_item(
            board_id: $boardId,
            item_name: $itemName,
            column_values: $columnValues
        ) {
            id
        }
    }
    """

    data = gql(
        query,
        {
            "boardId": board_id,
            "itemName": item_name,
            "columnValues": json.dumps(column_values),
        },
    )

    return data["create_item"]["id"]


# ---------------------------------------------------------------------------
# Value handling
# ---------------------------------------------------------------------------

def stringify_cell(value) -> str | None:
    """
    Convert an Excel cell into a string.

    No cleaning is performed.
    """

    if value is None:
        return None

    return str(value)


def build_column_values(
    header: list[str],
    row: list,
    col_ids: dict[str, str],
    name_column: str,
) -> dict:
    """
    Convert one raw Excel row into monday.com column values.

    EVERY column is sent as TEXT.

    This is intentional.

    Examples:

        "5360 HA"       -> "5360 HA"
        "45days"        -> "45days"
        "NA"            -> "NA"
        "#VALUE!"       -> "#VALUE!"
        "Close Date"    -> "Close Date"
        "4,875,000.000" -> "4,875,000.000"

    No date/number/status/dropdown normalization happens here.
    """

    column_values = {}

    for index, title in enumerate(header):

        # The first/name column becomes the monday.com item name.
        if title == name_column:
            continue

        # Some rows can contain fewer cells than the header.
        if index >= len(row):
            continue

        raw_value = row[index]

        # Preserve empty cells as empty/unset values.
        if raw_value is None:
            continue

        column_id = col_ids.get(title)

        if not column_id:
            continue

        raw = stringify_cell(raw_value)

        if raw is None:
            continue

        # IMPORTANT:
        # Always send the source value as TEXT.
        column_values[column_id] = raw

    return column_values


# ---------------------------------------------------------------------------
# Sheet import
# ---------------------------------------------------------------------------

def import_sheet(
    path: str,
    board_name: str,
    name_column: str,
    column_type_map: dict,
):
    """
    Create one monday.com board and import all rows from one Excel sheet.
    """

    print(f"Reading {path} ...")

    header, rows = read_sheet(path)

    print(
        f"  {len(header)} columns, "
        f"{len(rows)} rows"
    )

    # -------------------------------------------------------------------
    # Create board
    # -------------------------------------------------------------------

    print(
        f"Creating board '{board_name}' ..."
    )

    board_id = create_board(board_name)

    print(
        f"  board_id = {board_id}"
    )

    # -------------------------------------------------------------------
    # Create columns
    # -------------------------------------------------------------------

    print("Creating columns...")

    col_ids: dict[str, str] = {}

    for title in header:

        if title == name_column:
            continue

        source_type = column_type_map.get(
            title,
            "text",
        )

        # ---------------------------------------------------------------
        # IMPORTANT:
        #
        # Regardless of source semantic type, create a TEXT column.
        #
        # This prevents monday.com from rejecting messy values.
        # ---------------------------------------------------------------

        monday_type = "text"

        try:

            col_ids[title] = create_column(
                board_id,
                title,
                monday_type,
            )

            if source_type != "text":

                print(
                    f"  {title}: "
                    f"{source_type} -> text"
                )

        except Exception as exc:

            print(
                f"  WARNING: failed to create column "
                f"'{title}' as text: {exc}"
            )

            # Try one more time as text.
            col_ids[title] = create_column(
                board_id,
                title,
                "text",
            )

        time.sleep(COLUMN_DELAY_SECONDS)

    # -------------------------------------------------------------------
    # Import items
    # -------------------------------------------------------------------

    print(
        "Importing items "
        "(this can take a while for large sheets)..."
    )

    if name_column in header:
        name_index = header.index(name_column)
    else:
        name_index = 0

    successful = 0
    failed = 0

    for index, row in enumerate(rows):

        # ---------------------------------------------------------------
        # Determine item name
        # ---------------------------------------------------------------

        if name_index < len(row):
            item_name = stringify_cell(
                row[name_index]
            )
        else:
            item_name = None

        if not item_name:
            item_name = f"Row {index + 1}"

        # ---------------------------------------------------------------
        # Build raw text values
        # ---------------------------------------------------------------

        column_values = build_column_values(
            header=header,
            row=row,
            col_ids=col_ids,
            name_column=name_column,
        )

        # ---------------------------------------------------------------
        # Create item
        # ---------------------------------------------------------------

        try:

            create_item(
                board_id=board_id,
                item_name=item_name,
                column_values=column_values,
            )

            successful += 1

        except Exception as exc:

            failed += 1

            print(
                f"  WARNING: row {index + 1} "
                f"('{item_name}') failed to import: {exc}"
            )

        # ---------------------------------------------------------------
        # Progress
        # ---------------------------------------------------------------

        processed = index + 1

        if processed % 25 == 0:

            print(
                f"  ... {processed}/{len(rows)} rows processed "
                f"({successful} imported, {failed} failed)"
            )

        # ---------------------------------------------------------------
        # Rate-limit protection
        # ---------------------------------------------------------------

        time.sleep(ITEM_DELAY_SECONDS)

    # -------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------

    print()

    print(
        f"Done: {board_name}"
    )

    print(
        f"  board_id = {board_id}"
    )

    print(
        f"  imported = {successful}"
    )

    print(
        f"  failed   = {failed}"
    )

    if failed:
        print(
            "  WARNING: Some rows failed. "
            "Review the messages above."
        )

    return board_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Import raw Work Orders and Deals Excel data "
            "into monday.com."
        )
    )

    parser.add_argument(
        "--work-orders",
        required=True,
        help=(
            "Path to Work_Order_Tracker_Data.xlsx"
        ),
    )

    parser.add_argument(
        "--deals",
        required=True,
        help=(
            "Path to Deal_funnel_Data.xlsx"
        ),
    )

    args = parser.parse_args()

    # -------------------------------------------------------------------
    # Check API key
    # -------------------------------------------------------------------

    if not settings.MONDAY_API_KEY:

        raise SystemExit(
            "Set MONDAY_API_KEY in backend/.env "
            "before running this script."
        )

    print(
        f"Using monday.com API version "
        f"{MONDAY_API_VERSION}"
    )

    print()

    # -------------------------------------------------------------------
    # Work Orders
    # -------------------------------------------------------------------

    work_orders_id = import_sheet(
        path=args.work_orders,
        board_name="Work Orders",
        name_column="Deal name masked",
        column_type_map=WORK_ORDER_COLUMN_TYPES,
    )

    print()
    print("=" * 60)
    print()

    # -------------------------------------------------------------------
    # Deals
    # -------------------------------------------------------------------

    deals_id = import_sheet(
        path=args.deals,
        board_name="Deals",
        name_column="Deal Name",
        column_type_map=DEAL_COLUMN_TYPES,
    )

    # -------------------------------------------------------------------
    # Final IDs
    # -------------------------------------------------------------------

    print()
    print("=" * 60)
    print()

    print("Import complete.")
    print()

    print("Add these to backend/.env:")

    print(
        f"MONDAY_WORK_ORDERS_BOARD_ID={work_orders_id}"
    )

    print(
        f"MONDAY_DEALS_BOARD_ID={deals_id}"
    )


if __name__ == "__main__":
    main()