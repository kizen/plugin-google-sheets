import json
from urllib.parse import quote

BASE_URL = "/external-integrations/proxy/google_sheets/shared"


def raise_sheets_error(payload, context, fallback_status):
    # Proxy wraps upstream calls as {status_code, body}; a Google error lives at body["error"], a proxy error is flat, and body may not be a dict at all (e.g. HTML on a wrong host) — hence the isinstance guard.
    body = payload.get("body")
    google_error = body.get("error") if isinstance(body, dict) else None
    if isinstance(google_error, dict):
        message = google_error.get("message", "unknown_error")
        status = google_error.get("status", "unknown_error")
        raise Exception(f"Google Sheets error {context}: {status} — {message}")

    kizen_error = payload.get("error") or payload.get("detail")
    if kizen_error:
        raise Exception(f"Google Sheets error {context}: proxy_error — {kizen_error}")

    upstream_status = payload.get("status_code", fallback_status)
    snippet = str(body)[:200] if body is not None else "no body"
    raise Exception(f"Google Sheets error {context}: unknown_error — upstream HTTP {upstream_status}, body: {snippet}")


def a1_quote_sheet_name(name):
    # Quote unconditionally — always valid in A1 notation, so no need to guess when it's required.
    return "'" + name.replace("'", "''") + "'"


def resolve_positive_int(raw, name):
    # header_row/header_column are string inputs (a number-typed optional input crashes the whole run when blank) — blank defaults to 1.
    if not raw:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise Exception(f"{name} must be a whole number, got '{raw}'.")
    if value < 1:
        raise Exception(f"{name} must be 1 or greater, got {value}.")
    return value


def check_sheets_response(resp, context):
    # Handles both proxy-level failures (resp.ok False) and upstream failures wrapped inside a 200 envelope.
    try:
        payload = resp.json()
    except Exception:
        raise Exception(f"Google Sheets error {context}: unknown_error — HTTP {resp.status_code}")

    body = payload.get("body")
    if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
        raise_sheets_error(payload, context, resp.status_code)

    return body


def row_from_cells(data_row, headers):
    # Sheets omits trailing empty cells from a row entirely, so a row can be shorter than headers — pad missing cells with "".
    return {header: (data_row[i] if i < len(data_row) else "") for i, header in enumerate(headers)}


spreadsheet_id = inputs.spreadsheet_id
sheet_name = inputs.sheet_name
filter_column = getattr(inputs, "filter_column", None)
filter_value = getattr(inputs, "filter_value", None)

header_row = resolve_positive_int(getattr(inputs, "header_row", None), "header_row")
header_column = resolve_positive_int(getattr(inputs, "header_column", None), "header_column")

if filter_column and not filter_value:
    raise Exception("filter_value is required when filter_column is set.")
if filter_value and not filter_column:
    raise Exception("filter_column is required when filter_value is set.")

range_param = quote(a1_quote_sheet_name(sheet_name), safe="")

resp = kizen.api.get(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{range_param}")
body = check_sheets_response(resp, "reading rows")

values = body.get("values", [])

if not values:
    outputs.rows = json.dumps([])
    outputs.row_count = 0
else:
    if header_row > len(values):
        raise Exception(f"header_row {header_row} exceeds sheet '{sheet_name}' row count ({len(values)}).")

    header_row_values = values[header_row - 1]
    if header_column > len(header_row_values):
        raise Exception(f"header_column {header_column} exceeds header row's column count ({len(header_row_values)}) in sheet '{sheet_name}'.")

    headers = header_row_values[header_column - 1:]
    data_rows = [row[header_column - 1:] for row in values[header_row:]]

    if filter_column and filter_column not in headers:
        raise Exception(f"filter_column '{filter_column}' is not a header in sheet '{sheet_name}'. Headers: {headers}")

    rows = []
    for data_row in data_rows:
        row = row_from_cells(data_row, headers)
        if filter_column and row.get(filter_column) != filter_value:
            continue
        rows.append(row)

    outputs.rows = json.dumps(rows)
    outputs.row_count = len(rows)
