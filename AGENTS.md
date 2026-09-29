# Repository Guidelines

## Project Structure

This is a Python 3.11 desktop application built with PySide6. `main.py` starts the app. Keep UI code in `ui/`, startup and coordination in `app/`, configuration handling in `config/`, domain models in `models/`, background work and RabbitMQ integration in `services/`, and persistent task/index state in `state/`. Shared helpers belong in `utils/`. Tests are in `tests/`; operational and protocol documentation is in `docs/`. Windows packaging files live in `packaging/`, with the build script in `scripts/` and images/styles in `assets/` and `ui/`.

## Setup, Run, and Build

- `uv sync` installs the application dependencies from `pyproject.toml` and `uv.lock`.
- `uv run python main.py` launches the desktop app; add `--config config/app_config.yaml` to select a configuration explicitly.
- `uv run python -m unittest discover -s tests -v` runs the test suite.
- `uv sync --group build` installs packaging tools. On Windows, `powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1` builds the onedir executable. See `docs/build_windows.md` for installer steps.

## Code and Naming

Use four spaces for indentation and follow the existing Python style: type hints, `snake_case` for functions and variables, `PascalCase` for classes, and descriptive module names. Keep modules within their existing responsibility areas and match neighboring APIs and error-handling patterns. No formatter or linter is configured in `pyproject.toml`; do not introduce one as part of an unrelated change.

## Tests

Use the standard library `unittest` framework. Name test files `test_<area>.py`, test classes `<Feature>Test`, and test methods `test_<behavior>`. Add or update focused tests alongside changes, especially for task state, folder scanning, message routing, and UI behavior. Run the full command above before submitting when practical; GUI-dependent tests may skip when PySide6 is unavailable.

## Commits and Pull Requests

Recent history favors short, imperative commit subjects (for example, `Fix ...` or `Prepare ...`); Korean subjects are also present. Keep each commit focused. A pull request should explain the user-visible or behavioral change, list validation commands and results, link related issues when available, and include screenshots for UI changes. Call out configuration or packaging changes and update the relevant `docs/` guide when behavior or operations change.

## Configuration and Sensitive Data

Use `config/app_config.yaml` and `config/recipe_config.yaml` as development templates; do not commit machine-specific credentials, broker settings, or private recipe files. Runtime state and logs belong under the user's AppData directory, not in the repository or installation folder.
