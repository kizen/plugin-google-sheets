# Google Sheets Plugin — Developer Context

Reads, searches, and writes rows in Google Sheets from Kizen agentic workflows via a single business-level OAuth connection. This is spike KZN-18007. Every action is a plain data-fetch/write primitive against the Sheets API — no summarization or extraction, which belongs in existing LLM action steps.

---

## What This Plugin Does (v1)

Per the spike ticket: Get Rows / Read Range, Search Rows, Append Row, Update Row/Cell, Create Spreadsheet/Tab, and (if feasible) a New Row Added trigger.

1. **Get Rows / Read Range** — fetches rows keyed by header name, with optional single-column filtering. Built.
2. **Search Rows** — finds rows by exact column value; returns matches and their real sheet row numbers. Built.
3. **Append Row** — adds a row via a single JSON object keyed by header name (no dynamic per-column inputs — see below). Built.
4. **Update Row/Cell** — updates specific cells within one row, targeted by row number or column match, without touching the rest of the row. Built.
5. **Create Spreadsheet/Tab** — creates a new spreadsheet (blank or copied from an existing spreadsheet as a template), optionally with a header row and target Drive folder. Built.

No trigger was built — see Known Constraints.

---

## Auth Method

### OAuth 2.0 — business-level

- A single shared Google account (`auth_level: business`, service `shared` in `kizen.json`) — same pattern as `plugin-google-drive`, not a per-user connection like Calendar.
- **Why business-level:** every action runs as a Kizen Code Step, which executes as a fixed service account and cannot complete an interactive OAuth consent flow. A `user`-level service returns a 503 (`"User must authorize this service"`) regardless of authorization attempts — documented in `plugin-google-drive`'s CLAUDE.md after being discovered mid-build there. This plugin starts at `business` from the outset.
- Trade-off: all actions act as one shared identity. Access to a given spreadsheet depends on that shared account having access to it.

**Scopes:**

| Scope | Classification | Purpose |
| --- | --- | --- |
| `spreadsheets` | Sensitive, not Restricted | Read/write for all core actions. Upgraded from `spreadsheets.readonly` when Append Row required write access. |
| `drive` | **Restricted** | Lets Create Spreadsheet's `folder_id` move a newly created file, and `template_spreadsheet_id` copy an existing file this app didn't create. See below for why `drive.file` isn't sufficient. |
| `userinfo.email`, `userinfo.profile` | — | "Connected as {email}" in the setup assistant. |

`spreadsheets`/`spreadsheets.readonly` are Sensitive but not Restricted — no annual CASA assessment, unlike Drive's `drive`/`drive.readonly`. Confirmed against [Google's Sheets API scopes docs](https://developers.google.com/workspace/sheets/api/scopes).

Any scope change requires reconnecting the OAuth connection — an existing token doesn't retroactively gain a new scope — and adding the scope to the GCP OAuth consent screen's own scope list; declaring it in `kizen.json` alone is not sufficient.

**Why full `drive`, not `drive.file`.** `drive.file` only grants per-file access to files this app itself created or opened — fine for moving a spreadsheet the app just created via `folder_id`, but not for `template_spreadsheet_id`, which points at an arbitrary pre-existing spreadsheet the app has never touched. This is the exact situation `plugin-google-drive`'s `copy_file` action hit: `drive.readonly`/`drive.file` were insufficient there, only full `drive` worked. `drive` is Google-**Restricted**, which requires a CASA security assessment before production/general-availability use — not before testing with an explicit test-user allowlist, which is how this was built and verified. Moving this plugin to production requires that assessment; that's a deliberate, separate decision, not something resolved by this spike.

Kizen's proxy resolves the upstream host per `service_name`, fixed to that service's `base_service_url` — normally one host per service. This plugin originally worked around that by declaring a second service, `shared_drive`, with `base_service_url: https://www.googleapis.com`, so Drive calls had somewhere to go. That required two separate OAuth authorization steps in the setup assistant (one per service, confirmed live, despite both referencing the same underlying OAuth client and encrypted `client_secret`).

**`additional_service_urls` / `full_domain` — verified working.** The `shared_drive` service has since been removed. The `shared` service now declares `"additional_service_urls": ["www.googleapis.com"]`, a platform field that lets one service resolve to more than one upstream host, selected per-request via a `full_domain` query param on the proxy call (e.g. `...&full_domain=www.googleapis.com`) rather than a second service definition. All Drive calls in `create_spreadsheet` (`files.copy`, `files.update`) now go through `shared` with that query param appended, instead of a `shared_drive`-scoped URL. Confirmed end-to-end: both the `folder_id` move and the `template_spreadsheet_id` copy succeeded through the single service, and only one OAuth authorization was needed on re-publish (down from two). The companion `sub_domain_regex_validation` field is unused here — Sheets/Drive have no subdomain concept, and `www.googleapis.com` is matched as a full literal host, not a `subdomain.root` pair.

**Scope plan by action:**

| Action | Scope | Notes |
| --- | --- | --- |
| Get Rows, Search Rows, Append Row, Update Row/Cell | `spreadsheets` | Current. |
| Create Spreadsheet — bare create | `spreadsheets` | No Drive scope needed. |
| Create Spreadsheet — `folder_id` | `drive` via `shared` + `full_domain=www.googleapis.com` | Current. (`drive.file` would have been sufficient for this alone, but `template_spreadsheet_id` on the same action needs full `drive` anyway — see above.) |
| Create Spreadsheet — `template_spreadsheet_id` | `drive` via `shared` + `full_domain=www.googleapis.com` | Current. Restricted scope — see above for the production/CASA implication. |
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

**Demo action — `get_rows_number_input`.** A duplicate of `get_rows` (`src/automationSteps/get_rows_number_input/`) was kept unchanged with the original `data_type: "number"`, `required: true`, `default: 1` `header_row`/`header_column` inputs, to demo the design constraint side-by-side with the fix. That combination doesn't crash — `required: true` was exactly the workaround for the crash — but it means the field can never be genuinely left blank: the platform always shows a prefilled `1`, and switching it to `required: false` to make it truly optional is what triggers `KizenConversionError` on a blank value. `get_rows` now uses `header_row`/`header_column` as optional strings instead, which are genuinely blank-able with no crash risk either way. Not part of the core v1 action set; safe to delete after the demo.

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

### create_spreadsheet

File: `src/automationSteps/create_spreadsheet/script.py`

**Two mutually exclusive creation paths, branching on `template_spreadsheet_id`:**
- **Bare create** (no `template_spreadsheet_id`): `spreadsheets.create` via the `shared` service. `folder_id`, if set, needs a separate follow-up `files.update` (`addParents`/`removeParents=root`) — also via `shared`, using `full_domain=www.googleapis.com` to reach Drive, since the Sheets create call has no folder concept at all.
- **Template copy** (`template_spreadsheet_id` set): `files.copy` on the template, via `shared` + `full_domain=www.googleapis.com`, requiring full `drive` scope (Restricted) — see Auth Method for why `drive.file` isn't enough. `folder_id`, if set, is passed as `parents` directly in the same copy call, so no separate move step is needed on this path. Drive's file resource doesn't expose a spreadsheet's internal sheet list, so a second call — a Sheets API metadata fetch (`fields=sheets.properties.title`) via `shared` (default host) — is needed afterward to resolve the copied file's first tab name for the `sheet_name` output.
- Both Drive calls previously went through a second service, `shared_drive` — removed once `additional_service_urls`/`full_domain` was confirmed as a working replacement; see Auth Method.
- **Confirmed behavior:** on a template copy, if `folder_id` isn't set, the copy lands in the *template's own* current folder — Drive's `files.copy` defaults a new file's parent to the source file's parent when `parents` is omitted from the request body. Not a bug; just Drive's own default.

Either way, `header_row_values` (if set) is written the same way afterward, regardless of which path produced the spreadsheet.

No `header_row`/`header_column` inputs — there's no existing structure to resolve against for a bare create; headers always land at A1. (A template copy *does* inherit whatever structure the template already had — `header_row_values`, if set, still only touches row 1.)

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_name` | string | yes | Title for the new spreadsheet. Named `spreadsheet_name`, not `name` — `name` is a reserved API name (see Known Constraints). |
| `template_spreadsheet_id` | string | no | Spreadsheet ID to copy as a template. Requires `drive` (Restricted) — see Auth Method. |
| `header_row_values` | string | no | JSON array of header names, written into row 1. Uses `valueInputOption=RAW`, unlike `append_row`/`update_row`'s `USER_ENTERED` — headers should never be auto-formatted. On a template copy, this overwrites row 1 of whatever the template already had there. |
| `folder_id` | string | no | Drive folder ID to place the new spreadsheet into. |

| Output | Type | Notes |
| --- | --- | --- |
| `spreadsheet_id` | string | The new (or copied) spreadsheet's ID. |
| `sheet_name` | string | The first tab's name. For a bare create this is Google's own default (`Sheet1`); for a template copy, it's whatever the template's first tab was actually called — either way, parsed from the real response rather than assumed. |

**Bare-create path verified end-to-end**, all inputs: bare create; `folder_id` (visually confirmed placement in the target folder); `header_row_values` (confirmed via a raw read that the exact header values were written — a sheet with only a header row and no data rows correctly reports `row_count: 0` from `get_rows`, which isn't a bug, just headers never counting as a data row).

**Template-copy path confirmed working end-to-end**, including with a genuinely adversarial test: the template used was a spreadsheet created directly in the Google Sheets UI, never touched by this plugin's own API calls — exactly the case `drive.file` cannot reach, and full `drive` correctly could. A follow-up `get_rows` against the copy returned all 7 rows identical to the live template's current contents (not an empty shell), confirming `files.copy` genuinely duplicates data, and the metadata-lookup call correctly resolved the copied file's real sheet name.

**Re-verified after the `shared_drive` → `additional_service_urls` migration:** both `folder_id` (visually confirmed placement) and `template_spreadsheet_id` (copy succeeded, correctly defaulted to the template's own folder when `folder_id` wasn't set) still work with Drive calls routed through `shared` + `full_domain=www.googleapis.com` instead of the old `shared_drive` service.

---

## Known Constraints / Open Questions

**New Row Added trigger — investigated, cut from v1.**
- The Sheets API has no watch/push mechanism (Drive has a dedicated [push notifications guide](https://developers.google.com/workspace/drive/api/guides/push); the equivalent Sheets URL 404s).
- Drive's `files.watch` can technically target a spreadsheet's file ID (now feasible scope-wise, with `drive` in hand), but delivers little over polling: notifications are throttled to ~3 minutes minimum and carry no row/cell detail — still need a `get_rows`/`search_rows` follow-up and your own diff logic either way.
- No plugin in this workspace defines a trigger — Scheduled and Webhook triggers are native Kizen primitives. Drive's `watch_drive_changes` action registers Kizen's own Webhook trigger URL with Google; it isn't a trigger itself.
- Practical implication: polling requires no new plugin code. A native Scheduled trigger paired with `get_rows`/`search_rows` already supports "check periodically" — the remaining piece (diffing against last-seen state) belongs in the workflow.
- An Apps Script bridge is technically feasible (the [Apps Script API](https://developers.google.com/apps-script/api/how-tos/manage-projects) can create and deploy a script bound to a sheet) but requires a third Google API, a new OAuth scope (classification unconfirmed), a different runtime, and installable triggers that typically must be registered from within Apps Script itself. Large enough to warrant its own spike.

**`developer_business_id.staging`** was copied from `plugin-google-drive`'s `kizen.json`, assumed to be the shared staging test business. Confirmed correct for this repo.

**Reserved names apply to inputs, not just outputs.** `plugin-google-drive` hit this on an output (`files` → `matching_files`); this plugin hit it on an input (`name` → `spreadsheet_name`). No published list of reserved names exists — the shortest, most generic word is the first suspect.

**GCP project** is dedicated to this plugin, not shared with Drive/Calendar. The Drive API is enabled, and the `drive` scope (Restricted) is requested for `folder_id`/`template_spreadsheet_id`. The Apps Script API is not enabled, and stays off until a concrete need arises. Moving to production requires the CASA assessment that `drive` triggers — not done, and a deliberate separate decision (see Auth Method).

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
        ├── get_rows_number_input/  # demo-only, see Known Constraints
        │   ├── config.json
        │   └── script.py
        ├── search_rows/
        │   ├── config.json
        │   └── script.py
        ├── append_row/
        │   ├── config.json
        │   └── script.py
        ├── update_row/
        │   ├── config.json
        │   └── script.py
        └── create_spreadsheet/
            ├── config.json
            └── script.py
```
