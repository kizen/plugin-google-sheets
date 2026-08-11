import json
from urllib.parse import quote

# NOTE: while testing an unmerged PR, Kizen deploys under
# "{api_name}_preview_{branch_name_slugified}" instead of the plain api_name
# (learned the hard way on plugin-google-drive — every proxy call 404s otherwise).
# Check the PR's plugin-wizard bot comment for the current preview name.
BASE_URL = "/external-integrations/proxy/google_sheets/shared"


def raise_sheets_error(resp, context):
    # Kizen's proxy wraps a successful upstream call as {"status_code", "response_headers",
    # "body": <upstream response>} — a relayed Google error lives at payload["body"]["error"].
    # A proxy-level error (routing/auth/content-type) is Kizen's own flat, unwrapped shape.
    try:
        payload = resp.json()
    except Exception:
        raise Exception(f"Google Sheets error {context}: unknown_error — HTTP {resp.status_code}")

    body = payload.get("body")
    google_error = body.get("error") if isinstance(body, dict) else None
    if isinstance(google_error, dict):
        message = google_error.get("message", f"HTTP {resp.status_code}")
        status = google_error.get("status", "unknown_error")
        raise Exception(f"Google Sheets error {context}: {status} — {message}")

    kizen_error = payload.get("error") or payload.get("detail")
    if kizen_error:
        raise Exception(f"Google Sheets error {context}: proxy_error — {kizen_error}")

    raise Exception(f"Google Sheets error {context}: unknown_error — HTTP {resp.status_code}")


def a1_quote_sheet_name(name):
    # A1 notation always accepts a quoted sheet name, spaces or not, so quote
    # unconditionally rather than guessing when it's required.
    return "'" + name.replace("'", "''") + "'"


spreadsheet_id = inputs.spreadsheet_id
sheet_name = inputs.sheet_name
filter_column = getattr(inputs, "filter_column", None)
filter_value = getattr(inputs, "filter_value", None)

if filter_column and not filter_value:
    raise Exception("filter_value is required when filter_column is set.")

range_param = quote(a1_quote_sheet_name(sheet_name), safe="")

resp = kizen.api.get(f"{BASE_URL}/sheets/v4/spreadsheets/{spreadsheet_id}/values/{range_param}")
if not resp.ok:
    raise_sheets_error(resp, "reading rows")

values = resp.json().get("body", {}).get("values", [])

if not values:
    outputs.rows = json.dumps([])
    outputs.row_count = 0
else:
    headers = values[0]
    data_rows = values[1:]

    if filter_column and filter_column not in headers:
        raise Exception(f"filter_column '{filter_column}' is not a header in sheet '{sheet_name}'. Headers: {headers}")

    rows = []
    for data_row in data_rows:
        row = {header: (data_row[i] if i < len(data_row) else "") for i, header in enumerate(headers)}
        if filter_column and row.get(filter_column) != filter_value:
            continue
        rows.append(row)

    outputs.rows = json.dumps(rows)
    outputs.row_count = len(rows)
