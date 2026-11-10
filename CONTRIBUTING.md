# Contributing

## Git hooks

Enable the repo's hooks once per clone:

```bash
git config core.hooksPath .githooks
```

- Commits and merges directly on `main` are refused. Work on a branch and open a PR.
- Staged changes are scanned for secrets with [Betterleaks](https://github.com/betterleaks/betterleaks) when it is installed.

## Branches and commits

- Branches: `<issue#>-<short-description>`, or `<type>/<short-description>` without an issue.
- Commits and PR titles: `type: Sentence case` (`feat`, `fix`, `refactor`, `test`, `docs`, `chore`).
- PRs close their issue (`Closes #N`) and stay short.

## Tests

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && pytest
```

Unit tests mock Grist. Tests marked `integration` need a real Grist document and are skipped by default and in CI. To run them, set `GRIST_URL`, `GRIST_DOC_ID` and `GRIST_API_KEY` (dev `.env` or environment) for a **TEST** document, then:

```bash
BESTYET_INTEGRATION=1 pytest -m integration
```

### TEST-name guard

Any test that writes, deletes or loads demo data must use the `test_grist` fixture (`tests/conftest.py`). It reads the target document's name and fails the run unless the name contains `TEST` (case-sensitive). Never point integration tests at a real document. The guard lives only in `tests/`; the app and the init script have no such check and work on any document.
