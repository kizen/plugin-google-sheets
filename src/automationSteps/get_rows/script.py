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
filter_column = getattr(inputs, "filter_column", None)
filter_value = getattr(inputs, "filter_value", None)

if filter_column and not filter_value:
    raise Exception("filter_value is required when filter_column is set.")

range_param = quote(a1_quote_sheet_name(sheet_name), safe="")

resp = kizen.api.get(f"{BASE_URL}/v4/spreadsheets/{spreadsheet_id}/values/{range_param}")

try:
    payload = resp.json()
except Exception:
    raise Exception(f"Google Sheets error reading rows: unknown_error — HTTP {resp.status_code}")

body = payload.get("body")
if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
    raise_sheets_error(payload, "reading rows", resp.status_code)

values = body.get("values", [])

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
