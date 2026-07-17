# Contributing to NeverDry

Thanks for your interest in NeverDry — a Home Assistant custom integration for
ET-based smart irrigation. Contributions of all kinds are welcome: bug reports,
fixes, features, documentation, and help verifying the science.

## Ways to contribute

- **Report a bug or request a feature** — open a GitHub issue with clear steps /
  context. For irrigation behaviour, include your delivery mode and valve type.
- **Send a pull request** — see the workflow below.
- **Improve the docs** — user/developer manuals live in `docs/`; the engineering
  design notes live in the project documentation set (see *Understanding the
  architecture* below).
- **Help verify the science** — the bibliography behind the model is being
  reviewed claim-by-claim against primary sources. The method is documented and
  reproducible; picking up a few references is a great first contribution.

## Development setup

NeverDry targets **Python 3.11 / 3.12** and runs inside Home Assistant.

```bash
# clone your fork, then from the repo root:
# (the project pins dependencies via uv.lock; uv or pip both work)
pip install -r requirements_test.txt          # pytest + pytest-asyncio
pip install pytest-cov ruff bandit            # tooling used by CI
# Home Assistant must be importable to run the test suite.
```

The integration code lives in `custom_components/never_dry/`; tests in `tests/`.

## Before you commit — run what CI runs

CI will reject a PR that fails any of these, so run them locally first. They
mirror `.github/workflows/lint.yml` and `tests.yml`:

```bash
# 1. Lint + format (CI runs BOTH check and format --check)
ruff check  custom_components/never_dry/ tests/
ruff format custom_components/never_dry/ tests/      # then re-run with --check

# 2. Tests (must keep coverage >= 75%)
python -m pytest tests/ -v

# 3. Security scan
bandit -r custom_components/never_dry/ --severity-level medium --confidence-level medium
```

A pre-commit config is provided (`ruff --fix`, `ruff-format`, `bandit`):

```bash
pip install pre-commit && pre-commit install
```

> ⚠️ **Never run `ruff format` on `manifest.json` or other JSON files.** It adds
> trailing commas and corrupts the JSON, which breaks the integration and CI.
> The format command above is scoped to Python paths on purpose.

CI also validates the integration with **hassfest** and **HACS**; keep
`manifest.json` and `hacs.json` valid.

## Branch & PR workflow

- **Branch off `main`** with a descriptive name (e.g. `fix/valve-close-leak`,
  `feat/valve-entity-support`). Do **not** commit directly to `main`.
- Keep commits focused; write **commit messages and PR descriptions in English**.
- Open a PR against `main`. Describe *what* and *why*, and link any related issue
  (`Closes #123`). Make sure all CI checks pass.
- For new behaviour, add or update tests. For changes to irrigation/valve safety,
  a **non-regression test** of existing behaviour is expected.

## Code style

- Enforced by **ruff** (config in `pyproject.toml`): line length 120, target
  `py311`, rule sets `E,W,F,I,B,S,UP,SIM,RUF`.
- Match the style and altitude of the surrounding code; prefer clarity over
  cleverness.

## Understanding the architecture (start here)

To make a meaningful change, read the design notes — start with the index:

1. [`docs/design/README.md`](docs/design/README.md) — design & engineering notes
   in reading order (architecture → direction → science → testing).
2. `docs/developer_manual.md` — architecture overview, formulas, module map.
3. `docs/ha_integration_guide.md` — Home Assistant integration patterns.

The actuator-abstraction direction
([`docs/design/actuator-abstraction.md`](docs/design/actuator-abstraction.md))
is a **Draft** open for input on #74 — see *How we record design decisions* below.

## How we record design decisions

Significant or cross-cutting changes are captured as a single **design note**
whose `Status` field moves through a lifecycle. *RFC* and *ADR* are not separate
file types — they are **phases of the same document**:

```
Draft        → internal proposal, not yet circulated for comment
  → Proposed → open for comment (the "RFC" phase — usually on a GitHub issue)
  → Accepted → the decision is settled (this is the "ADR")
```

Plus terminal states: `Rejected` / `Withdrawn` / `Deferred`, and later
`Superseded` / `Deprecated`. A note is **never** marked `Accepted` while the
decision is still open for input.

**What this means for you:** for a large or architectural change, open or comment
on the relevant design note *before* sending a big PR — it avoids rework. For
example, the actuator-abstraction direction (design note
`05_actuator_abstraction_and_orchestration.md`, currently `Draft`) grew out of
[@fpytloun](https://github.com/fpytloun)'s proposal in **#74** — community input
like that is exactly how the bigger decisions get shaped, and it's much
appreciated. Small, self-contained fixes can go straight to a PR.

## Reporting security issues

Please **do not** open public issues for security vulnerabilities. See
[`SECURITY.md`](SECURITY.md) for private disclosure.

## License

By contributing, you agree that your contributions are licensed under the
project's [LICENSE](LICENSE).
