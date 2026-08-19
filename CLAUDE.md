# Google Sheets Plugin — Developer Context

Reads, searches, and writes rows in Google Sheets from Kizen agentic workflows via a single business-level OAuth connection. This is spike KZN-18007. Every action is a plain data-fetch/write primitive against the Sheets API — no summarization or extraction, which belongs in existing LLM action steps.

---

## What This Plugin Does (v1)

Per the spike ticket: Get Rows / Read Range, Search Rows, Append Row, Update Row/Cell, Create Spreadsheet/Tab, and (if feasible) a New Row Added trigger.

1. **Get Rows / Read Range** — fetches rows keyed by header name, with optional single-column filtering. Built.
2. **Search Rows** — finds rows by exact column value; returns matches and their real sheet row numbers. Built.
3. **Append Row** — adds a row via a single JSON object keyed by header name (no dynamic per-column inputs — see below). Built.
4. **Update Row/Cell** — updates specific cells within one row, targeted by row number or column match, without touching the rest of the row. Built.

No trigger was built — see Known Constraints.

**Create Spreadsheet/Tab was built and fully verified during the spike** (both a bare create and a template-copy path, including `folder_id` placement) but has since been removed from the v1 action set, along with its Drive dependency. All four remaining actions are Sheets-only — see Auth Method below for the resulting scope.

---

## Auth Method

### OAuth 2.0 — business-level

- A single shared Google account (`auth_level: business`, service `shared` in `kizen.json`) — same pattern as `plugin-google-drive`, not a per-user connection like Calendar.
- **Why business-level:** every action runs as a Kizen Code Step, which executes as a fixed service account and cannot complete an interactive OAuth consent flow. A `user`-level service returns a 503 (`"User must authorize this service"`) regardless of authorization attempts — documented in `plugin-google-drive`'s CLAUDE.md after being discovered mid-build there. This plugin starts at `business` from the outset.
- Trade-off: all actions act as one shared identity. Access to a given spreadsheet depends on that shared account having access to it.

**Scopes:**

| Scope | Classification | Purpose |
| --- | --- | --- |
| `spreadsheets` | Sensitive, not Restricted | Read/write for all four actions. Upgraded from `spreadsheets.readonly` when Append Row required write access. |
| `userinfo.email`, `userinfo.profile` | — | "Connected as {email}" in the setup assistant. |

`spreadsheets`/`spreadsheets.readonly` are Sensitive but not Restricted — no annual CASA assessment required. Confirmed against [Google's Sheets API scopes docs](https://developers.google.com/workspace/sheets/api/scopes).

Any scope change requires reconnecting the OAuth connection — an existing token doesn't retroactively gain a new scope — and adding the scope to the GCP OAuth consent screen's own scope list; declaring it in `kizen.json` alone is not sufficient.

**Historical note — Drive scope, removed.** Create Spreadsheet/Tab (removed from v1; see What This Plugin Does) needed Google's full `drive` scope — Restricted, requiring a CASA security assessment before production use — because `template_spreadsheet_id` could point at a spreadsheet this app never created, a case `drive.file` can't reach. That action also drove a platform-level finding worth keeping: Kizen's proxy pins one host per `service_name`, so reaching both `sheets.googleapis.com` and `www.googleapis.com` (Drive) from one service required a second service definition (`shared_drive`) — until `additional_service_urls` + a per-request `full_domain` query param was confirmed as a working replacement, letting one service reach both hosts with one OAuth connection. With Create Spreadsheet/Tab gone, `drive` and `additional_service_urls` are no longer requested/declared at all — see `kizen.json`. If Drive-backed functionality returns to this plugin later, this mechanism is the confirmed path, not `shared_drive`-style multi-service.

**Scope plan by action:**

| Action | Scope | Notes |
| --- | --- | --- |
| Get Rows, Search Rows, Append Row, Update Row/Cell | `spreadsheets` | Current — the only scope this plugin requests. |
| New Row Added trigger | None — no Sheets push mechanism | Cut from v1; see Known Constraints. |

**Proxy URL pattern:**

```text
/external-integrations/proxy/{plugin_api_name}/{service_name}/{api_path}
```

`{plugin_api_name}` is `google_sheets` only once merged and published. While a PR is open, Kizen deploys under `{api_name}_preview_{branch_slug}` — check the PR's `plugin-wizard` bot comment for the current name.

The Sheets API requires `base_service_url: https://sheets.googleapis.com`, not the generic `www.googleapis.com/sheets/v4/...` path Drive uses for its own API. That combination returns Google's generic branded 404 page, not a Sheets API error.

---

## kizen.api

Runtime-injected HTTP client, consistent across this workspace:

```python
kizen.api.get(url, params=None, headers=None)
kizen.api.post(url, data=None, json=None, headers=None)
kizen.api.patch(url, data=None, json=None, headers=None)
kizen.api.put(url, data=None, json=None, headers=None)
kizen.api.delete(url, headers=None)
```

`inputs`/`outputs` are runtime-injected globals: read `inputs.<script_alias>`, write `outputs.<script_alias> = ...`. Optional inputs must be read with `getattr(inputs, "name", None)` — direct attribute access raises `AttributeError` when the input is unwired.

Kizen's proxy wraps every upstream response in an envelope: `{"status_code", "response_headers", "body": <upstream response>}`. Scripts must read through `resp.json()["body"]`, not `resp.json()` directly.

---

## Agentic Workflow Steps

### get_rows

File: `src/automationSteps/get_rows/script.py`

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id` | string | yes | From the sheet's URL. |
| `sheet_name` | string | yes | Tab name. Always quoted in A1 notation. |
| `header_row` | string | no | 1-indexed header row. Blank defaults to `1` (script-side, not a platform `default`) — see Known Constraints for why this is a string, not a number. |
| `header_column` | string | no | 1-indexed header column. Blank defaults to `1`. Columns to the left are dropped from both headers and data. |
| `filter_column` | string | no | Header name to filter on. |
| `filter_value` | string | no | Required if `filter_column` is set; exact match only. |

| Output | Type | Notes |
| --- | --- | --- |
| `rows` | string | JSON array of row objects keyed by header name (no native array `data_type` in this framework). |
| `row_count` | number | |

Fetches the whole sheet via `values.get`, treats `header_row`/`header_column` as the top-left of the real table, and keys each subsequent row against the header names found there. Rows above `header_row` and columns left of `header_column` are dropped, not returned as data. Out-of-range values raise a clear error rather than an `IndexError`.

Verified end-to-end against a live sheet. Two issues surfaced and were fixed:
1. `base_service_url` must be `https://sheets.googleapis.com`; the generic `www.googleapis.com/sheets/v4/...` path returns a branded HTML 404, not an API error.
2. Error handling must check the wrapped `status_code` in the response body, not just `resp.ok` — the proxy returns HTTP 200 even when the upstream call failed, so an unhandled non-JSON body previously crashed with an opaque `AttributeError`.

Also confirmed: publishing is required before a plugin can be installed for testing, and while a PR is open, the OAuth consent screen needs both the test Google account added under **Test users** and the exact scopes added to its own scope list — declaring them in `kizen.json` is not sufficient.

`values.get` without `valueRenderOption` returns `FORMATTED_VALUE` (display strings, e.g. `"11/19/1990"`), not raw values.

**Framework limitation — optional numeric inputs.** An input with `data_type: "number"` and `required: false` crashes the entire run (`KizenConversionError: Failed to convert value '' to float`) before the script executes, whenever the field is left blank — the runtime unconditionally calls `float()` on the raw value regardless of whether the input is required. `string` inputs don't have this problem. Adding a `"default"` while keeping `required: false` does not help — `default` only pre-fills the UI when `required: true`.

`header_row`/`header_column` originally worked around this the same way as `plugin-mysql`'s `mysql_read.return_single_value`: `required: true` with a `"default"`. That avoids the crash, but forces every caller to always see a prefilled value rather than a genuinely optional field. These two inputs have since been migrated to the pattern used by `update_row`'s `row_number`: `data_type: "string"`, `required: false`, no platform `default` — the script's `resolve_positive_int` helper treats a blank/omitted value as `1` and raises a clear error on a non-numeric string, instead of the raw `KizenConversionError`. This is now the preferred pattern for any future numeric-like input on this plugin; the `required: true` + `default` workaround is a fallback only where genuine optionality isn't needed.

A demo-only duplicate of `get_rows`, `get_rows_number_input`, was kept temporarily with the original `data_type: "number"` inputs to show this design constraint side-by-side with the fix live; it has since been removed after serving that purpose.

### search_rows

File: `src/automationSteps/search_rows/script.py`

Same header-resolution mechanics as `get_rows`, but always filters (`column_name`/`match_value`, both required, exact match) and adds `return_all_matches` (boolean, `required: true` with `default: true`, same defaulted-input pattern).

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | — | Same as `get_rows`. |
| `column_name` | string | yes | Header name to search — always required, unlike `get_rows`' `filter_column`. |
| `match_value` | string | yes | Exact match only. |
| `return_all_matches` | boolean | yes | `true` (default): every matching row. `false`: stops at the first match. |

| Output | Type | Notes |
| --- | --- | --- |
| `matching_rows` | string | JSON array of matching row objects keyed by header name. |
| `row_numbers` | string | JSON array of the matches' real 1-indexed sheet row numbers, not array indices — e.g. `[4, 7]` means sheet rows 4 and 7. Deliberately real row numbers so the output can feed directly into `update_row`'s `row_number` input. |

Verified end-to-end, including both branches of `return_all_matches` against a sheet with a genuine duplicate value: `true` returned both matches, `false` stopped at the first. Also confirms the runtime coerces the literal string `"false"` to Python `False` correctly rather than truthy-casting any non-empty string.

Leaving `return_all_matches` blank does not crash (unlike a blank numeric input) — boolean coercion of `''` doesn't raise, though the resulting behavior should not be relied on; set it explicitly.

**Testing note:** setting a React-controlled input's `.value` directly via injected JS does not register with the dev toolkit's form state — the run submits the stale value. Use a real form-fill action or keystroke, not direct DOM assignment.

### append_row

File: `src/automationSteps/append_row/script.py`

**Not one input per mapped column, despite the ticket's proposal.** `config.json` is static and can't know a spreadsheet's headers in advance, and no plugin in this workspace supports dynamically generated per-column inputs (`plugin-mysql`'s `mysql_write` faces the same underlying problem and uses a raw query string instead). `row_data` is a JSON object string keyed by header name.

Requires the `spreadsheets` write scope (see Auth Method).

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | — | Same as `get_rows`/`search_rows`. |
| `row_data` | string | yes | JSON object keyed by header name, e.g. `{"Name": "Jane"}`. Unknown keys raise an error; missing headers default to blank. |

| Output | Type | Notes |
| --- | --- | --- |
| `row_number` | number | Parsed from Google's `updates.updatedRange` response (e.g. `"Sheet1!A4:D4"` → `4`), not assumed from a local row count. |
| `spreadsheet_id` | string | Echoes the input, per the ticket's spec. |

`row_data` behavior:
- Missing headers default to a blank cell.
- Unknown keys raise a clear error rather than being silently dropped or misplaced.
- Key order in the JSON object doesn't matter — values are reordered to match the sheet's actual header order.
- Written with `valueInputOption=USER_ENTERED`, so values are interpreted the same way as typed input (e.g. `"3/4/1995"` becomes a real date).

Single-row only — batch appends require multiple calls.

Column position (`header_column` + offset) is converted to an A1 column letter via a standalone `column_number_to_letter` helper, verified against known values (`26`→`Z`, `52`→`AZ`, `702`→`ZZ`, `703`→`AAA`).

Verified end-to-end after the OAuth reconnect for the `spreadsheets` write scope.

### update_row

File: `src/automationSteps/update_row/script.py`

**A partial update, not a full-row overwrite.** `row_data` only names the columns to change. Implemented via `spreadsheets.values:batchUpdate` with one `{range, values}` entry per changed column, rather than reading the row and writing it back — avoids a stale read clobbering an untouched column.

Row targeting is one of two mutually exclusive paths:
- `row_number` — direct; only the header row is fetched.
- `match_column` + `match_value` — searches the sheet; requires exactly one match. Zero matches raises "no row found"; multiple matches raise an "ambiguous update target" error listing every matching row and suggesting `row_number` instead. (Deliberate: an unbounded mutation from a fuzzy match carries different risk than `search_rows`' read-only default of returning all matches.)

Providing both or neither raises an error before any API call.

`row_number` is a `string` input, not `number`. There's no sensible numeric default for "which row," and it must remain optional (`match_column`/`match_value` is the alternative path) — a `required: false` numeric input would hit the blank-input crash documented under `get_rows`.

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | — | Same as other actions. |
| `row_number` | string | conditionally | Must be strictly greater than `header_row`. Required unless `match_column`/`match_value` are set. |
| `match_column` | string | conditionally | Required together with `match_value`, as an alternative to `row_number`. |
| `match_value` | string | conditionally | Exact match only. |
| `row_data` | string | yes | JSON object keyed by header name — only these columns change. Same behavior as `append_row`'s `row_data`. |

| Output | Type | Notes |
| --- | --- | --- |
| `row_number` | number | The row that was updated. |
| `success` | boolean | Always `true` when reached — failures raise exceptions rather than returning `false`. |

Verified end-to-end for all three paths:
- Direct `row_number`: changed one column; a follow-up `get_rows` confirmed the other columns were untouched.
- Unique `match_column`/`match_value`: resolved to the correct row.
- Ambiguous match: correctly rejected, listing the exact conflicting row numbers.

**Create Spreadsheet/Tab (`create_spreadsheet`) was built and fully verified here** — bare create, `folder_id` placement, and `template_spreadsheet_id` template copying (including via `additional_service_urls`/`full_domain`, see Auth Method) all worked end-to-end — but the action has since been removed from the v1 set, along with its Drive scope dependency. See git history on this branch (`fb1dfe0`, `4b14bbc`, `dcdf781`, `1784531`) for the implementation if this is revisited later.

---

## Known Constraints / Open Questions

**New Row Added trigger — investigated, cut from v1.**
- The Sheets API has no watch/push mechanism (Drive has a dedicated [push notifications guide](https://developers.google.com/workspace/drive/api/guides/push); the equivalent Sheets URL 404s).
- Drive's `files.watch` can technically target a spreadsheet's file ID, but would require re-adding the `drive` scope (see Auth Method — no longer requested) and delivers little over polling anyway: notifications are throttled to ~3 minutes minimum and carry no row/cell detail — still need a `get_rows`/`search_rows` follow-up and your own diff logic either way.
- No plugin in this workspace defines a trigger — Scheduled and Webhook triggers are native Kizen primitives. Drive's `watch_drive_changes` action registers Kizen's own Webhook trigger URL with Google; it isn't a trigger itself.
- Practical implication: polling requires no new plugin code. A native Scheduled trigger paired with `get_rows`/`search_rows` already supports "check periodically" — the remaining piece (diffing against last-seen state) belongs in the workflow.
- An Apps Script bridge is technically feasible (the [Apps Script API](https://developers.google.com/apps-script/api/how-tos/manage-projects) can create and deploy a script bound to a sheet) but requires a third Google API, a new OAuth scope (classification unconfirmed), a different runtime, and installable triggers that typically must be registered from within Apps Script itself. Large enough to warrant its own spike.

**`developer_business_id.staging`** was copied from `plugin-google-drive`'s `kizen.json`, assumed to be the shared staging test business. Confirmed correct for this repo.

**Reserved names apply to inputs, not just outputs.** `plugin-google-drive` hit this on an output (`files` → `matching_files`); this plugin hit it on an input (`name` → `spreadsheet_name`, on the now-removed `create_spreadsheet` action). No published list of reserved names exists — the shortest, most generic word is the first suspect.

**GCP project** is dedicated to this plugin, not shared with Drive/Calendar. The Drive API was enabled solely for the now-removed `create_spreadsheet` action; with `drive` no longer requested (see Auth Method), there's no current CASA obligation. The Apps Script API is not enabled, and stays off until a concrete need arises.

---

## SmartConnector CSV/XLSX Automatic Pull — Investigated, Not Built

Ticket question: *"Smartconnectors already support csv/xlsx manual input. Explore pulling from a connected Google Sheet automatically."*

SmartConnector is a core Kizen platform object (`/api/smart-connectors/{api_name}`), not part of the plugin SDK — confirmed by searching every plugin repo in this workspace. Its mechanics exist today only in Kizen's Workato and Zapier integrations:

1. `connector_type: spreadsheet` — a file is uploaded via a presigned-S3 flow (`GET /s3/presigned-post` → S3 → `POST /s3/success`, tagged `kind: "smart_connector_import"`) to obtain a Kizen file ID, which is then POSTed to `/api/smart-connectors/{api_name}/start-connector-flow` along with optional `sql_parameters`, `is_dry_run`, and `disable_diff_check`.
2. `connector_type: webhook` — skips the file; `POST /api/smart-connectors/{api_name}/webhook` with a JSON payload directly.

The missing piece — a generic "upload bytes, obtain a file ID, start a connector flow" bridge — is not Google-Sheets-specific, and no plugin implements it; Workato and Zapier each built it independently. Building it inside this plugin would be a third independent implementation of logic that likely belongs once, centrally, either as a Kizen-native step or a shared plugin capability. Documented rather than built, pending that decision.

`plugin-google-drive`'s `export_file` action can already export a Google Sheet to CSV (a Sheet is a Drive file), via `files.export` with `target_mime_type: "text/csv"` — so "get this sheet as CSV" already exists elsewhere. Note: Google's `files.export` has historically exported only the first tab of a spreadsheet, not all sheets — unconfirmed whether that still holds.

If built later, the shape would likely be: read the target sheet, upload the resulting bytes through Kizen's file-upload flow, then call `start-connector-flow` on the target SmartConnector's `api_name`.

---

## File Structure

```text
plugin-google-sheets/
├── kizen.json
├── CLAUDE.md
├── releaseNotes/
│   └── 1.0.0.md
└── src/
    └── automationSteps/
        ├── get_rows/
        │   ├── config.json
        │   └── script.py
        ├── search_rows/
        │   ├── config.json
        │   └── script.py
        ├── append_row/
        │   ├── config.json
        │   └── script.py
        └── update_row/
            ├── config.json
            └── script.py
```
