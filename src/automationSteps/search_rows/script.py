import json
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


spreadsheet_id = inputs.spreadsheet_id
sheet_name = inputs.sheet_name
column_name = inputs.column_name
match_value = inputs.match_value
return_all_matches = inputs.return_all_matches

header_row_input = getattr(inputs, "header_row", None)
header_row = int(header_row_input) if header_row_input is not None else 1
if header_row < 1:
    raise Exception(f"header_row must be 1 or greater, got {header_row}.")

header_column_input = getattr(inputs, "header_column", None)
header_column = int(header_column_input) if header_column_input is not None else 1
if header_column < 1:
    raise Exception(f"header_column must be 1 or greater, got {header_column}.")

range_param = quote(a1_quote_sheet_name(sheet_name), safe="")

resp = kizen.api.get(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{range_param}")

try:
    payload = resp.json()
except Exception:
    raise Exception(f"Google Sheets error searching rows: unknown_error — HTTP {resp.status_code}")

body = payload.get("body")
if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
    raise_sheets_error(payload, "searching rows", resp.status_code)

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
    # offset is 0-indexed within the data rows; the real sheet row number (1-indexed)
    # for offset 0 is header_row + 1 (the row right after the header row).
    for offset, data_row in enumerate(values[header_row:]):
        sliced_row = data_row[header_column - 1:]
        row = {header: (sliced_row[i] if i < len(sliced_row) else "") for i, header in enumerate(headers)}
        if row.get(column_name) != match_value:
            continue

        matching_rows.append(row)
        row_numbers.append(header_row + offset + 1)

        if not return_all_matches:
            break

    outputs.matching_rows = json.dumps(matching_rows)
    outputs.row_numbers = json.dumps(row_numbers)
