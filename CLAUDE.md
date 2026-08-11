# Google Sheets Plugin — Developer Context

Reads, searches, and writes rows in Google Sheets from Kizen agentic workflows, using a single business-level OAuth connection. This is a spike (KZN-18007) — every action is a plain data-fetch/write primitive against the Sheets API, deliberately with no summarization/extraction (that belongs in existing LLM action steps, not here).

---

## What This Plugin Does (v1 target)

Per the spike ticket, the full v1 surface is: Get Rows / Read Range, Search Rows, Append Row, Update Row/Cell, Create Spreadsheet/Tab, and (if feasible) a New Row Added trigger. Built incrementally, one action at a time.

1. **Get Rows / Read Range** — fetches rows from a sheet, keyed by header name, with optional single-column filtering. **Built.**
2. **Search Rows** — finds row(s) by exact column value, returning both the matching rows and their real sheet row numbers (for a subsequent Update Row). **Built.**
3. **Append Row** — adds a new row to the end of a sheet, keyed by header name via a single JSON-object input (no dynamic per-column inputs — see below). **Built.**

Everything else is not yet started.

---

## Auth Method

### OAuth 2.0 — business-level

- One business admin connects a single, shared Google account (`auth_level: business` in [kizen.json](kizen.json), service name `shared`) — same pattern as [plugin-google-drive](../plugin-google-drive/kizen.json), not a per-user connection like Calendar/Outlook Calendar.
- **Why business, not user-level, decided up front rather than discovered the hard way this time:** every action here is planned as a Code Step (Python automation-step). Code Steps execute as a fixed Kizen service account and can never complete an interactive OAuth consent screen — a `user`-level service 503s with `"User must authorize this service"` regardless of how many times a real human re-authorizes. `plugin-google-drive`'s `CLAUDE.md` documents hitting this live and having to migrate from `user` to `business` mid-build. Since every proposed Sheets action (Get Rows, Search Rows, Append Row, Update Row, Create Spreadsheet) is the same Code Step shape, this plugin starts at `business` from the outset.
- Trade-off, same as Drive: all actions act as one shared identity. Reading/writing a specific spreadsheet only works if that shared identity actually has access to it — there's no "acts as whichever staff member triggered the workflow."

**OAuth scopes requested (current):**

| Scope | Classification | Purpose |
| --- | --- | --- |
| `spreadsheets` | Sensitive (not Restricted) | Read + write access for Get Rows / Search Rows / Append Row. Upgraded from `spreadsheets.readonly` when Append Row needed write access — see below. |
| `userinfo.email`, `userinfo.profile` | — | Show "connected as {email}" in the setup assistant. |

**Scope upgraded for Append Row, `spreadsheets.readonly` → `spreadsheets`, in `kizen.json`.** Per the scope plan below, this was expected — but **every existing OAuth connection must be reconnected** before Append Row (or anything using the new scope) will actually work; an existing token issued under `.readonly` doesn't retroactively gain write access just because `kizen.json` changed. Also needs the new scope added to the GCP OAuth consent screen's own scope list, same as the original setup — declaring it in `kizen.json` alone isn't enough (see `get_rows`'s section below for that lesson the first time around).

**Scope plan for later actions** (document before building, since each scope change requires a fresh reconnect — an existing token doesn't retroactively gain a new scope):

| Action | Scope needed | Notes |
| --- | --- | --- |
| Get Rows, Search Rows, Append Row | `spreadsheets` | Current. |
| Update Row/Cell | `spreadsheets` | Already covered by the Append Row upgrade — no further scope change needed. |
| Create Spreadsheet/Tab — bare create + header row | `spreadsheets` | `spreadsheets.create` covers this; no Drive scope needed. |
| Create Spreadsheet/Tab — **from template** (`template_spreadsheet_id`) | Likely needs a Drive scope (`files.copy` on a file this app didn't create) | This is exactly the situation Drive's `copy_file` hit: `drive.readonly`/`drive.file` were insufficient, only full `drive` (Restricted, CASA) worked. Confirm this assumption with a feasibility test before committing to the template feature — don't assume `drive.file` is enough just because it's the "recommended" scope in Google's docs. |
| New Row Added trigger | none (no Sheets push-webhook — see Known Constraints) | Cut from v1 unless the polling/App Script bridge approach below pans out. |

`spreadsheets`/`spreadsheets.readonly` are both Google **Sensitive** scopes (real app verification required) but **not Restricted** — unlike Drive's `drive`/`drive.readonly`, they don't require the annual CASA security assessment. Confirmed against Google's current [Sheets API scopes docs](https://developers.google.com/workspace/sheets/api/scopes). Meaningfully lighter compliance lift than the Drive plugin faced, as long as this plugin never needs a Drive scope.

**Proxy URL pattern** (same convention as every other plugin in this workspace):

```text
/external-integrations/proxy/{plugin_api_name}/shared/{sheets_api_path}
```

`{plugin_api_name}` is `google_sheets` only once this plugin is merged and published. While testing an unmerged PR, Kizen deploys it under `{api_name}_preview_{branch_name_slugified}` instead — using the plain name 404s every proxy call. This cost Drive a full debugging session; check the PR's `plugin-wizard` bot comment ("App Preview Deployment Report") for the current preview name before testing.

The proxy appends the path after `shared` to `base_service_url`. **Unlike Drive (`https://www.googleapis.com` + `drive/v3/...`), Sheets must use `base_service_url: "https://sheets.googleapis.com"` + `v4/spreadsheets/{id}/values/{range}`** — the Sheets API isn't reachable under the generic `www.googleapis.com/sheets/v4/...` path the way Drive's API is under `www.googleapis.com/drive/v3/...`; that combination returns Google's generic branded HTML 404, not a Sheets API error. Confirmed directly with `curl` and by a live failed run — see the `get_rows` section below for the full story.

---

## kizen.api

Same runtime-injected HTTP client used by every other plugin in this workspace:

```python
kizen.api.get(url, params=None, headers=None)
kizen.api.post(url, data=None, json=None, headers=None)
kizen.api.patch(url, data=None, json=None, headers=None)
kizen.api.put(url, data=None, json=None, headers=None)
kizen.api.delete(url, headers=None)
```

Inputs/outputs are runtime-injected globals, not imports: read `inputs.<script_alias>`, write `outputs.<script_alias> = ...`. There's no `return`. **Optional inputs must be read with `getattr(inputs, "name", None)`** — an unwired optional input raises `AttributeError` on direct attribute access rather than evaluating to `None` (confirmed live on Drive's `search_files`).

Kizen's proxy wraps every successful upstream JSON response in an envelope — `{"status_code", "response_headers", "body": <actual upstream response>}` — every script must read through `resp.json()["body"]`, not `resp.json()` directly (Drive lost real debugging time to this).

---

## Agentic Workflow Steps

### `get_rows`

**File:** [src/automationSteps/get_rows/script.py](src/automationSteps/get_rows/script.py)

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id` | string | yes | From the sheet's URL. |
| `sheet_name` | string | yes | The tab name, e.g. `Sheet1`. Quoted in A1 notation unconditionally (handles spaces/special characters without guessing whether quoting is needed). |
| `header_row` | number | yes | 1-indexed row containing headers. `required: true` with `default: 1` — **not optional**, because this framework's runtime coerces every input to its declared `data_type` unconditionally, even when unset; an optional `number` input left blank arrives as `''` and crashes on `float('')` *before the script ever runs* (confirmed live — see below). `string` inputs don't have this problem (`str('')` is fine), which is why `filter_column`/`filter_value` stay `required: false`. Rows above `header_row` are ignored entirely (not returned as data). |
| `header_column` | number | yes | 1-indexed column the headers start in. Same `required: true` + `default: 1` shape as `header_row`, for the same reason. Columns to the left are dropped entirely from both headers and every data row — e.g. a leading row-label/index column. |
| `filter_column` | string | no | Header name to filter on. |
| `filter_value` | string | no | Required if `filter_column` is set; exact-match only. |

| Output | Type | Notes |
| --- | --- | --- |
| `rows` | string | JSON array string of row objects keyed by header name — the "JSON array string" convention used everywhere in this workspace since the framework has no native list/array `data_type` (see Drive's `matching_files`). |
| `row_count` | number | |

**Resolves by header name, not raw A1 notation**, per the ticket's ask: fetches the whole sheet via `values.get` (no cell range, just the sheet name), treats `header_row`/`header_column` (both default `1`) as the top-left corner of the real table, and zips each row below/right of that corner against the header names found there. A row shorter than the header count (Sheets omits trailing empty cells) gets `""` for the missing trailing columns. Rows above `header_row` and columns left of `header_column` (e.g. a title/banner row, or a row-label column) are dropped entirely, never returned as data. If either is beyond the sheet's actual dimensions, raises a clear error rather than an `IndexError`.

**Confirmed working end-to-end** against a real staging test sheet (3 rows, `Name`/`Email`/`Status` headers) — correct header-keyed JSON and `row_count`. Two real bugs surfaced getting there, both fixed:

1. **`base_service_url` must be `https://sheets.googleapis.com`, not `https://www.googleapis.com`** — unlike Drive's `drive/v3`, the Sheets API is *not* reachable under the generic `www.googleapis.com/sheets/v4/...` path. Hitting that path/host combo returns Google's generic branded HTML 404 page (`Error 404 (Not Found)!!1`, robot.png), not a Sheets API JSON error — confirmed directly with `curl`. `kizen.json`'s `base_service_url` is now `https://sheets.googleapis.com` and `script.py`'s path drops the `sheets/` prefix (`v4/spreadsheets/{id}/values/{range}`).
2. **Error detection must check the wrapped upstream `status_code`, not just `resp.ok`.** Kizen's proxy returns its own HTTP 200 even when the upstream call itself failed (e.g. the 404 above) — `resp.ok` only reflects proxy-level success. The original error handling only checked `resp.ok`, so a non-JSON upstream error body (the HTML page above) crashed with an opaque `AttributeError: 'str' object has no attribute 'get'` instead of a clean message. Fixed by also checking `payload.get("status_code")` and guarding with `isinstance(body, dict)` before treating it as JSON.

Also confirmed live: publishing an app is required before it can be installed into a business for testing (`Install Plugin` fails with "App not published" otherwise), and while a PR is open the OAuth consent screen must have both the target Google account added to **Test users** and the exact scopes (`spreadsheets.readonly`, `userinfo.email`, `userinfo.profile`) added to the consent screen's own scope list — declaring them in `kizen.json` alone isn't enough.

**Framework gotcha found while adding `header_row`:** an optional (`required: false`) input of `data_type: "number"` crashes the whole run with `KizenConversionError: Failed to convert value '' to float` *before* `script.py` executes at all, whenever the field is left blank — the runtime's `kznvar_to_pyvar` unconditionally calls `float(v)` on the raw value regardless of whether the input is required. This doesn't affect `string` inputs (`str('')` succeeds). **Tried adding `"default": 1` while keeping `required: false`, hoping the platform would substitute the default for a blank field — same crash, byte-for-byte.** `default` only works as a UI pre-fill tied to `required: true`; it does not make the runtime substitute a value for a truly optional field left blank. The only working fix: make numeric optional-in-spirit inputs `required: true` with a `"default"` value instead (precedent: `plugin-mysql`'s `mysql_read.return_single_value`) — the platform pre-fills the field in the UI so it's never actually sent blank. Any future numeric input on this plugin (e.g. a page-size input) should follow this pattern, not `required: false`.

**Confirmed live:** `values.get` without `valueRenderOption` returns `FORMATTED_VALUE` (what a user sees in the sheet UI), not raw underlying values — a `Birth Date` column came back as `"11/19/1990"` (a display string), not a raw date serial number. Worth keeping in mind before Search Rows/Update Row build on the same default.

### `search_rows`

**File:** [src/automationSteps/search_rows/script.py](src/automationSteps/search_rows/script.py)

Same header-resolution mechanics as `get_rows` (whole-sheet fetch, `header_row`/`header_column` locate the real table, same `required: true` + `default: 1` shape and the same reasons — see above), but always filters (per the ticket's `column_name`/`match_value`, both required, exact-match only) and adds `return_all_matches` (boolean, `required: true` with `default: true` — same defaulted-input pattern as a precaution).

**Confirmed working end-to-end** against the same real staging test sheet: searching `column_name="Status"`, `match_value="Active"` correctly returned only the matching row, keyed by header name, with `row_numbers: [2]` (the real sheet row, not an array index).

**Boolean blank-field behavior differs from `number`:** leaving `return_all_matches` blank in the dev toolkit (an empty string, same as the `header_row` crash scenario) did *not* crash — the run succeeded. Unlike `float('')`, whatever the runtime's boolean coercion does with `''` doesn't raise.

**Both branches of `return_all_matches` confirmed live** against a test sheet with a real duplicate value (two `Name="Scott F"` rows, at sheet rows 2 and 5): `return_all_matches: true` correctly returned both (`row_numbers: [2, 5]`); `return_all_matches: false` correctly stopped at the first (`row_numbers: [2]`). Also confirms the runtime coerces the literal string `"false"` to Python `False` rather than naively truthy-casting a non-empty string — worth knowing, since that would have been a silent, hard-to-notice bug if it went the other way.

One test-methodology note for future live-testing in this dev toolkit: setting a React-controlled input's `.value` directly via `element.value = ...` in injected JS does **not** register with the toolkit's form state — the run still submits the old value. Use the browser tool's dedicated form-fill action (or a real click/keystroke) instead of raw DOM `.value` assignment, or a test can silently run against stale input values.

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | — | Same as `get_rows`. |
| `column_name` | string | yes | Header name to search — unlike `get_rows`' `filter_column`, this is always required; searching is this action's whole purpose. |
| `match_value` | string | yes | Exact-match only, same semantics as `get_rows`' `filter_value`. |
| `return_all_matches` | boolean | yes | `true` (default): every matching row. `false`: stops at the first match. |

| Output | Type | Notes |
| --- | --- | --- |
| `matching_rows` | string | JSON array string of matching row objects keyed by header name. Named per the ticket's own spec — also sidesteps Drive's `files` → `matching_files` reserved-name lesson by starting specific. |
| `row_numbers` | string | JSON array string of the matches' **real 1-indexed sheet row numbers** (not array indices) — e.g. `[4, 7]` means sheet rows 4 and 7. The ticket's own spec lists this as `number, is_list`, but per the "no native list/array `data_type`" convention (see `get_rows`' `rows`), it's a JSON-array-encoded string like every other list output in this workspace. Deliberately real sheet row numbers, not 0-indexed offsets into the result — the ticket's proposed **Update Row/Cell** action takes `row_number` "from a prior Search Rows," so this only works as a `get_rows`↔`search_rows`↔`update_row` handoff if it's the actual number you'd type into the sheet. |

### `append_row`

**File:** [src/automationSteps/append_row/script.py](src/automationSteps/append_row/script.py)

**Not a per-mapped-column input shape, despite the ticket's proposal.** The ticket describes Append Row's input as "one input per mapped column," but `config.json` is static and fixed at build time — it has no way to know a given spreadsheet's actual headers in advance, and nothing in this workspace supports dynamically-generated per-column inputs (checked `plugin-mysql`'s `mysql_write` for precedent on the same underlying problem — structured data whose shape isn't known until runtime — and it sidesteps the problem entirely with a raw `query` string, not dynamic fields). Instead, `row_data` is a single JSON object string keyed by header name, extending the same "JSON-encoded string" convention already used for every list output in this workspace to inputs as well.

Requires the `spreadsheets` write scope — see the Auth Method section above for the scope upgrade and required reconnect.

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | — | Same as `get_rows`/`search_rows`. |
| `row_data` | string | yes | JSON object string keyed by header name, e.g. `{"Name": "Jane"}`. A key that isn't a real header raises a clear error (headers not present in `row_data` default to blank) rather than silently dropping or misplacing data. |

| Output | Type | Notes |
| --- | --- | --- |
| `row_number` | number | The real 1-indexed sheet row the new data landed on — parsed from Google's `updates.updatedRange` response (e.g. `"Sheet1!A4:D4"` → `4`), not assumed from a local row count. Usable directly as `search_rows`' `row_numbers` output would be. |
| `spreadsheet_id` | string | Echoes the input, per the ticket's own spec — lets a workflow chain off this action's output alone without re-referencing the original input. |

**`row_data` behavior, worth knowing:**
- Missing headers default to a blank cell, not an error — `row_data.get(header, "")`.
- A key that isn't a real header raises a clear error rather than silently dropping or misplacing data (see above).
- Key order in the JSON object doesn't matter — values are always re-ordered to match the sheet's actual header order.
- Written with `valueInputOption=USER_ENTERED`, so a value like `"3/4/1995"` is interpreted the same way Sheets would interpret it if typed into a cell directly (e.g. becomes a real date), not stored as a literal string.
- In a real Kizen workflow, `row_data` would typically be built by an upstream step (e.g. a JSON-builder/Format Text action mapping CRM fields into this shape) rather than hand-written — the ticket's original "one input per mapped column" would have been the more natural authoring experience, but this JSON-object shape is the closest equivalent this framework supports.

**Only single-row appends** — `row_data` is one object, not an array of objects; batch-appending N rows means N calls to this action. Not a limitation the ticket asked to solve, just worth being explicit about.

**Column-letter math is real, not a placeholder:** `header_column` (a number) has to become an actual A1 column letter (`1` → `A`, `27` → `AA`, etc.) to build the append target range correctly when `header_column` isn't `1` — implemented as a small standalone conversion function (`column_number_to_letter`), verified against known values (`26` → `Z`, `52` → `AZ`, `702` → `ZZ`, `703` → `AAA`) before ever hitting the real API.

**Confirmed working** after the OAuth reconnect under the new `spreadsheets` write scope — the header-row-only fetch, the `values:append` call, and the `updatedRange` row-number parsing all held up against the real API.

---

## Known Constraints / Open Questions From the Spike

- **No Sheets push-notification webhook, unlike Drive's `files.watch`/Changes API.** The Sheets API has no equivalent subscribe-to-changes mechanism. Options to investigate for the "New Row Added" trigger, in rough order of how much custom infrastructure they need:
  - **Polling**: a Scheduled trigger (native Kizen Agentic Workflow trigger, no custom plugin capability needed — same primitive Drive's `watch_drive_changes` design leans on) calls Get Rows/Search Rows on a cadence and diffs against the last-seen row count/row IDs. Simplest, but cadence is a real trade-off (latency vs. quota/cost) and there's no server-side "what changed" signal — the plugin has to compute the diff itself, probably by persisting last-seen row count somewhere.
  - **Apps Script bridge**: an Apps Script bound to the target sheet, using an `onChange`/`onEdit` installable trigger, that calls out to a Kizen webhook URL on new rows. Real push-like latency, but requires the user to install a script *inside every sheet* they want watched — a much heavier setup burden than Drive's plugin-side-only `watch_drive_changes`, and it's Apps Script (JavaScript in the user's Google account), not something this plugin's Python Code Steps can deploy on the user's behalf. Would need its own feasibility spike.
  - Recommendation for v1: cut the trigger entirely (per the ticket's own suggested fallback) and revisit with a dedicated feasibility pass once the five core actions are built and there's a concrete workflow that needs it.
- **`developer_business_id.staging` copied from `plugin-google-drive`'s `kizen.json`** (same value) — assumed to be the shared staging test business used across these plugin spikes. Confirm this is actually correct for this repo before testing against staging.
- **Reserved output names**: Drive's `search_files` had to rename its `files` output to `matching_files` after a deploy-time `400 API Name is reserved` with no advance list of reserved names to check against. `get_rows`' `rows`/`row_count` ran successfully against real staging without hitting this, so it's evidently not universally reserved — but there's still no advance list, so if a future output name 400s, suspect the shortest/most generic name first (per Drive's note) and rename.
- **OAuth Client ID / GCP project**: a new, dedicated GCP project was created for this plugin (not shared with Drive/Calendar's projects) — mirrors the fact that Drive and Calendar already use distinct `client_id`s. Sheets API is the only API enabled on it for now; the Drive API is deliberately not enabled, to avoid any Restricted-scope exposure until/unless the "Create Spreadsheet from template" action actually needs it.

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
        └── append_row/
            ├── config.json
            └── script.py
```
