import json
from urllib.parse import quote

# Preview-qualified path for this unmerged PR (see plugin-wizard bot comment) — MUST revert to "/external-integrations/proxy/google_sheets/shared" before merging.
BASE_URL = "/external-integrations/proxy/google_sheets_preview_kzn_18007_spike_explore_feasibility_of_google_sheets_integration/shared"

# Drive API call (www.googleapis.com), so it needs its own service — "shared" is pinned to sheets.googleapis.com; same MUST-revert caveat as BASE_URL applies here too.
DRIVE_BASE_URL = "/external-integrations/proxy/google_sheets_preview_kzn_18007_spike_explore_feasibility_of_google_sheets_integration/shared_drive"


def raise_sheets_error(payload, context, fallback_status):
    # Proxy wraps upstream calls as {status_code, body}; a Google error lives at body["error"], a proxy error is flat, and body may not be a dict at all (e.g. HTML on a wrong host) — hence the isinstance guard.
    body = payload.get("body")
    google_error = body.get("error") if isinstance(body, dict) else None
    if isinstance(google_error, dict):
        message = google_error.get("message", "unknown_error")
        status = google_error.get("status", "unknown_error")
        raise Exception(f"Google API error {context}: {status} — {message}")

    kizen_error = payload.get("error") or payload.get("detail")
    if kizen_error:
        raise Exception(f"Google API error {context}: proxy_error — {kizen_error}")

    upstream_status = payload.get("status_code", fallback_status)
    snippet = str(body)[:200] if body is not None else "no body"
    raise Exception(f"Google API error {context}: unknown_error — upstream HTTP {upstream_status}, body: {snippet}")


def a1_quote_sheet_name(sheet_name):
    # Quote unconditionally — always valid in A1 notation, so no need to guess when it's required.
    return "'" + sheet_name.replace("'", "''") + "'"


def check_sheets_response(resp, context):
    try:
        payload = resp.json()
    except Exception:
        raise Exception(f"Google API error {context}: unknown_error — HTTP {resp.status_code}")

    body = payload.get("body")
    if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
        raise_sheets_error(payload, context, resp.status_code)

    return body


spreadsheet_name = inputs.spreadsheet_name
template_spreadsheet_id = getattr(inputs, "template_spreadsheet_id", None)
header_row_values_raw = getattr(inputs, "header_row_values", None)
folder_id = getattr(inputs, "folder_id", None)

if template_spreadsheet_id:
    raise Exception(
        "template_spreadsheet_id is not yet supported: copying from a template needs a Google Drive "
        "API scope this plugin doesn't have, and it's unconfirmed whether drive.file would even be "
        "sufficient (see CLAUDE.md's scope plan). Leave template_spreadsheet_id blank to create a bare "
        "spreadsheet instead."
    )

header_row_values = None
if header_row_values_raw:
    try:
        header_row_values = json.loads(header_row_values_raw)
    except Exception as e:
        raise Exception(f"header_row_values must be a valid JSON array string: {e}")

    if not isinstance(header_row_values, list):
        raise Exception(f"header_row_values must be a JSON array (e.g. [\"Name\", \"Email\"]), got: {header_row_values_raw}")

body = check_sheets_response(
    kizen.api.post(f"{BASE_URL}/v4/spreadsheets", json={"properties": {"title": spreadsheet_name}}),
    "creating spreadsheet",
)

new_spreadsheet_id = body.get("spreadsheetId")
sheets = body.get("sheets", [])
if not new_spreadsheet_id or not sheets:
    raise Exception(f"Google API error creating spreadsheet: response missing spreadsheetId/sheets: {body}")

sheet_name = sheets[0].get("properties", {}).get("title")
if not sheet_name:
    raise Exception(f"Google API error creating spreadsheet: response missing the new sheet's title: {body}")

if header_row_values:
    quoted_sheet_name = a1_quote_sheet_name(sheet_name)
    header_range_param = quote(f"{quoted_sheet_name}!A1", safe="")
    check_sheets_response(
        kizen.api.put(
            f"{BASE_URL}/v4/spreadsheets/{new_spreadsheet_id}/values/{header_range_param}?valueInputOption=RAW",
            json={"values": [[str(v) for v in header_row_values]]},
        ),
        "writing header row",
    )

if folder_id:
    # The create call always lands the file in My Drive's root — remove "root" as a parent and add folder_id via the Drive API.
    move_url = (
        f"{DRIVE_BASE_URL}/drive/v3/files/{new_spreadsheet_id}"
        f"?addParents={quote(folder_id, safe='')}&removeParents=root&fields=id,parents"
    )
    check_sheets_response(kizen.api.patch(move_url, json={}), "moving spreadsheet into folder_id")

outputs.spreadsheet_id = new_spreadsheet_id
outputs.sheet_name = sheet_name
