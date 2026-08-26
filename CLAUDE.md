# Google Sheets Plugin — Developer Context

Reads, searches, and writes rows in Google Sheets from Kizen agentic workflows via a single business-level OAuth connection. Every action is a plain data-fetch/write primitive against the Sheets API — no summarization or extraction, which belongs in existing LLM action steps.

---

## What This Plugin Does (v1)

1. **Get Rows / Read Range** — fetches rows keyed by header name, with optional single-column filtering.
2. **Search Rows** — finds rows by exact column value; returns matches and their real sheet row numbers.
3. **Append Row** — adds a row via a single JSON object keyed by header name (no dynamic per-column inputs — see below).
4. **Update Row/Cell** — updates specific cells within one row, targeted by row number or column match, without touching the rest of the row.

No change-notification trigger is implemented — see Known Constraints.

A fifth action, Create Spreadsheet/Tab, was built and verified (bare create, `folder_id` placement, and `template_spreadsheet_id` template copying) but was removed from the v1 action set along with its Drive scope dependency. All four remaining actions are Sheets-only — see Auth Method for the resulting scope.

---

## Auth Method

### OAuth 2.0 — business-level

- A single shared Google account (`auth_level: business`, service `shared` in `kizen.json`) — not a per-user connection.
- **Why business-level:** every action runs as a Kizen Code Step, which executes as a fixed service account and cannot complete an interactive OAuth consent flow. A `user`-level service returns a 503 (`"User must authorize this service"`) regardless of authorization attempts.
- Trade-off: all actions act as one shared identity. Access to a given spreadsheet depends on that shared account having access to it.

**Caller-identity restriction — `scope: "service-account-only"`.** Not to be confused with the Google OAuth `scopes` string below — this is a separate `kizen.json` service-level field, enforced by Kizen's proxy on every request. Without it, the `shared` service's credentials could be called from any interactive JS surface (e.g. the frontend), not just this plugin's own packaged Code Steps. `service-account-only` locks it down so only this plugin's own automation steps can call it; a generic code step or any browser-side caller gets a 403. (Documented at `kizen.github.io/app-engine/06-auth-secrets-services.html#field-scope-caller-identity-restriction` — not part of the public plugin-developer docs, and no other plugin in the workspace uses it yet.)

**Scopes:**

| Scope | Classification | Purpose |
| --- | --- | --- |
| `spreadsheets` | Sensitive, not Restricted | Read/write for all four actions. |
| `userinfo.email`, `userinfo.profile` | — | "Connected as {email}" in the setup assistant. |

`spreadsheets`/`spreadsheets.readonly` are Sensitive but not Restricted — no annual CASA assessment required. Confirmed against [Google's Sheets API scopes docs](https://developers.google.com/workspace/sheets/api/scopes).

Any scope change requires reconnecting the OAuth connection — an existing token doesn't retroactively gain a new scope — and adding the scope to the GCP OAuth consent screen's own scope list; declaring it in `kizen.json` alone is not sufficient.

**Historical note — Drive scope, removed.** The removed Create Spreadsheet/Tab action needed Google's full `drive` scope — Restricted, requiring a CASA security assessment before production use — because `template_spreadsheet_id` could point at a spreadsheet this app never created, a case `drive.file` can't reach. That work also surfaced a reusable platform mechanism: Kizen's proxy normally pins one host per `service_name`, so reaching both `sheets.googleapis.com` and `www.googleapis.com` (Drive) from one service required a second service definition — until `additional_service_urls` plus a per-request `full_domain` query param was confirmed as a working alternative, letting one service reach both hosts through one OAuth connection. With Create Spreadsheet/Tab gone, `drive` and `additional_service_urls` are no longer requested or declared — see `kizen.json`. If Drive-backed functionality returns to this plugin, this is the mechanism to use.

**Scope plan by action:**

| Action | Scope | Notes |
| --- | --- | --- |
| Get Rows, Search Rows, Append Row, Update Row/Cell | `spreadsheets` | The only scope this plugin requests. |
| New Row Added trigger | None — no Sheets push mechanism | Not implemented; see Known Constraints. |

**Proxy URL pattern:**

```text
/external-integrations/proxy/{plugin_api_name}/{service_name}/{api_path}
```

`{plugin_api_name}` is `google_sheets`. (While a feature branch's PR is open, Kizen instead deploys under `{api_name}_preview_{branch_slug}` — check the PR's `plugin-wizard` bot comment for that name, and revert any hardcoded preview path back to `google_sheets` before merging.)

The Sheets API requires `base_service_url: https://sheets.googleapis.com`, not the generic `www.googleapis.com/sheets/v4/...` path Drive uses for its own API — that combination returns Google's generic branded 404 page, not a Sheets API error.

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
| `filter_column` | string | no | Header name to filter on. Required together with `filter_value` — setting either one without the other raises an error. |
| `filter_value` | string | no | Exact match only. |

| Output | Type | Notes |
| --- | --- | --- |
| `rows` | string | JSON array of row objects keyed by header name (no native array `data_type` in this framework). |
| `row_count` | number | |

Fetches the whole sheet via `values.get`, treats `header_row`/`header_column` as the top-left of the real table, and keys each subsequent row against the header names found there. Rows above `header_row` and columns left of `header_column` are dropped, not returned as data. Out-of-range values raise a clear error rather than an `IndexError`.

Implementation notes:
- `base_service_url` must be `https://sheets.googleapis.com`; the generic `www.googleapis.com/sheets/v4/...` path returns a branded HTML 404, not an API error.
- Error handling checks the wrapped `status_code` in the response body, not just `resp.ok` — the proxy can return HTTP 200 even when the upstream call failed, so an unhandled non-JSON body would otherwise crash with an opaque `AttributeError`.
- `values.get` without `valueRenderOption` returns `FORMATTED_VALUE` (display strings, e.g. `"11/19/1990"`), not raw values.

Publishing is required before a plugin can be installed for testing; while a PR is open, the OAuth consent screen needs both the test Google account added under **Test users** and the exact scopes added to its own scope list — declaring them in `kizen.json` is not sufficient.

**Framework limitation — optional numeric inputs.** An input with `data_type: "number"` and `required: false` crashes the entire run (`KizenConversionError: Failed to convert value '' to float`) before the script executes, whenever the field is left blank — the runtime unconditionally calls `float()` on the raw value regardless of whether the input is required. `string` inputs don't have this problem. Adding a `"default"` while keeping `required: false` does not help — `default` only pre-fills the UI when `required: true`.

`header_row`/`header_column` use the pattern also used by `update_row`'s `row_number`: `data_type: "string"`, `required: false`, no platform `default` — the script's `resolve_positive_int` helper treats a blank/omitted value as `1` and raises a clear error on a non-numeric string, instead of the raw `KizenConversionError`. This is the preferred pattern for any future numeric-like input on this plugin. (The alternative — `required: true` with a platform `default` — avoids the crash too, but forces every caller to always see a prefilled value rather than a genuinely optional field; use it only where genuine optionality isn't needed.)

### search_rows

File: `src/automationSteps/search_rows/script.py`

Same header-resolution mechanics as `get_rows`, but always filters (`column_name`/`match_value`, both required, exact match) and adds `return_all_matches` (boolean, `required: true` with `default: true`).

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

No matches returns empty arrays for both outputs rather than an error. The runtime coerces the literal string `"false"` to Python `False` correctly rather than truthy-casting any non-empty string; leaving `return_all_matches` blank does not crash (unlike a blank numeric input), but the resulting behavior shouldn't be relied on — set it explicitly.

**Testing note:** setting a React-controlled input's `.value` directly via injected JS does not register with the dev toolkit's form state — the run submits the stale value. Use a real form-fill action or keystroke, not direct DOM assignment.

### append_row

File: `src/automationSteps/append_row/script.py`

**Not one input per mapped column.** `config.json` is static and can't know a spreadsheet's headers in advance, and this framework has no mechanism for dynamically-generated per-column inputs. `row_data` is a JSON object string keyed by header name instead.

Requires the `spreadsheets` write scope (see Auth Method).

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | — | Same as `get_rows`/`search_rows`. |
| `row_data` | string | yes | JSON object keyed by header name, e.g. `{"Name": "Jane"}`. Unknown keys raise an error; missing headers default to blank. |

| Output | Type | Notes |
| --- | --- | --- |
| `row_number` | number | Parsed from Google's `updates.updatedRange` response (e.g. `"Sheet1!A4:D4"` → `4`), not assumed from a local row count. |
| `spreadsheet_id` | string | Echoes the input. |

`row_data` behavior:
- Missing headers default to a blank cell.
- Unknown keys raise a clear error rather than being silently dropped or misplaced.
- `header_column` beyond the header row's actual width also raises, rather than silently producing an empty header list.
- Key order in the JSON object doesn't matter — values are reordered to match the sheet's actual header order.
- Written with `valueInputOption=USER_ENTERED`, so values are interpreted the same way as typed input (e.g. `"3/4/1995"` becomes a real date).

Single-row only — batch appends require multiple calls.

Column position (`header_column` + offset) is converted to an A1 column letter via a standalone `column_number_to_letter` helper (`26`→`Z`, `52`→`AZ`, `702`→`ZZ`, `703`→`AAA`).

### update_row

File: `src/automationSteps/update_row/script.py`

**A partial update, not a full-row overwrite.** `row_data` only names the columns to change. Implemented via `spreadsheets.values:batchUpdate` with one `{range, values}` entry per changed column, rather than reading the row and writing it back — avoids a stale read clobbering an untouched column.

Row targeting is one of two mutually exclusive paths:
- `row_number` — direct; only the header row is fetched.
- `match_column` + `match_value` — searches the sheet; requires exactly one match. Zero matches raises "no row found"; multiple matches raise an "ambiguous update target" error listing every matching row and suggesting `row_number` instead. (Deliberate: an unbounded mutation from a fuzzy match carries different risk than `search_rows`' read-only default of returning all matches.)

Providing both or neither raises an error before any API call.

`row_number` is a `string` input, not `number` — there's no sensible numeric default for "which row," and it must remain optional (`match_column`/`match_value` is the alternative path), so a `required: false` numeric input would hit the blank-input crash documented under `get_rows`.

| Input | Type | Required | Notes |
| --- | --- | --- | --- |
| `spreadsheet_id`, `sheet_name`, `header_row`, `header_column` | — | — | Same as other actions. |
| `row_number` | string | conditionally | Must be strictly greater than `header_row`. Required unless `match_column`/`match_value` are set. |
| `match_column` | string | conditionally | Required together with `match_value`, as an alternative to `row_number`. |
| `match_value` | string | conditionally | Exact match only. |
| `row_data` | string | yes | JSON object keyed by header name — only these columns change. Same behavior as `append_row`'s `row_data`, including the `header_column` bounds check on the `row_number` path. |

| Output | Type | Notes |
| --- | --- | --- |
| `row_number` | number | The row that was updated. |
| `success` | boolean | Always `true` when reached — failures raise exceptions rather than returning `false`. |

A fifth action, Create Spreadsheet/Tab, was previously built here (bare create, `folder_id` placement, `template_spreadsheet_id` template copying) but has been removed from the v1 set along with its Drive scope dependency; see version control history if this is revisited later.

---

## Known Constraints / Open Questions

**New Row Added trigger — not implemented.**
- The Sheets API has no watch/push mechanism (Drive has a dedicated [push notifications guide](https://developers.google.com/workspace/drive/api/guides/push); the equivalent Sheets URL 404s).
- Drive's `files.watch` can technically target a spreadsheet's file ID, but would require re-adding the `drive` scope (see Auth Method — no longer requested) and delivers little over polling anyway: notifications are throttled to ~3 minutes minimum and carry no row/cell detail — still need a `get_rows`/`search_rows` follow-up and your own diff logic either way.
- Scheduled and Webhook triggers are native Kizen primitives, not something a plugin defines itself.
- Practical implication: polling requires no new plugin code. A native Scheduled trigger paired with `get_rows`/`search_rows` already supports "check periodically" — the remaining piece (diffing against last-seen state) belongs in the workflow.
- An Apps Script bridge is technically feasible (the [Apps Script API](https://developers.google.com/apps-script/api/how-tos/manage-projects) can create and deploy a script bound to a sheet) but requires a third Google API, a new OAuth scope (classification unconfirmed), a different runtime, and installable triggers that typically must be registered from within Apps Script itself — large enough to warrant its own investigation.

**Reserved names apply to inputs, not just outputs.** No published list of reserved names exists — the shortest, most generic word is the first suspect. (This plugin hit it on an input, `name` → `spreadsheet_name`, on the now-removed `create_spreadsheet` action.)

**GCP project** is dedicated to this plugin. The Drive API was enabled solely for the now-removed `create_spreadsheet` action; with `drive` no longer requested (see Auth Method), there's no current CASA obligation. The Apps Script API is not enabled, and stays off until a concrete need arises.

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
