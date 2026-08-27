# Google Sheets Plugin for Kizen

Read, search, append, and update rows in Google Sheets directly from Kizen agentic workflows.

## Overview

This plugin connects a single Google Sheets account to Kizen and exposes four actions as Agentic Workflow Code Steps. All actions run as Python 3.13 Code Steps and communicate with the Google Sheets API through Kizen's external-integrations proxy.

Every action is a plain data-fetch/write primitive addressed by column header name (or by real sheet row number) rather than raw A1 notation — no summarization or extraction, which belongs in existing LLM action steps.

## Actions

| Action | Description |
| --- | --- |
| **Get Rows / Read Range** | Fetch rows from a sheet, keyed by header name, with optional single-column filtering. |
| **Search Rows** | Find row(s) by exact column value; returns the matches and their real sheet row numbers. |
| **Append Row** | Add a new row to a sheet, keyed by header name via a single JSON object. |
| **Update Row/Cell** | Update specific cells within one row, targeted by row number or column match, without touching the rest of the row. |

An earlier fifth action, Create Spreadsheet/Tab (bare create, folder placement, template copying), was built and verified but removed from the released action set along with its Google Drive scope dependency — see version control history if it's revisited.

## Authentication

This plugin uses a single **business-level** Google OAuth connection — one shared Google account connects on behalf of the whole business, rather than each user connecting individually.

This is a requirement, not a design choice: every action runs as a Kizen Code Step, which always executes as a fixed service account. Code Steps cannot complete an interactive OAuth consent flow, so a per-user connection isn't possible here.

**Trade-off:** all actions act under one shared identity. Access to a given spreadsheet depends on that shared account already having been granted access to it.

**Caller-identity restriction.** The `shared` service in `kizen.json` sets `"scope": "service-account-only"` — a Kizen platform field, distinct from the Google OAuth scopes below, enforced by Kizen's proxy on every request. It restricts the service so only this plugin's own packaged Code Steps can call it; a generic code step or any frontend/browser caller gets a 403. Note: this also blocks the Kizen dev toolkit's own "Remote runner" test execution with the same 403 — if further testing is needed against this branch, this field will need to be temporarily unset again, republished, and re-added afterward.

### Scopes

| Scope | Classification | Purpose |
| --- | --- | --- |
| `spreadsheets` | Sensitive, not Restricted | Read/write access for all four actions. |
| `userinfo.email`, `userinfo.profile` | — | Displays the connected account in the setup assistant. |

`spreadsheets` is Sensitive but not Restricted, so it does not require an annual CASA (Cloud Application Security Assessment) before production use.

Any scope change requires reconnecting the OAuth connection — an existing token doesn't retroactively gain a new scope — and adding the scope to the GCP OAuth consent screen's own scope list; declaring it in `kizen.json` alone is not sufficient.

## Action Reference

### Get Rows / Read Range

| Input | Required | Notes |
| --- | --- | --- |
| `spreadsheet_id` | Yes | From the sheet's URL. |
| `sheet_name` | Yes | Tab name. |
| `header_row` | No | 1-indexed header row. Blank defaults to `1`. |
| `header_column` | No | 1-indexed header column. Blank defaults to `1`. Columns to the left are dropped from both headers and data. |
| `filter_column` | No | Header name to filter on. Required together with `filter_value` — setting either one without the other raises an error. |
| `filter_value` | No | Exact match only. |

**Outputs:** `rows` (JSON array string of row objects keyed by header name), `row_count`.

Fetches the whole sheet, treats `header_row`/`header_column` as the top-left of the real table, and keys each row against the header names found there. Out-of-range values raise a clear error rather than an unhandled exception.

### Search Rows

| Input | Required | Notes |
| --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | Same as Get Rows. |
| `column_name` | Yes | Header name to search. |
| `match_value` | Yes | Exact match only. |
| `return_all_matches` | Yes | `true` (default): every matching row. `false`: stops at the first match. |

**Outputs:** `matching_rows` (JSON array string), `row_numbers` (JSON array of the matches' real 1-indexed sheet row numbers — feeds directly into Update Row/Cell's `row_number` input).

No matches returns empty arrays for both outputs rather than an error.

### Append Row

| Input | Required | Notes |
| --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | Same as Get Rows/Search Rows. |
| `row_data` | Yes | JSON object keyed by header name, e.g. `{"Name": "Jane"}`. Unknown keys raise an error; missing headers default to blank. |

**Outputs:** `row_number` (the real sheet row the data landed on, parsed from Google's response), `spreadsheet_id`.

Single-row only — batch appends require multiple calls. Written with `valueInputOption=USER_ENTERED`, so values are interpreted the same way as typed input (e.g. `"3/4/1995"` becomes a real date).

### Update Row/Cell

| Input | Required | Notes |
| --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | Same as other actions. |
| `row_number` | Conditional | Real 1-indexed row to update, typically from a prior Search Rows. Provide this OR `match_column`/`match_value`, not both. |
| `match_column` | Conditional | Header name to search for the target row. Required together with `match_value`. |
| `match_value` | Conditional | Exact match only. |
| `update_all` | Yes | Only applies to `match_column`/`match_value` targeting. Defaults to `true` — every row matching `match_column`/`match_value` is updated. Set to `false` to require a single unambiguous match instead, raising an error if more than one row matches. No effect when targeting by `row_number`. |
| `row_data` | Yes | JSON object keyed by header name — only these columns change. |

**Outputs:** `rows_updated` (the row(s) updated — a bare number like `5` if `update_all` is `false`, or a JSON array string like `[5, 6, 7]` if `update_all` is `true`, even when only one row matched), `success` (boolean; failures raise an exception rather than returning `false`).

A partial update, not a full-row overwrite — only the columns named in `row_data` are touched. By default, `match_column`/`match_value` matching more than one row updates all of them; set `update_all` to `false` to instead require an exact single match, raising an "ambiguous update target" error listing the conflicting rows found so far.

## Known Limitations

- **No change-notification trigger.** The Sheets API has no push/webhook mechanism. A native Kizen Scheduled trigger paired with Get Rows or Search Rows supports a "check periodically" pattern with no new plugin code; diffing against previously seen data is left to the workflow.
- **Single-row writes only.** Append Row writes one row per call.
- **No spreadsheet or Drive-level operations.** No spreadsheet/tab creation, no moving, sharing, or permission changes — this plugin is Sheets-data-only.
- **No pagination.** Get Rows and Search Rows fetch the entire sheet in one call; very large sheets may be slow or approach response size limits.
- **`header_row`/`header_column` are strings, not numbers**, by design: an optional (`required: false`) numeric input in this framework crashes the entire run if left blank, regardless of the `required` flag. Blank values default to `1` in script.
- **`row_data` values are interpreted like typed input, not stored literally.** Append Row and Update Row/Cell write with `valueInputOption=USER_ENTERED`, which behaves exactly like typing directly into a cell — this is what lets a date string become a real date, but it has two consequences worth knowing:
  - A numeric-looking string like `"00501"` (e.g. a zip code) becomes the number `501`, losing its leading zeros.
  - A string starting with `=` is executed as a live formula (confirmed: `"=1+1"` evaluates to `2`, not stored as text) — a real risk if `row_data` values can ever come from external or untrusted input flowing through a workflow, not just a display quirk.

  Both are avoided the same way: prefix the value with an apostrophe in `row_data`, e.g. `{"Zip": "'00501"}` or `{"Status": "'=1+1"}` — the standard Sheets/Excel convention for forcing plain text. The apostrophe itself isn't stored or displayed. Confirmed to neutralize both cases.
- **Reserved input/output names exist with no published list.** The shortest, most generic word is the first suspect.

## Development

- Runtime: Python 3.13, executed as Kizen Agentic Workflow Code Steps.
- HTTP calls to Google go through Kizen's runtime-injected `kizen.api` client (`get`/`post`/`patch`/`put`/`delete`), which proxies requests via `/external-integrations/proxy/{plugin_api_name}/{service_name}/{api_path}`.
- Kizen's proxy wraps every upstream response in an envelope (`{"status_code", "response_headers", "body"}`) — scripts read through the `body` key, not the top-level response.
- The Sheets API requires `base_service_url: https://sheets.googleapis.com`, not the generic `www.googleapis.com/sheets/v4/...` path Drive uses for its own API — that combination returns Google's generic branded 404 page, not a Sheets API error.
- Local development and testing use the Kizen App Development Toolkit.

```text
plugin-google-sheets/
├── kizen.json
├── README.md
├── releaseNotes/
│   └── 1.0.0.md
└── src/
    └── automationSteps/
        ├── get_rows/
        ├── search_rows/
        ├── append_row/
        └── update_row/
            (each: config.json + script.py)
```
