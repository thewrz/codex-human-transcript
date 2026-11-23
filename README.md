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

In the dashboard, select a running Codex worker and press `[` to lower or `]` to raise its thinking effort for future turns. The dashboard steps through the levels its model supports. The next-turn label pulses while Python checks the setting on the worker, then becomes static after confirmation. The gear label shows the model and effort recorded for the most recent request.

Effort controls use the local Codex App Server Unix socket and require the Python `websockets` package (12 or later). Transcript viewing works with the standard library. This control uses the experimental thread settings API available in Codex 0.171.0 and works with multi-agent v2 disabled.
