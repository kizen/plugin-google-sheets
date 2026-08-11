import json
from urllib.parse import quote

# Preview-qualified path for this unmerged PR (see plugin-wizard bot comment) — MUST revert to "/external-integrations/proxy/google_sheets/shared" before merging.
BASE_URL = "/external-integrations/proxy/google_sheets_preview_kzn_18007_spike_explore_feasibility_of_google_sheets_integration/shared"


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


def column_number_to_letter(n):
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def get_sheets_json(url, context):
    resp = kizen.api.get(url)
    try:
        payload = resp.json()
    except Exception:
        raise Exception(f"Google Sheets error {context}: unknown_error — HTTP {resp.status_code}")

    body = payload.get("body")
    if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
        raise_sheets_error(payload, context, resp.status_code)

    return body


spreadsheet_id = inputs.spreadsheet_id
sheet_name = inputs.sheet_name

header_row_input = getattr(inputs, "header_row", None)
header_row = int(header_row_input) if header_row_input is not None else 1
if header_row < 1:
    raise Exception(f"header_row must be 1 or greater, got {header_row}.")

header_column_input = getattr(inputs, "header_column", None)
header_column = int(header_column_input) if header_column_input is not None else 1
if header_column < 1:
    raise Exception(f"header_column must be 1 or greater, got {header_column}.")

row_number_raw = getattr(inputs, "row_number", None)
match_column = getattr(inputs, "match_column", None)
match_value = getattr(inputs, "match_value", None)

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
    header_body = get_sheets_json(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{header_range_param}", "reading header row")
    header_values = header_body.get("values", [])
    if not header_values:
        raise Exception(f"header_row {header_row} in sheet '{sheet_name}' has no headers to update against.")

    headers = header_values[0][header_column - 1:]
else:
    # match_column/match_value targeting needs the whole sheet to find (and disambiguate) the row.
    range_param = quote(quoted_sheet_name, safe="")
    body = get_sheets_json(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{range_param}", "searching for row to update")
    values = body.get("values", [])

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
        row = {header: (sliced_row[i] if i < len(sliced_row) else "") for i, header in enumerate(headers)}
        if row.get(match_column) == match_value:
            matches.append(header_row + offset + 1)

    if not matches:
        raise Exception(f"No row found where '{match_column}' = '{match_value}' in sheet '{sheet_name}'.")
    if len(matches) > 1:
        raise Exception(f"{len(matches)} rows match '{match_column}' = '{match_value}' (rows {matches}) — ambiguous update target. Use row_number instead to target one exactly.")

    row_number = matches[0]

unknown_keys = [key for key in row_data if key not in headers]
if unknown_keys:
    raise Exception(f"row_data has key(s) not found in sheet '{sheet_name}' headers: {unknown_keys}. Headers: {headers}")

batch_data = []
for header, value in row_data.items():
    column_number = header_column + headers.index(header)
    column_letter = column_number_to_letter(column_number)
    batch_data.append({
        "range": f"{quoted_sheet_name}!{column_letter}{row_number}",
        "values": [[str(value)]],
    })

resp = kizen.api.post(
    f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate",
    json={"valueInputOption": "USER_ENTERED", "data": batch_data},
)

try:
    payload = resp.json()
except Exception:
    raise Exception(f"Google Sheets error updating row: unknown_error — HTTP {resp.status_code}")

body = payload.get("body")
if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
    raise_sheets_error(payload, "updating row", resp.status_code)

outputs.row_number = row_number
outputs.success = True
