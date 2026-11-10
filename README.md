# BestYet

Mobile-friendly athletics results and personal best tracker, backed by a self-hosted [Grist](https://www.getgrist.com/) document. Not affiliated with Grist Labs.

Results end up scattered across spreadsheets and apps, and a personal best is hard to find on a phone. BestYet is a small web app that gives a quick phone view of a Grist document: see each athlete's PBs, add a meet, and enter results from inside it.

> **Work in progress.** See [SPEC.md](SPEC.md) for what is being built.

## Getting started

Coming soon: set up a Grist document with the init script, then run the app.

## Development

    python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
    flask --app bestyet run --debug
    ruff check . && ruff format --check . && pytest

## Configuration

Set `GRIST_URL`, `GRIST_DOC_ID` and `GRIST_API_KEY` (required), and optionally `PORT` (default 8585) and `TZ` (default UTC), as environment variables. For development, copy `.env.example` to `.env`; real environment variables take precedence.

## Security

BestYet has no login. **Do not expose it to the internet.** Run it on a private network behind your own access control. See [SECURITY.md](SECURITY.md).

## Licence

[MIT](LICENSE)
