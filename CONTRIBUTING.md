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
