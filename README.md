# codex-human-transcript

Read local Codex and Claude Code sessions in your terminal. Requires Python 3.10 or newer.

```sh
git clone https://github.com/thewrz/codex-human-transcript.git
cd codex-human-transcript
```

| Platform | Run from the cloned folder |
| --- | --- |
| Windows (PowerShell or Command Prompt) | `.\codex-human-transcript.bat` |
| macOS or Linux | `./codex-human-transcript` |

The dashboard opens when Python has `curses`. Press `q` to quit. With standard Windows Python, the launcher follows the latest transcript as plain text. Press Ctrl+C to stop. It runs as your user and needs no PowerShell policy change.

Append `--file "path/to/session.jsonl" --once` to print one transcript and exit. Use `--help` for all options.
