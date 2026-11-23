# Repository Guidelines

## Project structure

The root executable, `codex-human-transcript`, is a Python 3 script that reads Codex and Claude Code JSONL sessions. Transcript discovery, parsing, rendering, and the curses dashboard live in that file. Keep its extensionless name intact because the test suite loads it by path.

Behavioral tests live in `tests/test_codex_human_transcript.py`. The JSONL samples in `tests/fixtures/claude/` cover resumed and compacted conversations. For background on Claude support, see the design in `docs/specs/` and the implementation plan in `docs/superpowers/plans/`.

## Local development

Run commands from the repository root. The executable uses the Python standard library and runs directly without a build step.

- `./codex-human-transcript` opens the dashboard in an interactive terminal.
- `./codex-human-transcript --help` lists options for transcript selection, display, and logging.
- `./codex-human-transcript --file tests/fixtures/claude/resumed.jsonl --once --no-color --no-ledger` renders a sample session once without ANSI colors or ledger writes.

## Coding style

Use four-space indentation, snake_case for functions and variables, PascalCase for classes, and uppercase names for module constants. Follow the existing type annotations for paths, transcript records, and return values. The repository has no configured formatter or linter.

## Testing

Run `python3 -m pytest -q` for the full suite. During development, use `python3 -m pytest -q -k render` to select rendering tests. Name tests `test_<behavior>` and start each bug fix with a failing regression test. Reuse the record builders and pytest’s `tmp_path` fixture for filesystem cases. Use synthetic transcript content in committed fixtures. The suite has no configured coverage threshold.

## Commits and pull requests

Follow the Conventional Commit style used by the import commit, for example `fix: preserve timestamps in resumed sessions`. Branch from `origin/main` using `type/slug`. Open draft pull requests focused on one demonstrable change. Describe the user-visible behavior, link the issue with `Closes #N`, and include the verification commands and results.
