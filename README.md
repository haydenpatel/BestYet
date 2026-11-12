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

Set up a Grist document with the init script, `bestyet-init`. It only ever adds what is missing (tables, columns, formulas, the personal-bests page, example data), so running it again is safe.

**Set up your settings first.** The script reads environment variables (or a `.env` file: copy [`.env.example`](.env.example) to `.env` and fill it in). `GRIST_URL` and `GRIST_API_KEY` are always needed. `GRIST_DOC_ID` is needed to set up an existing document with flags, and not at all for `--create`; in guided mode it is optional (it just adds the option to set up that document). Nothing is saved by the script.

- **Guided:** run `bestyet-init` with no options in a terminal. It asks what it needs (workspace, new or existing document, name, example data), shows the plan and asks before changing anything. It ends with a summary and, for a new document, the `GRIST_DOC_ID=...` line to add to your settings.
- **With flags:** for scripts, containers and repeat runs. Without a terminal it never asks, so give every value you need as a flag.

| Flag | What it does | Required | Default |
| --- | --- | --- | --- |
| `--create` | Create a new document | No | off: set up the document in `GRIST_DOC_ID` |
| `--workspace W` | Workspace id or name for the new document | Only with `--create` (asked for in a terminal) | none |
| `--org O` | Organisation id or name, to narrow `--workspace` | No | all organisations |
| `--name N` | Name of the new document; one that already exists in the workspace is never duplicated | No | `BestYet` |
| `--seed-implements` | Also add the example implements (the example events are always added) | No | off |
| `--demo-data` | Also load fictional athletes, meets and results; refused unless those tables are empty | No | off |
| `--allow-existing-tables` | Add to a document that has tables of its own (a blank one made in Grist has an empty `Table1`; `--create` avoids this) | No | off: refused |
| `-y`, `--yes` | Apply without asking | Without a terminal (unless `--dry-run`) | off: asks |
| `--dry-run` | Show the plan and change nothing | No | off |
| `--check` | Read-only: list every difference from the schema contract, or say it matches; exits non-zero on any difference. The app runs the same check at startup | No | off |

| Setting | Required | Notes |
| --- | --- | --- |
| `GRIST_URL` | Yes | Base URL of the Grist server |
| `GRIST_API_KEY` | Yes | Never printed |
| `GRIST_DOC_ID` | Yes, unless `--create`; optional in guided mode | The document to set up or `--check` |

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
