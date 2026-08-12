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
    # Handles both proxy-level failures (resp.ok False) and upstream failures wrapped inside a 200 envelope.
    try:
        payload = resp.json()
    except Exception:
        raise Exception(f"Google API error {context}: unknown_error — HTTP {resp.status_code}")

    body = payload.get("body")
    if not resp.ok or payload.get("status_code", 200) >= 400 or not isinstance(body, dict):
        raise_sheets_error(payload, context, resp.status_code)

    return body


def resolve_first_sheet_name(response, context):
    # Shared by both creation paths — each locates the first tab's title in a differently-shaped response.
    sheets = response.get("sheets", [])
    if not sheets:
        raise Exception(f"Google API error {context}: response missing sheets: {response}")

    sheet_name = sheets[0].get("properties", {}).get("title")
    if not sheet_name:
        raise Exception(f"Google API error {context}: response missing the first sheet's title: {response}")

    return sheet_name


spreadsheet_name = inputs.spreadsheet_name
template_spreadsheet_id = getattr(inputs, "template_spreadsheet_id", None)
header_row_values_raw = getattr(inputs, "header_row_values", None)
folder_id = getattr(inputs, "folder_id", None)

header_row_values = None
if header_row_values_raw:
    try:
        header_row_values = json.loads(header_row_values_raw)
    except Exception as e:
        raise Exception(f"header_row_values must be a valid JSON array string: {e}")

    if not isinstance(header_row_values, list):
        raise Exception(f"header_row_values must be a JSON array (e.g. [\"Name\", \"Email\"]), got: {header_row_values_raw}")

if template_spreadsheet_id:
    # Setting parents here does the folder placement in the same call — no separate move step needed on this path.
    copy_body = {"name": spreadsheet_name}
    if folder_id:
        copy_body["parents"] = [folder_id]

    copy_response = check_sheets_response(
        kizen.api.post(
            f"{DRIVE_BASE_URL}/drive/v3/files/{template_spreadsheet_id}/copy?supportsAllDrives=true&fields=id",
            json=copy_body,
        ),
        "copying template spreadsheet",
    )

    new_spreadsheet_id = copy_response.get("id")
    if not new_spreadsheet_id:
        raise Exception(f"Google API error copying template spreadsheet: response missing id: {copy_response}")

    # Drive's file resource doesn't expose the spreadsheet's internal sheets, hence this separate metadata call.
    metadata = check_sheets_response(
        kizen.api.get(f"{BASE_URL}/v4/spreadsheets/{new_spreadsheet_id}?fields=sheets.properties.title"),
        "reading copied spreadsheet's sheet names",
    )
    sheet_name = resolve_first_sheet_name(metadata, "copying template spreadsheet")
else:
    body = check_sheets_response(
        kizen.api.post(f"{BASE_URL}/v4/spreadsheets", json={"properties": {"title": spreadsheet_name}}),
        "creating spreadsheet",
    )

    new_spreadsheet_id = body.get("spreadsheetId")
    if not new_spreadsheet_id:
        raise Exception(f"Google API error creating spreadsheet: response missing spreadsheetId: {body}")

    sheet_name = resolve_first_sheet_name(body, "creating spreadsheet")

    if folder_id:
        # The create call always lands the file in My Drive's root — remove "root" as a parent and add folder_id via the Drive API.
        move_url = (
            f"{DRIVE_BASE_URL}/drive/v3/files/{new_spreadsheet_id}"
            f"?addParents={quote(folder_id, safe='')}&removeParents=root&fields=id,parents"
        )
        check_sheets_response(kizen.api.patch(move_url, json={}), "moving spreadsheet into folder_id")

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

outputs.spreadsheet_id = new_spreadsheet_id
outputs.sheet_name = sheet_name
