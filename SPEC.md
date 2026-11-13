# Athletics sidecar: specification

A small, stateless mobile web app that fronts a [Grist](https://www.getgrist.com/) document used to track athletics results. Grist is the only datastore and the desktop UI. This app is the quick phone view.

## Goal

Let a parent or coach, from a phone:

1. View personal bests (PBs) per athlete.
2. View, add and edit a meet.
3. Add results from inside a meet, so the meet is already chosen and never re-selected for each result.

## Principles

- **Stateless.** No database, no files, no sessions beyond what is needed to render a page. Deleting and recreating the container loses nothing.
- **Grist owns the logic.** Mark parsing, PB rules and defaults live in Grist formulas. The app reads computed columns (including the PB summary table) and writes raw inputs, so the phone and the desktop view can never disagree.
- **No built-in login.** The app is meant to run on a private network behind whatever access control the owner already uses. Do not build user accounts.
- **Secrets stay server-side.** The Grist API key is read from the environment and must never reach the browser, logs or rendered HTML.
- **Small and readable.** A few hundred lines. Prefer boring tools over frameworks.

## Architecture

- Grist (full or core edition, REST API available) holds all data.
- This app is a separate service. It calls Grist's REST API server-side.
- Suggested stack: Python (FastAPI or Flask) with server-rendered templates and HTMX, plus a web app manifest and icons so it installs to a phone home screen. Avoid a heavy single-page-app framework.

### Configuration (environment variables)

| Variable | Purpose | Secret |
|---|---|---|
| `GRIST_URL` | Base URL of the Grist server (no trailing slash) | No |
| `GRIST_DOC_ID` | Id of the Grist document | No |
| `GRIST_API_KEY` | API key for the Grist user the app acts as | Yes |
| `PORT` | Port the app listens on | No |
| `TZ` | Time zone used to work out today's date (default for a new meet) | No |

`GRIST_URL` must be overridable per environment (for example a public URL in development and an internal service URL in production). Nothing may be hardcoded. Ship a `.env.example` with placeholders only, and gitignore `.env`.

## Grist data model (the contract)

Table and column ids are case sensitive. At startup, read the schema from `GET /api/docs/{doc}/tables` and `.../columns`, and fail loudly with a clear message if anything below is missing.

### Athletes
- `Name` (Text)
- `Grade` (Choice, for example "Grade 9"): current grade. New results default to it.
- `Division` (Choice, label "Default division"): the athlete's usual division. New results default to it.

### Events
- `Name` (Text)
- `Kind` (Choice: `Time` or `Distance`). Time: lower is better. Distance: higher is better.
- Example seed: 60m, 80m, 100m, 200m, 400m, 800m, 1500m, 3000m, 80m Hurdles, 100m Hurdles, Long Jump, High Jump, Triple Jump, Shot Put, Discus, Javelin, Turbo Javelin, Vortex.
- The default event list is opt-in (`--seed-events` in the init script), not loaded by default.

### Meets
- `Name` (Text, unique, for example "Club night 12 Nov 2025")
- `Date` (Date)
- `Location` (Text)

### Implements (lookup table)
- `Event` (Ref to Events), `Grade` (Choice), `Division` (Choice), `Weight` (Text, for example "500g", "1.500kg").
- Maps an event, grade and division to the implement used for throws, matched exactly. Events with no implement (sprints, jumps) have no row.
- A division is a label the user sets (the init script creates the choices `Girls`, `Boys` and `Open`; the choices are deployment data). Rows are only found for the divisions they are entered for: there is no fallback from one division to another.

### Results (one row per result)
Writable by the app:
- `Athlete` (Ref to Athletes), `Event` (Ref to Events), `Meet` (Ref to Meets)
- `Mark` (Text, as typed: `2.98`, `13.45`, `2:15.30`)
- `Place` (Int, optional)
- `Division` (Choice), `Grade` (Choice) and `Implement` (Text): leave these out of the payload for normal entry so Grist fills them in. Send `Division` only to override the athlete's default division for one result.

Computed by Grist, read only (never write these):
- `Value` (Numeric): `Mark` parsed to a number (seconds for times, metres for distances). Errors if `Mark` cannot be parsed.
- `Date` (Date): formula `$Meet.Date`.
- `IsPB` (Bool): true if this row is the athlete's best for that Event and Implement.

### Trigger formulas that supply defaults
- `Results.Grade` = `$Athlete.Grade`, recalculated when `Athlete` is set. Changing an athlete's grade later does not alter old results.
- `Results.Division` = `$Athlete.Division`, recalculated when `Athlete` is set. It can be overridden per result.
- `Results.Implement` = lookup in `Implements` by Event, Grade and Division (exact match, blank when there is no row), recalculated when `Event` or `Division` is set.
- Limitation: `Implement` is triggered by `Event` and `Division` only. Send `Athlete` and `Event` in the same create call so all the defaults resolve. A later edit to Athlete or Grade does not recompute Implement.

### PB summary table
`Results_summary_Athlete_Event_Implement` has one row per Athlete, Event and Implement, with `count` and `Best` (Numeric). It is part of the contract, and the app reads personal bests from it: the app never calculates them. If it is missing or renamed, the startup check fails.

Grist keeps a summary table only while a view section uses it. The contract therefore also includes a page named `Personal Bests` (the desktop view of personal bests) that shows the summary table. Do not delete the page.

### Ref columns show names
`Results.Athlete`, `Results.Event`, `Results.Meet` and `Implements.Event` show the target's `Name`, not the row id (Grist `visibleCol`), and so do the summary table's Athlete and Event columns.

### Reference formulas (to recreate the schema in a fresh document)

`Results.Value` (regular formula, Numeric):
```python
s = str($Mark).strip().lower().rstrip("ms").replace(",", ".")
if not s:
  return None
if ":" in s:
  m, sec = s.split(":")
  return int(m) * 60 + float(sec)
return float(s)
```

`Results.IsPB` (regular formula, Bool):
```python
if $Value is None or not $Event:
  return False
vals = [r.Value for r in Results.lookupRecords(Athlete=$Athlete, Event=$Event, Implement=$Implement) if r.Value is not None]
best = min(vals) if $Event.Kind == "Time" else max(vals)
return $Value == best
```

`Results.Date` (regular formula): `$Meet.Date`

`Results.Grade` (trigger formula, recalculate when `Athlete` changes): `$Athlete.Grade`

`Results.Division` (trigger formula, recalculate when `Athlete` changes): `$Athlete.Division`

`Results.Implement` (trigger formula, recalculate when `Event` or `Division` changes):
```python
g = $Grade or $Athlete.Grade
d = $Division or $Athlete.Division
match = Implements.lookupOne(Event=$Event, Grade=g, Division=d)
return match.Weight if match else ""
```
(Do not name a variable `rec`; Grist reserves it.)

`Results_summary_Athlete_Event_Implement.Best` (summary table, grouped by Athlete, Event, Implement):
```python
vals = [r.Value for r in $group if r.Value is not None]
if not vals:
  return None
return min(vals) if $Event.Kind == "Time" else max(vals)
```

## Grist API notes

- Auth header: `Authorization: Bearer <GRIST_API_KEY>`.
- Records: `GET /api/docs/{doc}/tables/{table}/records` (optional `filter` JSON and `sort`), `POST .../records` with `{"records":[{"fields":{...}}]}`, `PATCH .../records` with `{"records":[{"id":N,"fields":{...}}]}`, and `POST .../data/delete` with a JSON array of ids.
- SQL (SELECT only): `POST /api/docs/{doc}/sql` with `{"sql":"...","args":[...]}`.
- Ref columns are numeric row ids in both directions. Resolve names to ids by loading Athletes, Events and Meets per request, or cache briefly in memory with a short TTL (data is edited directly in Grist too).
- Date columns are Unix seconds at UTC midnight (for example 1 Nov 2026 is `1793491200`). Convert with no time zone applied, so the date entered is the date stored and shown; the configured time zone is only used to work out today's date.
- Setting up a column: `recalcDeps` of a trigger column is sent as `["L", colRef, ...]` in an update after the column is created, not in the create call. `visibleCol` needs a display column too: set it, then apply `SetDisplayFormula` (for example `$Athlete.Name`). Both were checked against a running Grist.
- Verify behaviour against the running Grist version rather than assuming newer endpoints.

## Screens (mobile first, thumb friendly)

1. **Home / PBs**
   - Athlete switcher (chips) at the top.
   - PBs grouped as Sprints, Jumps and Throws (use `Events.Kind` plus event names), each showing event, best mark, implement if any, and the date and meet of the PB result.
   - A small "new PB" badge for results where `IsPB` is true and the meet is the most recent one.
2. **Meets**
   - Newest first, showing name, date, location and result count.
   - "New meet": name, date (native date input), location. Prefill location with the last one used.
3. **Meet detail**
   - Header with name, date and location, plus edit.
   - Results grouped by athlete, with PB badges and place.
   - A persistent "Add result" bar. Flow: athlete chip (remember the last athlete), event picker (grouped, recent events first), mark field with `inputmode="decimal"` (with an "m:ss.ss" helper for times), optional place. Save returns to the same screen with athlete and meet kept, ready for the next result. The meet is never asked for.
   - Tap a result to edit or delete (confirm delete).
4. **Edit result:** Athlete, Event, Mark, Place. Meet shown but changeable.

Out of scope for v1: charts, offline queue, multi-user accounts, editing Athletes, Events or Implements (done in Grist).

## Validation and errors

- Validate `Mark` before sending. Accept a number such as `2.98` or `13.45`, or `m:ss.ss` such as `2:15.30`. Strip a trailing `m` or `s`. Reject anything else with a clear inline message, because the Grist `Value` formula throws on junk.
- A mark with more than two decimals should warn but is allowed.
- Show Grist API errors as readable messages, never as raw tracebacks, and never include headers or keys.

## Environments and document initialisation

Run the app against a separate Grist document per environment. Development and tests use a TEST document with invented data. Production uses its own document. The document id is just `GRIST_DOC_ID`, so switching environments is a configuration change with no code change.

### Init script (a deliverable)

The repo must include a script, with documentation in the README, that sets up a document so the schema contract above is satisfied. It is the executable form of this spec, and it is how anyone installs the app on a fresh Grist document.

Requirements:
- Uses Grist's REST API only, with the same `GRIST_URL` and `GRIST_API_KEY` settings as the app.
- Can create a new empty document in a given workspace (for example `--create --workspace <id-or-name> --name "..."`), or set up an existing empty document given by `GRIST_DOC_ID`.
- Creates the tables, columns, Choice lists, Ref links, the computed and trigger formulas from "Reference formulas", column labels where the contract names one (`Athletes.Division`: "Default division"; the check matches on column id only), and the PB summary table with its `Best` column. The summary table is created through a view section on the `Personal Bests` page, only if that page is missing; an existing page is never changed. Page names match ignoring case and surrounding spaces, so an existing `Personal bests` page counts (here, in `--check` and in the startup check). It sets `visibleCol` on the Ref columns listed under "Ref columns show names" only where it is unset, and creates no other pages or forms (the other pages are Grist's own raw-data views).
- Optionally seeds the Events table (`--seed-events`, the default event list) and the Implements table (`--seed-implements`, sample weights by grade and division). Both are opt-in, so a user can end up with empty tables. Each seed adds only the events it needs: `--seed-implements` the events its rows use (Discus and Shot Put), `--demo-data` the events its results and those implements use. Existing rows are never changed or duplicated.
- Optional `--demo-data`: loads a few fictional athletes, meets and results so the app has something to show. It is a plan step like the others and also seeds the Implements and the events it needs. The demo data must be obviously fake. It only loads into a document whose Athletes, Meets and Results tables are all empty (or one the script has just created), and otherwise refuses. This protects real data without restricting what the document is called.
- Makes no assumptions about the environment: it works on a document with any name, against any Grist server. `--create` defaults to the name "BestYet" and accepts any `--name`. Nothing in the script or the app may require a particular name or marker, so anyone can set up a fresh production document with it.
- Idempotent: running it again on an already initialised document makes no changes and says so. It only adds what is missing and never drops, renames or overwrites anything.
- Optional `--check`: validates an existing document against the contract and reports every difference. The app reuses the same check at startup.
- Refuses to modify a document that already has unrelated tables unless the owner passes an explicit flag. The one exception is Grist's empty default table (`Table1`, columns A, B, C) in a document the script has just created: it is removed only if it is the only table, has only the default columns and has no rows, and otherwise left with a warning.
- Prints the whole plan before changing anything and asks for confirmation (`--yes` skips the prompt; `--dry-run` only prints the plan). Without a terminal and without `--yes` or `--dry-run` it refuses. Suitable as a one-off container command: configuration from environment variables only, output on stdout/stderr, nothing written to disk.
- Prints what it did, never prints the API key, and exits non-zero on failure.

The script is exercised by its own tests: initialise a fresh TEST document, then run the contract check against it, then run the app's integration tests against it.

## Testing

- Unit tests for mark validation and Ref resolution.
- An integration test script that creates a throwaway meet and a throwaway result, checks that `Value`, `Grade`, `Division`, `Implement` and `IsPB` come back as expected, then deletes exactly what it created. Never leave test rows behind and never touch other rows.
- Safety guard (test suite only): any test that writes, deletes or loads demo data must first read the target document's name and refuse to run unless the name contains `TEST`. Integration tests run against the TEST document only, and are skipped by default in CI. This guard lives in the test fixtures and contributor tooling. The app and the init script must not depend on it, so they stay usable on any document, including a production one. Document the guard in CONTRIBUTING, not in the user-facing setup instructions.

## Packaging

- A small, non-root image with a healthcheck, writing nothing to disk.
- Public defaults must be sensible for any user, not tied to one setup. The image, service and container are all named `bestyet`. `compose.example.yml` uses placeholder values (`GRIST_URL`, `GRIST_DOC_ID`, a secret `GRIST_API_KEY` from `.env`), a default `PORT` of 8585, no host-specific labels, no fixed `user:` unless the image needs one, and no volumes. Reverse-proxy and platform labels are optional extras shown as commented examples. Container naming conventions, hostnames and platform labels belong only in a private, untracked deployment file.
- A README covering local development, environment variables, how to test from a phone, and deployment.

## Possible later work

- A progress sparkline per event (data: Results filtered by athlete, event and implement, ordered by `Date`).
- An offline queue for entry with no signal.
