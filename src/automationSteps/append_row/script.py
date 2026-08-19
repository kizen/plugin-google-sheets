import json
import re
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


spreadsheet_id = inputs.spreadsheet_id
sheet_name = inputs.sheet_name

header_row = resolve_positive_int(getattr(inputs, "header_row", None), "header_row")
header_column = resolve_positive_int(getattr(inputs, "header_column", None), "header_column")

try:
    row_data = json.loads(inputs.row_data)
except Exception as e:
    raise Exception(f"row_data must be a valid JSON object string: {e}")

if not isinstance(row_data, dict):
    raise Exception(f"row_data must be a JSON object (e.g. {{\"Name\": \"Jane\"}}), got: {inputs.row_data}")

quoted_sheet_name = a1_quote_sheet_name(sheet_name)

# Only the header row is needed to determine column order — no need to fetch the whole sheet.
header_range_param = quote(f"{quoted_sheet_name}!{header_row}:{header_row}", safe="")
header_resp = kizen.api.get(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{header_range_param}")
header_body = check_sheets_response(header_resp, "reading header row")

header_values = header_body.get("values", [])
if not header_values:
    raise Exception(f"header_row {header_row} in sheet '{sheet_name}' has no headers to append against.")

header_row_values = header_values[0]
if header_column > len(header_row_values):
    raise Exception(f"header_column {header_column} exceeds header row's column count ({len(header_row_values)}) in sheet '{sheet_name}'.")

headers = header_row_values[header_column - 1:]

unknown_keys = [key for key in row_data if key not in headers]
if unknown_keys:
    raise Exception(f"row_data has key(s) not found in sheet '{sheet_name}' headers: {unknown_keys}. Headers: {headers}")

ordered_values = [str(row_data.get(header, "")) for header in headers]

start_column_letter = column_number_to_letter(header_column)
append_range_param = quote(f"{quoted_sheet_name}!{start_column_letter}{header_row}", safe="")
append_url = (
    f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{append_range_param}:append"
    f"?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
)

resp = kizen.api.post(append_url, json={"values": [ordered_values]})
body = check_sheets_response(resp, "appending row")

updated_range = body.get("updates", {}).get("updatedRange", "")
match = re.search(r"![A-Za-z]+(\d+)", updated_range)
if not match:
    raise Exception(f"Could not determine the appended row number from Google's response (updatedRange: '{updated_range}').")

outputs.row_number = int(match.group(1))
outputs.spreadsheet_id = spreadsheet_id
