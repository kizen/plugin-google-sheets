# Google Sheets Plugin — Developer Context

Reads, searches, and writes rows in Google Sheets from Kizen agentic workflows, using a single business-level OAuth connection. This is a spike (KZN-18007) — every action is a plain data-fetch/write primitive against the Sheets API, deliberately with no summarization/extraction (that belongs in existing LLM action steps, not here).

---

## What This Plugin Does (v1 target)

Per the spike ticket, the full v1 surface is: Get Rows / Read Range, Search Rows, Append Row, Update Row/Cell, Create Spreadsheet/Tab, and (if feasible) a New Row Added trigger. Built incrementally, one action at a time.

1. **Get Rows / Read Range** — fetches rows from a sheet, keyed by header name, with optional single-column filtering. **Built.**

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
| `spreadsheets.readonly` | Sensitive (not Restricted) | Read access for Get Rows / Search Rows. |
| `userinfo.email`, `userinfo.profile` | — | Show "connected as {email}" in the setup assistant. |

**Scope plan for later actions** (document before building, since each scope change requires a fresh reconnect — an existing token doesn't retroactively gain a new scope):

| Action | Scope needed | Notes |
| --- | --- | --- |
| Get Rows, Search Rows | `spreadsheets.readonly` | Current. |
| Append Row, Update Row/Cell | `spreadsheets` (drop `.readonly`) | `spreadsheets` is Sensitive, same tier as `.readonly` — no extra CASA cost to upgrade, unlike Drive's `drive.readonly` → `drive` jump (both Restricted). |
| Create Spreadsheet/Tab — bare create + header row | `spreadsheets` | `spreadsheets.create` covers this; no Drive scope needed. |
| Create Spreadsheet/Tab — **from template** (`template_spreadsheet_id`) | Likely needs a Drive scope (`files.copy` on a file this app didn't create) | This is exactly the situation Drive's `copy_file` hit: `drive.readonly`/`drive.file` were insufficient, only full `drive` (Restricted, CASA) worked. Confirm this assumption with a feasibility test before committing to the template feature — don't assume `drive.file` is enough just because it's the "recommended" scope in Google's docs. |
| New Row Added trigger | none (no Sheets push-webhook — see Known Constraints) | Cut from v1 unless the polling/App Script bridge approach below pans out. |

`spreadsheets`/`spreadsheets.readonly` are both Google **Sensitive** scopes (real app verification required) but **not Restricted** — unlike Drive's `drive`/`drive.readonly`, they don't require the annual CASA security assessment. Confirmed against Google's current [Sheets API scopes docs](https://developers.google.com/workspace/sheets/api/scopes). Meaningfully lighter compliance lift than the Drive plugin faced, as long as this plugin never needs a Drive scope.

**Proxy URL pattern** (same convention as every other plugin in this workspace):

```text
/external-integrations/proxy/{plugin_api_name}/shared/{sheets_api_path}
```

`{plugin_api_name}` is `google_sheets` only once this plugin is merged and published. While testing an unmerged PR, Kizen deploys it under `{api_name}_preview_{branch_name_slugified}` instead — using the plain name 404s every proxy call. This cost Drive a full debugging session; check the PR's `plugin-wizard` bot comment ("App Preview Deployment Report") for the current preview name before testing.

The proxy appends the path after `shared` to `base_service_url` (`https://www.googleapis.com`), so `sheets/v4/spreadsheets/{id}/values/{range}` resolves to `https://www.googleapis.com/sheets/v4/spreadsheets/{id}/values/{range}`.

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
| `filter_column` | string | no | Header name to filter on. |
| `filter_value` | string | no | Required if `filter_column` is set; exact-match only. |

| Output | Type | Notes |
| --- | --- | --- |
| `rows` | string | JSON array string of row objects keyed by header name — the "JSON array string" convention used everywhere in this workspace since the framework has no native list/array `data_type` (see Drive's `matching_files`). |
| `row_count` | number | |

**Resolves by header name, not raw A1 notation**, per the ticket's ask: fetches the whole sheet via `values.get` (no cell range, just the sheet name), treats row 1 as headers, and zips each subsequent row against those headers. A row shorter than the header count (Sheets omits trailing empty cells) gets `""` for the missing trailing columns.

**Not yet tested against a real staging sheet** — built and internally consistent with the Drive plugin's proven patterns (proxy envelope unwrapping, error shape, `getattr` for optional inputs), but no live run yet. Next step before calling this action done.

**Untested assumption:** `values.get` without `valueRenderOption` returns `FORMATTED_VALUE` (what a user sees in the sheet UI — e.g. dates/currency formatted as strings), not raw underlying values. Worth confirming this is the desired default before Search Rows/Update Row build on the same assumption.

---

## Known Constraints / Open Questions From the Spike

- **No Sheets push-notification webhook, unlike Drive's `files.watch`/Changes API.** The Sheets API has no equivalent subscribe-to-changes mechanism. Options to investigate for the "New Row Added" trigger, in rough order of how much custom infrastructure they need:
  - **Polling**: a Scheduled trigger (native Kizen Agentic Workflow trigger, no custom plugin capability needed — same primitive Drive's `watch_drive_changes` design leans on) calls Get Rows/Search Rows on a cadence and diffs against the last-seen row count/row IDs. Simplest, but cadence is a real trade-off (latency vs. quota/cost) and there's no server-side "what changed" signal — the plugin has to compute the diff itself, probably by persisting last-seen row count somewhere.
  - **Apps Script bridge**: an Apps Script bound to the target sheet, using an `onChange`/`onEdit` installable trigger, that calls out to a Kizen webhook URL on new rows. Real push-like latency, but requires the user to install a script *inside every sheet* they want watched — a much heavier setup burden than Drive's plugin-side-only `watch_drive_changes`, and it's Apps Script (JavaScript in the user's Google account), not something this plugin's Python Code Steps can deploy on the user's behalf. Would need its own feasibility spike.
  - Recommendation for v1: cut the trigger entirely (per the ticket's own suggested fallback) and revisit with a dedicated feasibility pass once the five core actions are built and there's a concrete workflow that needs it.
- **`developer_business_id.staging` copied from `plugin-google-drive`'s `kizen.json`** (same value) — assumed to be the shared staging test business used across these plugin spikes. Confirm this is actually correct for this repo before testing against staging.
- **Reserved output names**: Drive's `search_files` had to rename its `files` output to `matching_files` after a deploy-time `400 API Name is reserved` with no advance list of reserved names to check against. `rows`/`row_count` haven't been deploy-tested yet — if either fails the same way, suspect the shortest/most generic name first (per Drive's note) and rename.
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
        └── get_rows/
            ├── config.json
            └── script.py
```
