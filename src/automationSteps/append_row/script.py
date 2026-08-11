import json
import re
from urllib.parse import quote

# While this PR is unmerged, Kizen deploys under the preview-qualified api_name below,
# not the plain "google_sheets" — confirmed via this PR's plugin-wizard bot comment.
# MUST be reverted to "/external-integrations/proxy/google_sheets/shared" before merging to main.
BASE_URL = "/external-integrations/proxy/google_sheets_preview_kzn_18007_spike_explore_feasibility_of_google_sheets_integration/shared"


def raise_sheets_error(payload, context, fallback_status):
    # Kizen's proxy wraps a successful upstream call as {"status_code", "response_headers",
    # "body": <upstream response>} — a relayed Google error lives at payload["body"]["error"].
    # A proxy-level error (routing/auth/content-type) is Kizen's own flat, unwrapped shape.
    # The upstream body isn't always JSON either (e.g. a wrong host/path returns Google's
    # generic HTML 404 page) — body.get(...) below would itself crash with an unhelpful
    # AttributeError if not guarded by isinstance.
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
    # A1 notation always accepts a quoted sheet name, spaces or not, so quote
    # unconditionally rather than guessing when it's required.
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

try:
    row_data = json.loads(inputs.row_data)
except Exception as e:
    raise Exception(f"row_data must be a valid JSON object string: {e}")

if not isinstance(row_data, dict):
    raise Exception(f"row_data must be a JSON object (e.g. {{\"Name\": \"Jane\"}}), got: {inputs.row_data}")

quoted_sheet_name = a1_quote_sheet_name(sheet_name)

# Only the header row is needed to determine column order — no need to fetch the whole sheet.
header_range_param = quote(f"{quoted_sheet_name}!{header_row}:{header_row}", safe="")
header_body = get_sheets_json(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{header_range_param}", "reading header row")

header_values = header_body.get("values", [])
if not header_values:
    raise Exception(f"header_row {header_row} in sheet '{sheet_name}' has no headers to append against.")

headers = header_values[0][header_column - 1:]

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

try:
    payload = resp.json()
except Exception:
    raise Exception(f"Google Sheets error appending row: unknown_error — HTTP {resp.status_code}")

body = payload.get("body")
if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
    raise_sheets_error(payload, "appending row", resp.status_code)

updated_range = body.get("updates", {}).get("updatedRange", "")
match = re.search(r"![A-Za-z]+(\d+)", updated_range)
if not match:
    raise Exception(f"Could not determine the appended row number from Google's response (updatedRange: '{updated_range}').")

outputs.row_number = int(match.group(1))
outputs.spreadsheet_id = spreadsheet_id
