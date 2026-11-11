# Contributing to Keyup

Thanks for taking the time. Bug reports, fixes and documentation improvements are all welcome.

## Before you start

- **Bugs:** open an issue with what you did, what you expected and what happened. Include the Keyup
  version (or commit), how it is deployed (`docker compose`, a dev checkout) and the relevant log lines
  from `docker compose logs api worker`. Remove tokens, passwords and anything identifying your site.
- **Features and larger changes:** open an issue first so the design can be agreed before you write
  the code. Keyup's API is consumed by recorders and other programs, so a change to a route, a
  payload or a default is a compatibility question as much as a code one.
- **Security problems:** do not open a public issue; see [SECURITY.md](SECURITY.md).

## Development setup

You need Docker (for the throwaway Postgres containers), Python 3.12, Node 24, ffmpeg and, to
regenerate the Python locks, [uv](https://docs.astral.sh/uv/).

```bash
make dev-db test-db          # Postgres on 55440 (dev) and 55441 (tests)
make venv migrate            # backend/.venv from the lock, alembic upgrade head
cd backend && .venv/bin/uvicorn app.main:app --port 8096 --reload
cd frontend && npm ci && npm run dev     # http://localhost:5174, proxies /api to :8096
```

## Checks a pull request must pass

CI (`.github/workflows/ci.yml`) runs the same steps; run them locally first:

```bash
make lint                    # ruff
make migration-roundtrip     # every alembic revision down and up again
make test                    # backend suite against the test Postgres
cd frontend && npm run lint && npm test && npm run build
```

The Playwright suite (`make e2e`) reads a live archive and is not part of CI; run it when you change
the UI. See `frontend/README.md`.

## Conventions

- **Tests with the change.** A bug fix comes with a test that fails without it; a feature with tests
  for its behaviour. Test fixtures are synthetic (`backend/tests/fixtures/`); never commit real
  recordings, transcripts, radio IDs or credentials.
- **Migrations.** Schema changes go through Alembic (`backend/alembic/versions/`). Every revision
  must downgrade cleanly; `make migration-roundtrip` checks it.
- **Dependencies.** Edit `backend/pyproject.toml`, then `make lock` (keeps existing pins) and commit
  the regenerated `backend/requirements*.lock`. Frontend dependencies go through `npm install` and the
  committed `package-lock.json`. After either, run `make third-party` and commit `THIRD-PARTY.md`.
  New dependencies must be under a licence compatible with AGPL-3.0-or-later.
- **Defaults.** Changing a default changes every existing installation on upgrade. Call it out in
  the pull request and in `CHANGELOG.md` under *Unreleased*.
- **Style.** Python is checked by ruff (`backend/pyproject.toml`); the frontend by Biome
  (`npm run lint`). Keep commits focused, with a message that says why as well as what.
- **Docs.** If behaviour an operator can see changes, update the matching page in `docs/`.

## How changes are merged

Pull requests are reviewed here. An accepted change is applied to the maintainer's working tree with
its authorship preserved and appears in the next release commit, so a merged pull request may be
closed as "applied" rather than merged with the GitHub button.

## Licence

Keyup is licensed under the GNU Affero General Public License v3.0 or later ([LICENSE](LICENSE)). By
submitting a contribution you agree that it is licensed under the same terms.
