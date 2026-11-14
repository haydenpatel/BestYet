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

1. **Settings.** Set `GRIST_URL` and `GRIST_API_KEY` (see [Configuration](#configuration)).
2. **Run `bestyet-init`** to set up the Grist document. It only adds what is missing, so running it again is safe.
   - **Guided:** run it with no options in a terminal. If `GRIST_DOC_ID` is set it first offers to set up that document; otherwise (or if you decline) it asks for the workspace, then which document or a new one. It shows the plan and asks before changing anything. After creating a document it prints the `GRIST_DOC_ID=...` line to add to your settings.
   - **With flags:** for scripts, containers and repeat runs. Without a terminal it never asks, so give every value as a flag. To set up an existing document this way, set `GRIST_DOC_ID` too.

| Flag | What it does | Required | Default |
| --- | --- | --- | --- |
| `--create` | Create a new document | No | off: use the document in `GRIST_DOC_ID` |
| `--workspace W` | Workspace id or name for the new document | With `--create` (asked for in a terminal) | none |
| `--org O` | Organisation id or name, to narrow `--workspace` | No | all organisations |
| `--name N` | Name of the new document; an existing name in the workspace is never duplicated | No | `BestYet` |
| `--seed-events` | Also add the default events (the standard track and field list). Without it the Events table stays empty (apart from the few events `--seed-implements` or `--demo-data` need) and you add events by hand in Grist | No | off |
| `--seed-implements` | Also add sample implement weights, by grade and division (edit them in Grist) | No | off |
| `--demo-data` | Also load fictional athletes, meets and results, with the events and sample implements they use (not the full default list); refused unless those tables are empty | No | off |
| `--allow-existing-tables` | Add to a document that has tables of its own (a blank one made in Grist has an empty `Table1`; `--create` avoids this) | No | off: refused |
| `-y`, `--yes` | Apply without asking | Without a terminal, unless `--dry-run` | off: asks |
| `--dry-run` | Show the plan and change nothing | No | off |
| `--check` | Read-only (uses `GRIST_DOC_ID`): list every difference from the schema contract, or say it matches; exits non-zero on any difference. The app runs the same check at startup | No | off |

A result's implement weight is looked up by event, grade and division, with an exact match; no match leaves it blank. A division is a label you set (the script creates `Girls`, `Boys` and `Open`): each athlete has a default division, and a result can override it. The sample implements are Discus and Shot Put weights for Grades 7-15, Girls and Boys. Adjust them to your own rules in Grist, and add any others by hand in the Implements table.

Once the project's container image exists (a later milestone), the same command can run as a one-off in it, with the settings passed as environment variables and nothing written to disk:

```
docker run --rm -e GRIST_URL -e GRIST_API_KEY <your-bestyet-image> bestyet-init --create --workspace Home --yes
```

Then run the app.

BestYet has no login: read [Privacy](#privacy) before you run it.

## Configuration

The settings the init script and the app read, as environment variables:

| Variable | Purpose | Required | Default |
|---|---|---|---|
| `GRIST_URL` | Base URL of the Grist server | Yes | |
| `GRIST_API_KEY` | API key for the Grist user BestYet acts as | Yes | |
| `GRIST_DOC_ID` | Id of the Grist document | The app: yes. The init script: only for an existing document (optional in guided mode, not used with `--create`) | |
| `PORT` | Port the container will listen on (not yet used by `flask run`; see [Development](#development)) | No | `8585` |
| `TZ` | Time zone, used for "today" | No | `UTC` |

A `.env` file is not required (real environment variables work and take precedence), but it is strongly recommended for local use: copy [`.env.example`](.env.example) to `.env` and fill it in.

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
flask --app bestyet run --debug --port 8585
ruff check . && ruff format --check . && pytest
```

`flask run` does not read `PORT`: it listens on Flask's own default (5000) unless you pass `--port`, so use `--port 8585` (or the value of `PORT`). The container work will use `PORT`.

Enable the git hooks once per clone with `git config core.hooksPath .githooks`. They refuse commits and merges on `main`: work on a branch and open a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Status and Limitations

BestYet 0.0.2 is a non-functional foundations release: the init script and the startup contract check work, but there is no app yet. It works with Grist's REST API, so it is tied to the document layout described in [SPEC.md](SPEC.md).

- No app screens yet: `flask --app bestyet run` serves only `/health`, once the startup contract check passes.
- No container image or compose file yet.
- No login: never expose it to the internet (see [Privacy](#privacy)).
- Grist's hosted service should work but is untested.

## License

MIT
