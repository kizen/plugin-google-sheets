import json
import re
from urllib.parse import quote

# Preview-qualified path for this unmerged PR (see plugin-wizard bot comment) — MUST revert to "/external-integrations/proxy/google_sheets/shared" before merging.
BASE_URL = "/external-integrations/proxy/google_sheets_preview_google_sheets_update_data_type/shared"


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


def column_number_to_letter(n):
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


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


spreadsheet_id = validate_spreadsheet_id(inputs.spreadsheet_id)
sheet_name = inputs.sheet_name

header_row = resolve_positive_int(getattr(inputs, "header_row", None), "header_row")
header_column = resolve_positive_int(getattr(inputs, "header_column", None), "header_column")

row_number_raw = getattr(inputs, "row_number", None)
match_column = getattr(inputs, "match_column", None)
match_value = getattr(inputs, "match_value", None)
update_all = inputs.update_all

has_row_number = bool(row_number_raw)
has_match = bool(match_column) or bool(match_value)

if has_row_number and has_match:
    raise Exception("Provide either row_number or match_column/match_value to target a row, not both.")
if not has_row_number and not has_match:
    raise Exception("Provide either row_number or match_column/match_value to target a row to update.")
if has_match and not (match_column and match_value):
    raise Exception("Both match_column and match_value are required together.")

try:
    row_data = json.loads(inputs.row_data)
except Exception as e:
    raise Exception(f"row_data must be a valid JSON object string: {e}")

if not isinstance(row_data, dict):
    raise Exception(f"row_data must be a JSON object (e.g. {{\"Status\": \"Inactive\"}}), got: {inputs.row_data}")
if not row_data:
    raise Exception("row_data must include at least one column to update.")

quoted_sheet_name = a1_quote_sheet_name(sheet_name)

if has_row_number:
    try:
        row_number = int(row_number_raw)
    except (TypeError, ValueError):
        raise Exception(f"row_number must be a whole number, got '{row_number_raw}'.")

    if row_number <= header_row:
        raise Exception(f"row_number {row_number} is on or above header_row {header_row} — it isn't a data row.")

    # Only the header row is needed here — row_number already tells us exactly where to write.
    header_range_param = quote(f"{quoted_sheet_name}!{header_row}:{header_row}", safe="")
    header_resp = kizen.api.get(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{header_range_param}")
    header_body = check_sheets_response(header_resp, "reading header row")
    header_values = header_body.get("values", [])
    if not header_values:
        raise Exception(f"header_row {header_row} in sheet '{sheet_name}' has no headers to update against.")

    header_row_values = header_values[0]
    if header_column > len(header_row_values):
        raise Exception(f"header_column {header_column} exceeds header row's column count ({len(header_row_values)}) in sheet '{sheet_name}'.")

    headers = header_row_values[header_column - 1:]
    row_numbers = [row_number]
else:
    # match_column/match_value targeting needs the whole sheet to find (and disambiguate) the row.
    range_param = quote(quoted_sheet_name, safe="")
    search_resp = kizen.api.get(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{range_param}")
    search_body = check_sheets_response(search_resp, "searching for row to update")
    values = search_body.get("values", [])

    if not values or header_row > len(values):
        raise Exception(f"header_row {header_row} exceeds sheet '{sheet_name}' row count ({len(values)}).")

    header_row_values = values[header_row - 1]
    if header_column > len(header_row_values):
        raise Exception(f"header_column {header_column} exceeds header row's column count ({len(header_row_values)}) in sheet '{sheet_name}'.")

    headers = header_row_values[header_column - 1:]
    if match_column not in headers:
        raise Exception(f"match_column '{match_column}' is not a header in sheet '{sheet_name}'. Headers: {headers}")

    matches = []
    for offset, data_row in enumerate(values[header_row:]):
        sliced_row = data_row[header_column - 1:]
        row = row_from_cells(sliced_row, headers)
        if row.get(match_column) == match_value:
            matches.append(header_row + offset + 1)
            if not update_all and len(matches) > 1:
                # Already ambiguous and update_all isn't set — no need to keep scanning the rest of the sheet.
                break

    if not matches:
        raise Exception(f"No row found where '{match_column}' = '{match_value}' in sheet '{sheet_name}'.")
    if len(matches) > 1 and not update_all:
        raise Exception(f"At least {len(matches)} rows match '{match_column}' = '{match_value}' (including rows {matches}) — ambiguous update target with update_all set to false. Use row_number instead to target one exactly, or remove update_all (or set it to true) to update every matching row.")

    row_numbers = matches

unknown_keys = [key for key in row_data if key not in headers]
if unknown_keys:
    raise Exception(f"row_data has key(s) not found in sheet '{sheet_name}' headers: {unknown_keys}. Headers: {headers}")

# headers[0] sits at column header_column itself, so no further offset is needed beyond each header's list index.
column_by_header = {header: header_column + i for i, header in enumerate(headers)}

batch_data = []
for target_row in row_numbers:
    for header, value in row_data.items():
        column_letter = column_number_to_letter(column_by_header[header])
        batch_data.append({
            "range": f"{quoted_sheet_name}!{column_letter}{target_row}",
            "values": [["" if value is None else str(value)]],
        })

batch_resp = kizen.api.post(
    f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate",
    json={"valueInputOption": "USER_ENTERED", "data": batch_data},
)
check_sheets_response(batch_resp, "updating row")

# update_all off means row_numbers is always a single-element list — output it as a bare number, not a JSON array.
outputs.rows_updated = json.dumps(row_numbers) if update_all else str(row_numbers[0])
outputs.success = True
