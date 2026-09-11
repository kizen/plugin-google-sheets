import json
import re
from urllib.parse import quote

# Preview-qualified path for this unmerged PR (see plugin-wizard bot comment) — MUST revert to "/external-integrations/proxy/google_sheets/shared" before merging.
BASE_URL = "/external-integrations/proxy/google_sheets_preview_google_sheets_update_data_type/shared"

# Kizen's longtext field type tops out around 50k characters — a single output over this would fail downstream anyway.
MAX_OUTPUT_CHARS = 50000

MATCH_TYPES = {"equals", "not_equals", "contains", "starts_with", "is_empty", "is_not_empty"}
MATCH_TYPES_REQUIRING_VALUE = MATCH_TYPES - {"is_empty", "is_not_empty"}


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


def validate_spreadsheet_id(spreadsheet_id):
    # Interpolated directly into the request URL — reject anything that could inject query params or extra path segments.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
        raise Exception(f"spreadsheet_id must contain only letters, numbers, hyphens, and underscores, got: {spreadsheet_id}")
    return spreadsheet_id


def resolve_positive_int(raw, name):
    # Optional number with a platform default of 1 — this guard covers a bound variable resolving to blank or non-whole at runtime.
    if raw is None or raw == "":
        return 1
    if isinstance(raw, float) and not raw.is_integer():
        raise Exception(f"{name} must be a whole number, got {raw}.")
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

    if not isinstance(payload, dict):
        raise Exception(f"Google Sheets error {context}: unknown_error — unexpected response shape, HTTP {resp.status_code}: {str(payload)[:200]}")

    body = payload.get("body")
    if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
        raise_sheets_error(payload, context, resp.status_code)

    return body


def row_from_cells(data_row, headers):
    # Sheets omits trailing empty cells from a row entirely, so a row can be shorter than headers — pad missing cells with "".
    return {header: (data_row[i] if i < len(data_row) else "") for i, header in enumerate(headers)}


def value_matches(cell_value, match_type, match_value):
    if match_type == "equals":
        return cell_value == match_value
    if match_type == "not_equals":
        return cell_value != match_value
    if match_type == "contains":
        return match_value in cell_value
    if match_type == "starts_with":
        return cell_value.startswith(match_value)
    if match_type == "is_empty":
        return cell_value == ""
    return cell_value != ""  # is_not_empty


spreadsheet_id = validate_spreadsheet_id(inputs.spreadsheet_id)
sheet_name = inputs.sheet_name
column_name = inputs.column_name
match_value = getattr(inputs, "match_value", None)
return_all_matches = inputs.return_all_matches

header_row = resolve_positive_int(getattr(inputs, "header_row", None), "header_row")
header_column = resolve_positive_int(getattr(inputs, "header_column", None), "header_column")

match_type_raw = getattr(inputs, "match_type", None)
match_type = match_type_raw if match_type_raw else "equals"
if match_type not in MATCH_TYPES:
    raise Exception(f"match_type must be one of {sorted(MATCH_TYPES)}, got '{match_type}'.")
if match_type in MATCH_TYPES_REQUIRING_VALUE and not match_value:
    raise Exception(f"match_value is required when match_type is '{match_type}'.")

range_param = quote(a1_quote_sheet_name(sheet_name), safe="")

resp = kizen.api.get(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{range_param}")
body = check_sheets_response(resp, "searching rows")

values = body.get("values", [])

if not values or header_row > len(values):
    outputs.matching_rows = json.dumps([])
    outputs.row_numbers = json.dumps([])
else:
    header_row_values = values[header_row - 1]
    if header_column > len(header_row_values):
        raise Exception(f"header_column {header_column} exceeds header row's column count ({len(header_row_values)}) in sheet '{sheet_name}'.")

    headers = header_row_values[header_column - 1:]
    if column_name not in headers:
        raise Exception(f"column_name '{column_name}' is not a header in sheet '{sheet_name}'. Headers: {headers}")

    matching_rows = []
    row_numbers = []
    # offset is 0-indexed within data rows; the real (1-indexed) sheet row for offset 0 is header_row + 1.
    for offset, data_row in enumerate(values[header_row:]):
        sliced_row = data_row[header_column - 1:]
        row = row_from_cells(sliced_row, headers)
        if not value_matches(row.get(column_name, ""), match_type, match_value):
            continue

        matching_rows.append(row)
        row_numbers.append(header_row + offset + 1)

        if not return_all_matches:
            break

    matching_rows_json = json.dumps(matching_rows)
    if len(matching_rows_json) > MAX_OUTPUT_CHARS:
        raise Exception(f"matching_rows output would be {len(matching_rows_json)} characters, over the {MAX_OUTPUT_CHARS}-character output limit. Narrow the result with a more specific match_value, or set return_all_matches to false.")

    outputs.matching_rows = matching_rows_json
    outputs.row_numbers = json.dumps(row_numbers)
