# BestYet

[![Python](https://img.shields.io/badge/Python-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-000000.svg?style=flat&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)

Results end up scattered across spreadsheets and apps, and a personal best is hard to find on a phone. **BestYet** is a small mobile web app that gives a quick phone view of an athletics results document in [Grist](https://www.getgrist.com/): see each athlete's personal bests, add a meet, and enter results from inside it.

Not affiliated with Grist Labs.

> **Early development.** BestYet is not usable yet. See [Status and Limitations](#status-and-limitations).

## Features

The goals for version 1 (see [Status and Limitations](#status-and-limitations) for what works today):

- Personal bests per athlete, grouped as sprints, jumps and throws
- Meets, newest first, with their results
- Add a result from inside a meet: the meet is already chosen, and the last athlete is remembered
- Mobile first: big tap targets and a numeric keypad for marks
- Grist holds all the data and the rules (PB logic, defaults), so the phone and the desktop view always agree
- No database of its own: BestYet is stateless

## Getting started

Set up a Grist document with the init script (`GRIST_URL` and `GRIST_API_KEY` set, see [Configuration](#configuration)):

```
bestyet-init --create --workspace "My Workspace" --name "BestYet"   # new document; prints its id
bestyet-init                                                         # existing document in GRIST_DOC_ID
```

It prints the whole plan, then asks `Apply these changes? [y/N]` before changing anything; `--yes` skips the prompt (required without a terminal), `--dry-run` only shows the plan. It only adds what is missing (tables, columns, the formulas and triggers, the personal-bests summary table on a `Personal bests` page, and names in Ref columns where unset), and a second run does nothing. With `--create` it also removes the empty `Table1` that Grist puts in every new document, but only if that table is untouched. A document with tables of its own is refused unless you pass `--allow-existing-tables`; this includes a blank document made in the Grist UI (it has an empty `Table1`), so use `--create` instead. It also adds the example events (matched by name; existing rows are never changed or duplicated). Pass `--seed-implements` to add the example implement rows too (matched by event and grade; both ignore case and spaces). The example implements are only examples: adjust them in Grist. Pass `--demo-data` to load a few fictional athletes, meets and results (plus the example events and implements they use); it refuses, writing nothing, unless Athletes, Meets and Results are all empty.

To validate a document without changing it, run `bestyet-init --check` (uses `GRIST_DOC_ID`). It lists every difference from the schema contract by name (missing tables, columns, the summary table or the `Personal bests` page) and exits non-zero on any. The app runs the same check at startup and refuses to start, with the same list, if the document does not match.

Once the project's container image exists (a later milestone), the same command can run as a one-off in it, with the settings passed as environment variables and nothing written to disk:

```
docker run --rm -e GRIST_URL -e GRIST_API_KEY <your-bestyet-image> bestyet-init --create --workspace Home --yes
```

Then run the app.

BestYet has no login: read [Privacy](#privacy) before you run it.

## Configuration

Set these as environment variables:

| Variable | Purpose | Default |
|---|---|---|
| `GRIST_URL` | Base URL of the Grist server | required |
| `GRIST_DOC_ID` | Id of the Grist document | required |
| `GRIST_API_KEY` | API key for the Grist user BestYet acts as | required |
| `PORT` | Port to listen on | `8585` |
| `TZ` | Time zone, used for "today" | `UTC` |

For development, copy `.env.example` to `.env`; real environment variables take precedence.

## Privacy

BestYet has no login: anyone who can reach it can read and change the results in the connected document. **Do not expose it to the internet.** Run it on a private network, behind your own access control. See [SECURITY.md](SECURITY.md).

- The Grist API key stays on the server. It is never sent to the browser or written to logs.
- BestYet stores nothing itself. Planned: remembering your last-used athlete in this browser's `localStorage`.

## Tech Stack

- Python and Flask
- Jinja templates (HTMX planned)
- httpx
- pytest and ruff

## Development

Assuming your environment is already configured:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
flask --app bestyet run --debug
ruff check . && ruff format --check . && pytest
```

Enable the git hooks once per clone with `git config core.hooksPath .githooks`. They refuse commits and merges on `main`: work on a branch and open a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Status and Limitations

BestYet is in early development and does not do anything useful yet. It works with Grist's REST API, so it is tied to the document layout described in [SPEC.md](SPEC.md).

## License

MIT
