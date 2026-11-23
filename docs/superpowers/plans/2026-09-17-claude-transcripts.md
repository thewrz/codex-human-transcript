# Claude Code Transcript Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach `codex-human-transcript` to discover, render, tail, and mirror Claude Code session transcripts alongside Codex rollouts in one merged session list, fixing the four shared-plumbing defects that Claude transcripts would otherwise hit constantly.

**Architecture:** A `Format` adapter object (`CODEX`, `CLAUDE`) owns every format-specific behavior — discovery, metadata, rendering, state — while a normalized `SessionRef` descriptor flows through the format-neutral layers (`NdjsonTail`, `TranscriptCache`, `TextLog`, `MirrorSet`, dashboard, `follow()`). Stage 1 repairs framing, checkpointing, redaction, and read bounds first, so the new format lands on plumbing that survives split records and large payloads.

**Tech Stack:** Python 3.11+, standard library only (`curses`, `json`, `argparse`, `codecs`, `hashlib`, `pathlib`). Tests: `pytest`. Lint: `ruff` via `uvx`.

**Spec:** `~/github/codex-human-transcript/docs/specs/2026-09-17-claude-transcripts-design.md`

## Global Constraints

- **Single file, stdlib only.** All production code goes in `~/github/codex-human-transcript/codex-human-transcript`. No new modules, no third-party imports, no package split.
- **Config path unchanged:** `~/.config/codex-human-transcript/config.json`. **Log dir unchanged:** `~/.local/state/codex-human-transcript/logs/`.
- **Existing Codex tests pass unchanged** at every task boundary. Public names the tests use keep working: `cht.render(record)`, `cht.TranscriptCache(path)`, `cht.TextLog(log_dir, path)`, `cht.mirror(root, log_dir, poll=..., once=...)`, `cht.has_completed(path)`, `cht.discover_sessions(root)`, `cht.resolve_root(...)`, `cht.load_config/save_config`.
- **Codex mirror log names unchanged:** an existing Codex main session log stays `<rollout-stem>.log`.
- **Never crash on unexpected input;** one bad source never stops the others.
- **Constants (exact values):** `READ_CHUNK_BYTES = 1048576`, `MAX_RECORD_BYTES = 4194304`, `SNIFF_BYTES = 262144`, `SNIFF_RECORDS = 50`, `SCAN_CHUNK = 65536`, `MAX_SCAN_BYTES = 1048576`, `RECENT_DAYS = 14`, `MAIN_LIMIT = 3`, `SUBAGENT_LIMIT = 20`, `CONFIG_VERSION = 2`.
- **States (exact strings):** `"working"`, `"done"`, `"error"`, `"unknown"`.
- **Format tags (exact strings):** `"codex"`/`"CX"`, `"claude"`/`"CL"`.
- **Verification per task:** `python3 -m pytest tests -q` and `uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`.
- **Line-length limit:** 120.
- **Commit style:** Conventional Commits, with trailer `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `codex-human-transcript` (modify) | The whole tool. New sections are added in place, next to the code they extend: framing/redaction helpers near the top, `Format`/`SessionRef` after the Codex renderers, Claude discovery+render after the Codex discovery block, config/picker changes in the settings block. |
| `tests/test_codex_human_transcript.py` (modify) | All tests. New helpers `claude_rec()`, `write_jsonl()`, `write_claude_session()` join the existing `rec()`/`meta()`/`write_rollout()` helpers. |
| `tests/fixtures/claude/` (create) | Sanitized real Claude transcripts: `resumed.jsonl`, `compacted.jsonl`. |
| `.gitignore` (create) | `__pycache__/`, `*.pyc`, `.pytest_cache/`. |

---

## Task 0: Repo baseline

**Files:**
- Create: `~/github/codex-human-transcript/.gitignore`
- Modify: none

**Interfaces:**
- Consumes: nothing.
- Produces: a git repo at `~/github/codex-human-transcript` whose first commit is the current green state; every later task commits into it.

- [ ] **Step 1: Confirm the current state is green**

```bash
cd ~/github/codex-human-transcript
python3 -m pytest tests -q
```

Expected: all tests pass. If any fail, STOP and report — the baseline must be green before changes.

- [ ] **Step 2: Create .gitignore**

```
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 3: Initialize the repo and commit the baseline**

```bash
cd ~/github/codex-human-transcript
git init
git add .gitignore codex-human-transcript tests docs
git commit -m "chore: baseline codex-human-transcript before Claude support

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: Verify the commit exists**

Run: `git -C ~/github/codex-human-transcript log --oneline -1`
Expected: one commit listed.

---

# Stage 1 — shared plumbing fixes

## Task 1: Bounded, record-aligned tailing

**Files:**
- Modify: `codex-human-transcript` — constants block (~line 39-45), `NdjsonTail` (~line 632-670), `TranscriptCache.update` (~line 692), `TextLog.sync` (~line 767)
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class TailRead(NamedTuple): truncated: bool; records: list[dict]; more_pending: bool` — the new return of `NdjsonTail.read()`.
  - `NdjsonTail.committed: int` — byte offset just past the last **complete** record.
  - `NdjsonTail.read(backfill: int | None = None, max_bytes: int = READ_CHUNK_BYTES) -> TailRead`.
  - Oversized records surface as the record `{"__oversized__": <int bytes>}`; `render()` turns that into a placeholder in Task 12 and `advance()` ignores it.

- [ ] **Step 1: Write the failing tests**

```python
def test_tail_read_reports_committed_offset_excluding_partial(tmp_path):
    path = tmp_path / "rollout-t1.jsonl"
    first = json.dumps(rec("agent_message", top="event_msg", message="one")) + "\n"
    path.write_text(first + '{"timestamp": "2026', encoding="utf-8")  # trailing partial record
    tail = cht.NdjsonTail(path)
    result = tail.read()
    assert len(result.records) == 1
    assert tail.committed == len(first.encode())
    assert tail.offset == path.stat().st_size


def test_tail_read_is_bounded_and_reports_more_pending(tmp_path):
    path = tmp_path / "rollout-t2.jsonl"
    line = json.dumps(rec("agent_message", top="event_msg", message="x" * 2000)) + "\n"
    path.write_text(line * 1000, encoding="utf-8")  # ~2 MB, larger than READ_CHUNK_BYTES
    tail = cht.NdjsonTail(path)
    first = tail.read(max_bytes=65536)
    assert first.more_pending is True
    assert 0 < len(first.records) < 1000
    seen = len(first.records)
    while True:
        nxt = tail.read(max_bytes=65536)
        seen += len(nxt.records)
        if not nxt.more_pending:
            break
    assert seen == 1000


def test_tail_skips_oversized_record_without_stalling(tmp_path):
    path = tmp_path / "rollout-t3.jsonl"
    huge = json.dumps(rec("agent_message", top="event_msg", message="y" * (cht.MAX_RECORD_BYTES + 1000)))
    good = json.dumps(rec("agent_message", top="event_msg", message="after"))
    path.write_text(huge + "\n" + good + "\n", encoding="utf-8")
    tail = cht.NdjsonTail(path)
    records = []
    while True:
        result = tail.read()
        records.extend(result.records)
        if not result.more_pending:
            break
    assert any(r.get("__oversized__") for r in records)
    assert any(r.get("payload", {}).get("message") == "after" for r in records)
    assert tail.committed == path.stat().st_size
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "tail_read or oversized"`
Expected: FAIL — `AttributeError: 'tuple' object has no attribute 'records'` and `module 'cht' has no attribute 'MAX_RECORD_BYTES'`.

- [ ] **Step 3: Implement bounded, record-aligned tailing**

Add to the constants block:

```python
READ_CHUNK_BYTES = 1048576        # max bytes absorbed per tail pass
MAX_RECORD_BYTES = 4194304        # a single record larger than this is skipped, not buffered
```

Replace `NdjsonTail.read` and add `committed`, keeping the existing decoder behavior:

```python
class TailRead(NamedTuple):
    truncated: bool
    records: list[dict[str, Any]]
    more_pending: bool


class NdjsonTail:
    """Reads only newly appended bytes of a transcript; survives multibyte splits and truncation."""

    def __init__(self, path: Path, offset: int = 0):
        self.path = path
        self.offset = offset
        self.committed = offset
        self.partial = ""
        self._partial_bytes = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def reset(self, offset: int = 0) -> None:
        self.offset = offset
        self.committed = offset
        self.partial = ""
        self._partial_bytes = 0
        self._decoder.reset()

    def read(self, backfill: int | None = None, max_bytes: int = READ_CHUNK_BYTES) -> TailRead:
        try:
            size = self.path.stat().st_size
        except OSError:
            return TailRead(False, [], False)
        truncated = size < self.offset
        if truncated:
            self.reset()
        if size == self.offset:
            return TailRead(truncated, [], False)
        try:
            with self.path.open("rb") as handle:
                if self.offset == 0 and backfill and size > backfill:
                    handle.seek(size - backfill)
                    handle.readline()          # discard the partial record at the seek point
                    self.offset = self.committed = handle.tell()
                else:
                    handle.seek(self.offset)
                chunk = handle.read(max_bytes)
                self.offset = handle.tell()
        except OSError:
            return TailRead(truncated, [], False)
        pieces = (self.partial + self._decoder.decode(chunk)).split("\n")
        self.partial = pieces.pop()
        self._partial_bytes = len(self.partial.encode("utf-8", "replace"))
        self.committed = self.offset - self._partial_bytes
        out = list(records(pieces))
        if self._partial_bytes > MAX_RECORD_BYTES:      # a single record too big to hold
            out.append({"__oversized__": self._partial_bytes})
            self._drop_partial()
        return TailRead(truncated, out, self.offset < size)

    def _drop_partial(self) -> None:
        """Abandon an oversized in-progress record: skip to its terminating newline."""
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                while True:
                    chunk = handle.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    nl = chunk.find(b"\n")
                    if nl >= 0:
                        self.offset = self.offset + nl + 1
                        break
                    self.offset += len(chunk)
        except OSError:
            pass
        self.partial = ""
        self._partial_bytes = 0
        self.committed = self.offset
        self._decoder.reset()
```

Update the two existing call sites to the named tuple:

```python
# TranscriptCache.update
result = self.tail.read(backfill=BACKFILL_BYTES)
truncated, new_records = result.truncated, result.records
```

```python
# TextLog.sync
result = self.tail.read()
truncated, new_records = result.truncated, result.records
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS, including the pre-existing multibyte-split test.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "fix: bound tail reads and track a record-aligned committed offset

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Record-aligned, versioned mirror checkpoints

**Files:**
- Modify: `codex-human-transcript` — `TextLog` (~line 731-784)
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `NdjsonTail.committed`, `TailRead` (Task 1).
- Produces:
  - Offset file content is now JSON: `{"v": 2, "offset": <int>, "format": "codex"|"claude", "render": {}}`.
  - `TextLog._resume_offset() -> int` still returns an int; a bare-integer (v1) file is accepted as a Codex offset.
  - `TextLog.sync()` streams blocks to the file handle instead of buffering a list.

- [ ] **Step 1: Write the failing tests**

```python
def test_text_log_checkpoint_keeps_partial_record_for_next_run(tmp_path):
    src = tmp_path / "sessions" / "rollout-c1.jsonl"
    src.parent.mkdir(parents=True)
    full = json.dumps(rec("agent_message", top="event_msg", message="complete one")) + "\n"
    half = json.dumps(rec("agent_message", top="event_msg", message="second one"))
    src.write_text(full + half[:40], encoding="utf-8")   # second record still being written
    cht.TextLog(tmp_path / "logs", src).sync()
    with src.open("a", encoding="utf-8") as handle:
        handle.write(half[40:] + "\n")
    log = cht.TextLog(tmp_path / "logs", src)            # fresh object = process restart
    log.sync()
    text = log.path.read_text(encoding="utf-8")
    assert text.count("complete one") == 1
    assert text.count("second one") == 1


def test_text_log_offset_file_is_versioned_json_and_reads_legacy_int(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-c2.jsonl",
                        [meta(), rec("agent_message", top="event_msg", message="first")])
    log = cht.TextLog(tmp_path / "logs", src)
    log.sync()
    saved = json.loads(log.offset_path.read_text(encoding="utf-8"))
    assert saved["v"] == 2 and saved["format"] == "codex" and saved["offset"] > 0
    log.offset_path.write_text(str(saved["offset"]), encoding="utf-8")   # legacy v1 file
    with src.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec("agent_message", top="event_msg", message="second")) + "\n")
    cht.TextLog(tmp_path / "logs", src).sync()
    text = log.path.read_text(encoding="utf-8")
    assert text.count("first") == 1 and text.count("second") == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "checkpoint or versioned_json"`
Expected: FAIL — the log duplicates or loses `second one`, and the offset file is a bare integer.

- [ ] **Step 3: Implement versioned, record-aligned checkpoints**

```python
CHECKPOINT_VERSION = 2


class TextLog:
    """Plain-text mirror of one transcript: `<log_dir>/<name>.log`, resumable."""

    def __init__(self, log_dir: Path, source: Path, fmt_name: str = "codex",
                 log_name: str | None = None, render_opts: dict[str, Any] | None = None):
        self.source = source
        self.fmt_name = fmt_name
        self.render_opts = render_opts or {}
        stem = log_name or source.stem
        self.path = log_dir / f"{stem}.log"
        self.offset_path = log_dir / f".{stem}.offset"
        offset = self._resume_offset()
        self.tail = NdjsonTail(source, offset=offset)
        self.dedupe = Deduper()
        self._fresh = offset == 0

    def _resume_offset(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            raw = self.offset_path.read_text(encoding="utf-8").strip()
        except OSError:
            return 0
        try:
            saved = json.loads(raw)
        except ValueError:
            try:
                return int(raw)            # legacy v1: a bare integer, always Codex
            except ValueError:
                return 0
        if not isinstance(saved, dict):
            return 0
        if saved.get("format", "codex") != self.fmt_name or saved.get("render", {}) != self.render_opts:
            return 0                       # options changed → rebuild rather than append inconsistently
        try:
            return int(saved.get("offset", 0))
        except (TypeError, ValueError):
            return 0

    def _save_offset(self) -> None:
        payload = {"v": CHECKPOINT_VERSION, "offset": self.tail.committed,
                   "format": self.fmt_name, "render": self.render_opts}
        self.offset_path.write_text(json.dumps(payload), encoding="utf-8")
```

Rewrite `sync()` so blocks stream to the handle and the committed offset is what gets saved:

```python
    def sync(self) -> bool:
        result = self.tail.read()
        rebuild = result.truncated or self._fresh
        if not rebuild and not result.records:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        wrote = False
        with self.path.open("w" if rebuild else "a", encoding="utf-8") as handle:
            if rebuild:
                handle.write(self._header())
                wrote = True
            for record in result.records:
                rendered = render(record)
                if rendered and self.dedupe.fresh(rendered):
                    handle.write(CTRL_RE.sub(" ", strip_ansi(rendered)) + "\n")
                    wrote = True
        self._save_offset()
        self._fresh = False
        return wrote
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS, including the existing resume and truncation tests.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "fix: checkpoint mirrors at record boundaries with a versioned offset file

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Recursive, key-aware redaction

**Files:**
- Modify: `codex-human-transcript` — `clean()` (~line 70-85), `kv_lines()` (~line 214)
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `redact(value: Any) -> Any` — returns a copy with every value under a `SECRET_KEYS`-matching key replaced by the string `"[REDACTED]"`, recursing through dicts and lists. `kv_lines()` and `clean()` call it.

- [ ] **Step 1: Write the failing tests**

```python
def test_redact_masks_nested_secret_keys():
    out = cht.redact({"outer": {"api_key": "sk-live-123", "safe": "keep"},
                      "list": [{"authorization": "Bearer xyz"}]})
    assert out["outer"]["api_key"] == "[REDACTED]"
    assert out["outer"]["safe"] == "keep"
    assert out["list"][0]["authorization"] == "[REDACTED]"


def test_kv_lines_redacts_by_key_not_only_by_value():
    text = cht.kv_lines({"api_key": "sk-live-123", "cmd": "ls -la"})
    assert "sk-live-123" not in text
    assert "[REDACTED]" in text
    assert "ls -la" in text


def test_mirror_never_writes_a_secret_from_nested_tool_input(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-r1.jsonl", [
        meta(),
        rec("function_call", name="deploy",
            arguments=json.dumps({"env": {"API_KEY": "sk-live-987"}, "target": "prod"})),
    ])
    cht.TextLog(tmp_path / "logs", src).sync()
    text = (tmp_path / "logs" / "rollout-r1.log").read_text(encoding="utf-8")
    assert "sk-live-987" not in text
    assert "prod" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "redact or secret"`
Expected: FAIL — `module 'cht' has no attribute 'redact'`, and the mirror test finds `sk-live-987` in the log.

- [ ] **Step 3: Implement recursive redaction**

Add above `clean()`:

```python
REDACTED = "[REDACTED]"


def redact(value: Any, depth: int = 0) -> Any:
    """Recursively mask values whose key looks secret; leaves other values untouched."""
    if depth > 12:
        return value
    if isinstance(value, dict):
        return {str(k): (REDACTED if SECRET_KEYS.search(str(k)) else redact(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value]
    return value
```

In `clean()`, replace the outermost-only dict handling with the recursive call:

```python
def clean(value: Any, limit: int = 1200) -> str:
    dumped = not isinstance(value, str)
    if isinstance(value, (dict, list)):
        value = redact(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    ...  # the existing pattern scrubbers and truncation are unchanged
```

In `kv_lines()`, redact the structure first so keys still carry context:

```python
def kv_lines(args: dict[str, Any], limit: int = 400) -> str:
    safe = redact(args)
    return "\n".join(f"{key}: {clean(value, limit)}" for key, value in safe.items())
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "fix: redact secrets by key recursively before rendering or mirroring

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: One framing implementation in follow() and --once

**Files:**
- Modify: `codex-human-transcript` — `follow()` (~line 1369-1434)
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `NdjsonTail`, `TailRead` (Task 1).
- Produces: `follow()` no longer opens a text handle or calls `readline()`; it owns one `NdjsonTail` per source and drains it with `more_pending`.

- [ ] **Step 1: Write the failing test**

```python
def test_follow_once_does_not_lose_a_record_split_across_writes(tmp_path, capsys):
    path = tmp_path / "rollout-f1.jsonl"
    line = json.dumps(rec("agent_message", top="event_msg", message="split survivor")) + "\n"
    data = line.encode()
    path.write_bytes(data[: len(data) // 2])          # first half only
    path.write_bytes(data)                            # completed before --once runs
    cht.follow(path, None, once=True, poll=0.01)
    assert "split survivor" in capsys.readouterr().out


def test_follow_once_emits_every_record_of_a_large_file(tmp_path, capsys):
    path = tmp_path / "rollout-f2.jsonl"
    lines = [json.dumps(rec("agent_message", top="event_msg", message=f"msg{i} " + "z" * 3000)) + "\n"
             for i in range(400)]                     # comfortably past READ_CHUNK_BYTES
    path.write_text("".join(lines), encoding="utf-8")
    cht.follow(path, None, once=True, poll=0.01)
    out = capsys.readouterr().out
    assert "msg0" in out and "msg399" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "follow_once"`
Expected: the large-file test FAILs (only the first chunk is emitted) once Task 1's bound is in place.

- [ ] **Step 3: Rewrite follow()'s reading loop**

Replace the handle/`readline()` machinery. The switching, footer, and mirror behavior stay as they are:

```python
def follow(path: Path | None, root: Path | None, once: bool, poll: float,
           log_dir: Path | None = None) -> None:
    printed = False
    dedupe = Deduper()
    mirrors = MirrorSet(log_dir) if log_dir else None

    def emit(record: dict[str, Any]) -> None:
        nonlocal printed
        rendered = render(record)
        if rendered and dedupe.fresh(rendered):
            print(rendered, flush=True)
            printed = True

    def drain(tail: NdjsonTail) -> None:
        while True:
            result = tail.read()
            for record in result.records:
                emit(record)
            if not result.more_pending:
                return

    current = path
    tail: NdjsonTail | None = None
    announced = False
    last_footer = None
    next_refresh = 0.0
    while True:
        if root is not None:
            newest = newest_transcript(root)
            if newest is not None and newest != current:
                current, tail = newest, None
                dedupe = Deduper()
                print(STYLE.paint("1;90", f"\n── switched to {current.name} ──\n"), flush=True)
        if current is None or not current.is_file():
            if not announced:
                print(f"Waiting for a transcript under {root or path}", flush=True)
                announced = True
            time.sleep(poll)
            continue
        if tail is None:
            tail = NdjsonTail(current)
            drain(tail)
            if once:
                if mirrors is not None:
                    mirrors.sync([("file", "file", current)])
                return
            if not printed:
                print(f"Watching {current}", flush=True)
        if time.monotonic() >= next_refresh:
            ...  # unchanged footer + mirror refresh block
            next_refresh = time.monotonic() + BG_UPDATE_SECS
        before = tail.committed
        drain(tail)
        if tail.committed == before:
            time.sleep(poll)
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS, including the existing broken-pipe test.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "fix: read follow() through NdjsonTail so split records survive

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Per-source failure isolation and backfill repair

**Files:**
- Modify: `codex-human-transcript` — `TranscriptCache` (~line 673-726), `MirrorSet.sync` (~line 809), `save_config` (~line 602)
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `TailRead`, `NdjsonTail.committed`.
- Produces:
  - `TranscriptCache.error: str | None` — a short reason shown in the session row; set instead of raising.
  - `MirrorSet.sync()` returns created paths as before, and records failures in `MirrorSet.errors: dict[Path, str]` without raising.
  - `NdjsonTail.read(backfill=...)` extends its window once when the backfill point lands inside a record larger than the window.

- [ ] **Step 1: Write the failing tests**

```python
def test_cache_marks_error_instead_of_raising_when_file_vanishes(tmp_path):
    path = write_rollout(tmp_path / "rollout-e1.jsonl",
                         [meta(), rec("agent_message", top="event_msg", message="here")])
    cache = cht.TranscriptCache(path)
    cache.update()
    path.unlink()
    cache.update()                       # must not raise
    assert cache.error is None or isinstance(cache.error, str)


def test_mirror_set_keeps_going_when_one_source_is_unreadable(tmp_path):
    good = write_rollout(tmp_path / "sessions" / "rollout-g1.jsonl",
                         [meta(), rec("agent_message", top="event_msg", message="good one")])
    bad = tmp_path / "sessions" / "rollout-g2.jsonl"
    bad.write_text("{}\n", encoding="utf-8")
    bad.chmod(0o000)
    mirrors = cht.MirrorSet(tmp_path / "logs")
    mirrors.sync([("a", "session", good), ("b", "session", bad)])   # must not raise
    bad.chmod(0o600)
    assert (tmp_path / "logs" / "rollout-g1.log").read_text().count("good one") == 1


def test_backfill_window_extends_past_one_huge_final_record(tmp_path):
    path = tmp_path / "rollout-b1.jsonl"
    big = json.dumps(rec("agent_message", top="event_msg", message="w" * 600_000)) + "\n"
    path.write_text(big, encoding="utf-8")
    cache = cht.TranscriptCache(path)
    cache.update()
    assert cache.lines, "a single 600 KB record must still render under the display backfill"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "vanishes or unreadable or backfill_window"`
Expected: FAIL — `cache.error` does not exist; the backfill test produces no lines.

- [ ] **Step 3: Implement isolation and the backfill extension**

In `NdjsonTail.read`, replace the plain backfill seek so a too-large trailing record still yields a usable window:

```python
                if self.offset == 0 and backfill and size > backfill:
                    start = size - backfill
                    handle.seek(start)
                    handle.readline()
                    if handle.tell() >= size:          # the window held no record boundary
                        window = min(MAX_RECORD_BYTES, size)
                        handle.seek(size - window)
                        if window < size:
                            handle.readline()
                    self.offset = self.committed = handle.tell()
```

In `TranscriptCache`, add `self.error: str | None = None` in `__init__` and wrap the pass:

```python
    def update(self) -> bool:
        try:
            result = self.tail.read(backfill=BACKFILL_BYTES)
        except OSError as exc:
            self.error = f"read failed: {exc.strerror or exc}"
            return False
        self.error = None
        ...  # the rest of the existing body, using result.truncated / result.records
```

In `MirrorSet`, collect failures per source:

```python
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.logs: dict[Path, TextLog] = {}
        self.errors: dict[Path, str] = {}

    def sync(self, sessions: list[tuple[str, str, Path]]) -> list[Path]:
        created = []
        for _, _, path in sessions:
            try:
                log = self.logs.get(path)
                if log is None:
                    log = self.logs[path] = TextLog(self.log_dir, path)
                if log.fresh:
                    created.append(log.path)
                log.sync()
                self.errors.pop(path, None)
            except OSError as exc:
                self.errors[path] = f"{exc.strerror or exc}"
        if sessions and sessions[0][2] in self.logs:
            point_latest(self.log_dir, self.logs[sessions[0][2]].path)
        return created
```

Make `save_config` non-fatal by returning a status instead of raising:

```python
def save_config(path: Path, data: dict[str, Any]) -> str | None:
    """Returns None on success, or a short error string the caller can show."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return f"config save failed: {exc.strerror or exc}"
    return None
```

Update `change_root()` to show that string in `status_msg` when it is not `None`.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "fix: isolate per-source failures and repair the oversized backfill window

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

# Stage 2 — the format seam

## Task 6: SessionRef, Format, and the Codex adapter

**Files:**
- Modify: `codex-human-transcript` — new section after the Codex renderers (~after line 420), plus `discover_sessions`, `TranscriptCache.__init__`, `TextLog.__init__`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: everything from Stage 1.
- Produces:
  - `class SessionRef(NamedTuple): fmt: "Format"; path: Path; kind: str; session_id: str | None; parent_id: str | None; agent_id: str | None; name: str; role: str; cwd: str | None; mtime: float; source_key: str` — `kind` is `"main"` or `"subagent"`.
  - `class Format:` with attributes `name`, `tag`, and methods `discover(root) -> list[SessionRef]`, `meta(path) -> dict`, `render(record, opts) -> str | None`, `advance(state, record) -> str`, `scan_state(path) -> str`, `dedupe: bool`.
  - `CODEX: Format` — wraps the existing Codex functions with no behavior change.
  - `source_key(fmt_name, root, parent_or_session_id, agent_id=None) -> str`.
  - `TranscriptCache(source: Path | SessionRef)` and `TextLog(log_dir, source: Path | SessionRef)` accept either; a bare `Path` means Codex.

- [ ] **Step 1: Write the failing tests**

```python
def test_codex_adapter_discovers_sessions_as_refs(tmp_path):
    root = tmp_path / "sessions"
    write_rollout(root / "2026/08/20/rollout-2026-08-20T09-00-00-aaaa.jsonl",
                  [meta(), rec("agent_message", top="event_msg", message="hi")])
    refs = cht.CODEX.discover(root)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.fmt is cht.CODEX and ref.kind == "main" and ref.role == "session"
    assert ref.source_key.startswith("codex-")


def test_source_key_differs_for_same_basename_under_different_roots(tmp_path):
    a = cht.source_key("claude", tmp_path / "rootA", "parent1", "agent-abc")
    b = cht.source_key("claude", tmp_path / "rootB", "parent1", "agent-abc")
    c = cht.source_key("claude", tmp_path / "rootA", "parent2", "agent-abc")
    assert a != b and a != c


def test_transcript_cache_and_textlog_still_accept_a_bare_path(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-s1.jsonl",
                        [meta(), rec("agent_message", top="event_msg", message="compat")])
    assert cht.TranscriptCache(src).update() is True
    log = cht.TextLog(tmp_path / "logs", src)
    log.sync()
    assert log.path.name == "rollout-s1.log"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "adapter or source_key or bare_path"`
Expected: FAIL — `module 'cht' has no attribute 'CODEX'`.

- [ ] **Step 3: Implement the seam**

```python
import hashlib

STATE_WORKING, STATE_DONE, STATE_ERROR, STATE_UNKNOWN = "working", "done", "error", "unknown"


def source_key(fmt_name: str, root: Path, owner_id: str, agent_id: str | None = None) -> str:
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:8]
    tail = f"-{agent_id}" if agent_id else ""
    return f"{fmt_name}-{digest}-{owner_id}{tail}"


class Format:
    """Everything format-specific: discovery, metadata, rendering, state."""

    def __init__(self, name: str, tag: str, discover: Callable[[Path], list["SessionRef"]],
                 meta: Callable[[Path], dict[str, Any]],
                 render: Callable[[dict[str, Any], dict[str, Any]], str | None],
                 advance: Callable[[str, dict[str, Any]], str],
                 scan_state: Callable[[Path], str], dedupe: bool):
        self.name = name
        self.tag = tag
        self.discover = discover
        self.meta = meta
        self.render = render
        self.advance = advance
        self.scan_state = scan_state
        self.dedupe = dedupe


class SessionRef(NamedTuple):
    fmt: Format
    path: Path
    kind: str                 # "main" | "subagent"
    session_id: str | None
    parent_id: str | None
    agent_id: str | None
    name: str
    role: str
    cwd: str | None
    mtime: float
    source_key: str
```

Codex adapter functions (thin wrappers over what exists):

```python
def codex_render(record: dict[str, Any], opts: dict[str, Any]) -> str | None:
    return render(record)                      # opts unused: Codex reasoning is encrypted


def codex_advance(state: str, record: dict[str, Any]) -> str:
    return next_state(payload_kind(record), state)


def codex_scan_state(path: Path) -> str:
    return STATE_DONE if has_completed(path) else STATE_WORKING


def codex_discover(root: Path) -> list[SessionRef]:
    refs: list[SessionRef] = []
    for name, role, path in discover_sessions(root):
        info = session_meta(path) or {}
        identity = subagent_identity(info)
        sid = str(info.get("id") or path.stem)
        refs.append(SessionRef(
            fmt=CODEX, path=path, kind="subagent" if identity else "main",
            session_id=sid, parent_id=str(info.get("parent_thread_id") or sid),
            agent_id=path.stem if identity else None, name=name, role=role,
            cwd=info.get("cwd"), mtime=_mtime(path) or 0.0,
            source_key=source_key("codex", root, sid, path.stem if identity else None)))
    return refs


CODEX = Format("codex", "CX", codex_discover, session_meta, codex_render,
               codex_advance, codex_scan_state, dedupe=True)
```

Accept either type in the two consumers, keeping `Path` working:

```python
def as_ref(source: "Path | SessionRef") -> "SessionRef":
    if isinstance(source, SessionRef):
        return source
    path = source
    return SessionRef(CODEX, path, "main", path.stem, path.stem, None, path.stem[-12:],
                      "session", None, _mtime(path) or 0.0, source_key("codex", path.parent, path.stem))
```

`TranscriptCache.__init__` and `TextLog.__init__` call `as_ref(source)` first, store `self.ref`, and use `self.ref.fmt` for rendering, dedupe, and state. `TextLog` keeps `<stem>.log` when `ref.fmt is CODEX and ref.kind == "main"`, otherwise uses `ref.source_key` (Task 7 adds the index file).

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS — every existing Codex test unchanged.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "refactor: add SessionRef and the Format seam with a Codex adapter

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Collision-free mirror identity

**Files:**
- Modify: `codex-human-transcript` — `TextLog`, `MirrorSet`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `SessionRef.source_key`, `as_ref` (Task 6).
- Produces:
  - `logs/index.json` mapping `source_key → log filename`, written on every `MirrorSet.sync()`.
  - `MirrorSet.sync(refs: list[SessionRef] | list[tuple[str, str, Path]])` — tuples still accepted.
  - Aliases of one physical file (same `st_dev`/`st_ino`) mirror once.

- [ ] **Step 1: Write the failing tests**

```python
def test_same_basename_under_two_parents_gets_two_logs(tmp_path):
    log_dir = tmp_path / "logs"
    refs = []
    for parent in ("p1", "p2"):
        src = write_rollout(tmp_path / parent / "agent-abc.jsonl",
                            [meta(), rec("agent_message", top="event_msg", message=f"from {parent}")])
        refs.append(cht.SessionRef(cht.CODEX, src, "subagent", parent, parent, "agent-abc",
                                   "abc", "worker", None, 0.0,
                                   cht.source_key("codex", tmp_path / parent, parent, "agent-abc")))
    mirrors = cht.MirrorSet(log_dir)
    mirrors.sync(refs)
    logs = sorted(p.name for p in log_dir.glob("*.log"))
    assert len(logs) == 2
    bodies = [(log_dir / name).read_text(encoding="utf-8") for name in logs]
    assert any("from p1" in b for b in bodies) and any("from p2" in b for b in bodies)
    assert json.loads((log_dir / "index.json").read_text(encoding="utf-8"))


def test_mirror_deduplicates_two_aliases_of_one_file(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-a1.jsonl",
                        [meta(), rec("agent_message", top="event_msg", message="only once")])
    alias = tmp_path / "alias.jsonl"
    os.link(src, alias)
    mirrors = cht.MirrorSet(tmp_path / "logs")
    mirrors.sync([("a", "session", src), ("b", "session", alias)])
    total = sum((p).read_text(encoding="utf-8").count("only once") for p in (tmp_path / "logs").glob("*.log"))
    assert total == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "two_parents or aliases"`
Expected: FAIL — both parents write `agent-abc.log`; the alias is mirrored twice.

- [ ] **Step 3: Implement stable mirror identity**

In `TextLog.__init__`, choose the name:

```python
        legacy = log_dir / f"{source.stem}.log"
        if ref.fmt.name == "codex" and ref.kind == "main" and (legacy.is_file() or log_name is None):
            stem = source.stem                    # keep existing Codex log names working
        else:
            stem = ref.source_key
```

In `MirrorSet.sync`, dedupe aliases and write the index:

```python
    def sync(self, sessions) -> list[Path]:
        refs = [as_ref(s[2]) if isinstance(s, tuple) else s for s in sessions]
        created, seen_inodes = [], set()
        for ref in refs:
            try:
                st = ref.path.stat()
            except OSError as exc:
                self.errors[ref.path] = f"{exc.strerror or exc}"
                continue
            if (st.st_dev, st.st_ino) in seen_inodes:
                continue
            seen_inodes.add((st.st_dev, st.st_ino))
            ...  # existing per-source TextLog create + sync, wrapped as in Task 5
        self._write_index(refs)
        if refs:
            point_latest(self.log_dir, self.logs[refs[0].path].path)
        return created

    def _write_index(self, refs) -> None:
        index = {ref.source_key: self.logs[ref.path].path.name for ref in refs if ref.path in self.logs}
        with contextlib.suppress(OSError):
            (self.log_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS, including the existing mirror/latest-symlink test.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "fix: give each mirrored source a collision-free log identity

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Format detection with an unknown result

**Files:**
- Modify: `codex-human-transcript` — new detection helpers near the discovery block; `build_parser()` (~line 1447)
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `CODEX` (Task 6); `CLAUDE` is referenced by name and lands in Task 10 — until then `detect_format` returns the string `"claude"` via a module-level lookup `FORMATS: dict[str, Format]`.
- Produces:
  - `detect_format(path: Path) -> Format | None`
  - `detect_root_format(root: Path) -> Format | None`
  - `--format {codex,claude}` CLI flag, applying to `--root` and `--file`.

- [ ] **Step 1: Write the failing tests**

```python
def test_detect_format_identifies_codex_and_claude(tmp_path):
    codex_file = write_rollout(tmp_path / "rollout-d1.jsonl",
                               [meta(), rec("agent_message", top="event_msg", message="hi")])
    claude_file = tmp_path / "projects" / "slug" / "11111111-2222-3333-4444-555555555555.jsonl"
    claude_file.parent.mkdir(parents=True)
    claude_file.write_text(json.dumps({
        "type": "assistant", "sessionId": "s1", "timestamp": "2026-09-17T06:00:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}}) + "\n",
        encoding="utf-8")
    assert cht.detect_format(codex_file) is cht.CODEX
    assert cht.detect_format(claude_file).name == "claude"


@pytest.mark.parametrize("body", ["", "\n", '{"type":"attachment"}\n', '{"timestamp": "2026'])
def test_detect_format_returns_none_for_undecidable_files(tmp_path, body):
    path = tmp_path / "unknown.jsonl"
    path.write_text(body, encoding="utf-8")
    assert cht.detect_format(path) is None


def test_detect_format_resolves_after_the_file_grows(tmp_path):
    path = tmp_path / "later.jsonl"
    path.write_text("", encoding="utf-8")
    assert cht.detect_format(path) is None
    path.write_text(json.dumps(rec("agent_message", top="event_msg", message="now")) + "\n",
                    encoding="utf-8")
    assert cht.detect_format(path) is cht.CODEX
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "detect_format"`
Expected: FAIL — `module 'cht' has no attribute 'detect_format'`.

- [ ] **Step 3: Implement positive-signature detection**

```python
SNIFF_BYTES = 262144
SNIFF_RECORDS = 50
CLAUDE_BOOKKEEPING = {"attachment", "file-history-snapshot", "file-history-delta", "mode",
                      "permission-mode", "last-prompt", "ai-title", "queue-operation", "atis-latch"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _path_signature(path: Path) -> str | None:
    parts = path.parts
    if path.name.startswith("rollout-") and path.suffix == ".jsonl":
        return "codex"
    if path.parent.name == "subagents" and path.name.startswith("agent-"):
        return "claude"
    if UUID_RE.match(path.stem) and "projects" in parts:
        return "claude"
    return None


def detect_format(path: Path) -> Format | None:
    """Positive signatures only: None means 'not yet decidable', never 'assume Codex'."""
    signature = _path_signature(path)
    if signature:
        return FORMATS[signature]
    try:
        with path.open("rb") as handle:
            blob = handle.read(SNIFF_BYTES)
    except OSError:
        return None
    for line in blob.decode("utf-8", "replace").split("\n")[:SNIFF_RECORDS]:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("payload"), dict) or record.get("type") == "session_meta":
            return CODEX
        if record.get("sessionId") and (isinstance(record.get("message"), dict)
                                        or record.get("type") in CLAUDE_BOOKKEEPING):
            return FORMATS["claude"]
    return None


def detect_root_format(root: Path) -> Format | None:
    if any((root / name).is_dir() for name in ("projects",)) or root.name == "projects":
        return FORMATS["claude"]
    if _day_dirs(root) or any(root.rglob("rollout-*.jsonl")):
        return CODEX
    for candidate in list(root.rglob("*.jsonl"))[:20]:
        found = detect_format(candidate)
        if found:
            return found
    return None
```

Add the CLI flag in `build_parser()`:

```python
    parser.add_argument("--format", choices=["codex", "claude"], default=None,
                        help="Force the transcript format for --root/--file instead of detecting it")
```

In `main()`, an ambiguous `--root` with no `--format` is a clear error:

```python
    if args.root is not None and args.format is None and detect_root_format(args.root) is None:
        print(f"Cannot tell which agent wrote transcripts under {args.root}; "
              f"pass --format codex|claude", file=sys.stderr)
        return 1
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS. (`FORMATS["claude"]` is registered in Task 10; until then register a placeholder `Format("claude", "CL", ...)` whose callables raise `NotImplementedError` — no test calls them.)

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: detect transcript format by positive signature with an unknown result

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

# Stage 3 — Claude discovery, state, rendering

## Task 9: Claude roots and discovery

**Files:**
- Modify: `codex-human-transcript` — `candidate_roots()` (~line 562), new Claude discovery section
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `SessionRef`, `source_key`, `RECENT_DAYS`.
- Produces:
  - `claude_candidate_roots(home=None, env=None, mnt=Path("/mnt"), home_parent=None) -> list[Path]`
  - `claude_discover(root: Path) -> list[SessionRef]`
  - `RootSel(NamedTuple): fmt_name: str; path: Path; enabled: bool`
  - `discovery_snapshot(roots: list[RootSel]) -> list[SessionRef]` — the single shared scan: mains sorted newest-first and capped at `MAIN_LIMIT`, then subagents capped at `SUBAGENT_LIMIT`.

- [ ] **Step 1: Write the failing tests**

Add helpers next to the existing ones:

```python
def claude_rec(kind: str, ts: str = "2026-09-17T06:00:00.000Z", session: str = "s1", **fields):
    return {"type": kind, "sessionId": session, "timestamp": ts, **fields}


def assistant_text(text: str, **fields):
    return claude_rec("assistant", message={"role": "assistant", "content": [{"type": "text", "text": text}],
                                            "id": "msg_1", "stop_reason": None}, **fields)


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path
```

Tests:

```python
def test_claude_discover_finds_mains_and_subagents(tmp_path):
    root = tmp_path / "projects"
    session = "11111111-2222-3333-4444-555555555555"
    write_jsonl(root / "-home-user-repo" / f"{session}.jsonl",
                [claude_rec("user", message={"role": "user", "content": "hello"}, cwd="/home/user/repo")])
    agent = write_jsonl(root / "-home-user-repo" / session / "subagents" / "agent-abc123.jsonl",
                        [assistant_text("sub speaking")])
    (agent.parent / "agent-abc123.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "Task 2: remove gate tests"}),
        encoding="utf-8")
    refs = cht.claude_discover(root)
    mains = [r for r in refs if r.kind == "main"]
    subs = [r for r in refs if r.kind == "subagent"]
    assert len(mains) == 1 and mains[0].cwd == "/home/user/repo"
    assert len(subs) == 1
    assert subs[0].role == "general-purpose"
    assert "remove gate tests" in subs[0].name
    assert subs[0].parent_id == session and subs[0].agent_id == "agent-abc123"


def test_claude_discover_lists_a_recent_subagent_under_an_old_parent(tmp_path):
    root = tmp_path / "projects"
    session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    old_main = write_jsonl(root / "slug" / f"{session}.jsonl", [assistant_text("old")])
    agent = write_jsonl(root / "slug" / session / "subagents" / "agent-new.jsonl", [assistant_text("new")])
    stale = time.time() - 40 * 86400
    os.utime(old_main, (stale, stale))
    refs = cht.claude_discover(root)
    assert [r.agent_id for r in refs if r.kind == "subagent"] == ["agent-new"]


def test_discovery_snapshot_merges_formats_newest_first(tmp_path):
    codex_root = tmp_path / "sessions"
    claude_root = tmp_path / "projects"
    cx = write_rollout(codex_root / "2026/09/17/rollout-2026-09-17T09-00-00-aaaa.jsonl",
                       [meta(), rec("agent_message", top="event_msg", message="cx")])
    cl = write_jsonl(claude_root / "slug" / "11111111-2222-3333-4444-555555555555.jsonl",
                     [assistant_text("cl")])
    now = time.time()
    os.utime(cx, (now - 50, now - 50))
    os.utime(cl, (now, now))
    snapshot = cht.discovery_snapshot([cht.RootSel("codex", codex_root, True),
                                       cht.RootSel("claude", claude_root, True)])
    assert [r.fmt.tag for r in snapshot] == ["CL", "CX"]


def test_claude_discover_opens_no_unselected_file(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    session = "11111111-2222-3333-4444-555555555555"
    write_jsonl(root / "slug" / f"{session}.jsonl", [assistant_text("current")])
    for i in range(30):
        old = write_jsonl(root / f"old{i}" / f"2222{i:04d}-2222-3333-4444-555555555555.jsonl",
                          [assistant_text("old")])
        stale = time.time() - 60 * 86400
        os.utime(old, (stale, stale))
    opened = []
    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (opened.append(self), real_open(self, *a, **k))[1])
    cht.claude_discover(root)
    assert all("old" not in str(p) for p in opened), f"stale files were opened: {opened}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "claude_discover or discovery_snapshot"`
Expected: FAIL — `module 'cht' has no attribute 'claude_discover'`.

- [ ] **Step 3: Implement Claude discovery**

```python
class RootSel(NamedTuple):
    fmt_name: str
    path: Path
    enabled: bool


def claude_candidate_roots(home: Path | None = None, env: Any = None, mnt: Path = Path("/mnt"),
                           home_parent: Path | None = None) -> list[Path]:
    home = home or Path.home()
    env = os.environ if env is None else env
    found: list[Path] = []
    if env.get("CLAUDE_CONFIG_DIR"):
        found.append(Path(env["CLAUDE_CONFIG_DIR"]) / "projects")
    found.append(home / ".claude" / "projects")
    for drive in _children(mnt):
        for user in _children(drive / "Users"):
            found.append(user / ".claude" / "projects")
    for parent in dict.fromkeys([home_parent or home.parent, Path("/home"), Path("/Users")]):
        for user in _children(parent):
            found.append(user / ".claude" / "projects")
    return list(dict.fromkeys(p for p in found if p.is_dir()))


def _recent(mtime: float, days: int = RECENT_DAYS) -> bool:
    return (time.time() - mtime) <= days * 86400


def _sidecar(path: Path) -> dict[str, Any]:
    side = path.with_suffix(".meta.json")
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claude_discover(root: Path) -> list[SessionRef]:
    """Mains and subagents under <root>/<project-slug>/; identity comes from the path, not contents."""
    refs: list[SessionRef] = []
    try:
        slugs = [entry for entry in os.scandir(root) if entry.is_dir()]
    except OSError:
        return []
    for slug in slugs:
        try:
            entries = list(os.scandir(slug.path))
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.name.endswith(".jsonl") and UUID_RE.match(Path(entry.name).stem):
                if not _recent(entry.stat().st_mtime):
                    continue
                sid = Path(entry.name).stem
                refs.append(SessionRef(
                    fmt=FORMATS["claude"], path=Path(entry.path), kind="main", session_id=sid,
                    parent_id=sid, agent_id=None, name=Path(slug.name).name[-20:] or sid[:8],
                    role="session", cwd=None, mtime=entry.stat().st_mtime,
                    source_key=source_key("claude", root, sid)))
            elif entry.is_dir() and UUID_RE.match(entry.name):
                refs.extend(_claude_subagents(root, Path(entry.path), entry.name))
    return refs


def _claude_subagents(root: Path, session_dir: Path, parent_id: str) -> list[SessionRef]:
    out: list[SessionRef] = []
    try:
        entries = list(os.scandir(session_dir / "subagents"))
    except OSError:
        return out
    for entry in entries:
        if not (entry.is_file() and entry.name.startswith("agent-") and entry.name.endswith(".jsonl")):
            continue
        mtime = entry.stat().st_mtime
        if not _recent(mtime):                 # eligibility is the agent's own age, not its parent's
            continue
        agent_id = Path(entry.name).stem
        side = _sidecar(Path(entry.path))
        name = str(side.get("description") or agent_id)[:24]
        role = str(side.get("agentType") or "subagent")
        out.append(SessionRef(FORMATS["claude"], Path(entry.path), "subagent", None, parent_id,
                              agent_id, name, role, None, mtime,
                              source_key("claude", root, parent_id, agent_id)))
    return out


def discovery_snapshot(roots: list[RootSel]) -> list[SessionRef]:
    """The single shared scan behind the dashboard, follow(), mirrors, and the footer."""
    refs: list[SessionRef] = []
    for sel in roots:
        if not sel.enabled:
            continue
        fmt = FORMATS.get(sel.fmt_name)
        if fmt is None:
            continue
        with contextlib.suppress(OSError):
            refs.extend(fmt.discover(sel.path))
    mains = sorted((r for r in refs if r.kind == "main"), key=lambda r: r.mtime, reverse=True)
    subs = sorted((r for r in refs if r.kind == "subagent"), key=lambda r: r.mtime, reverse=True)
    return mains[:MAIN_LIMIT] + subs[:SUBAGENT_LIMIT]
```

Register the Claude format placeholder from Task 8 with the real `discover`; `render`/`advance`/`scan_state` land in Tasks 10-12.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: discover Claude projects, sessions, and subagents

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Claude metadata and cache invalidation

**Files:**
- Modify: `codex-human-transcript` — metadata cache section (~line 421-438), `claude_discover`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `claude_discover` (Task 9).
- Produces:
  - `claude_meta(path: Path) -> dict[str, Any]` with keys `session_id`, `cwd`, `gitBranch`, `version` — each `None` until seen; cached by `(path, st_size, st_mtime)`; an entry with any `None` is re-read when the file grows.
  - `FORMATS["claude"].meta` bound to it; `claude_discover` fills `SessionRef.cwd` from it for mains.

- [ ] **Step 1: Write the failing tests**

```python
def test_claude_meta_collects_fields_independently(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        claude_rec("attachment", attachment={"type": "hook_success"}),            # sessionId, no cwd
        claude_rec("user", message={"role": "user", "content": "hi"}, cwd="/repo", gitBranch="main"),
    ])
    info = cht.claude_meta(path)
    assert info["session_id"] == "s1" and info["cwd"] == "/repo" and info["gitBranch"] == "main"


def test_claude_meta_refreshes_when_missing_fields_arrive_later(tmp_path):
    path = write_jsonl(tmp_path / "s2.jsonl", [claude_rec("attachment", attachment={"type": "x"})])
    assert cht.claude_meta(path)["cwd"] is None
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(claude_rec("user", message={"role": "user", "content": "hi"},
                                           cwd="/late")) + "\n")
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert cht.claude_meta(path)["cwd"] == "/late"


def test_late_sidecar_is_picked_up_on_the_next_discovery(tmp_path):
    root = tmp_path / "projects"
    session = "11111111-2222-3333-4444-555555555555"
    agent = write_jsonl(root / "slug" / session / "subagents" / "agent-late.jsonl", [assistant_text("x")])
    assert cht.claude_discover(root)[0].role == "subagent"
    (agent.parent / "agent-late.meta.json").write_text(json.dumps({"agentType": "Explore",
                                                                  "description": "look around"}),
                                                       encoding="utf-8")
    refreshed = [r for r in cht.claude_discover(root) if r.kind == "subagent"][0]
    assert refreshed.role == "Explore" and "look around" in refreshed.name
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "claude_meta or late_sidecar"`
Expected: FAIL — `module 'cht' has no attribute 'claude_meta'`.

- [ ] **Step 3: Implement metadata collection**

```python
_CLAUDE_META: dict[Path, tuple[tuple[int, float], dict[str, Any]]] = {}
CLAUDE_META_FIELDS = ("cwd", "gitBranch", "version")


def claude_meta(path: Path) -> dict[str, Any]:
    """Collect each field independently; incomplete results are re-read when the file grows."""
    try:
        st = path.stat()
        stamp = (st.st_size, st.st_mtime)
    except OSError:
        return {"session_id": None, "cwd": None, "gitBranch": None, "version": None}
    hit = _CLAUDE_META.get(path)
    if hit and (hit[0] == stamp or all(v is not None for v in hit[1].values())):
        return hit[1]
    info: dict[str, Any] = {"session_id": None, "cwd": None, "gitBranch": None, "version": None}
    try:
        with path.open("rb") as handle:
            blob = handle.read(SNIFF_BYTES)
    except OSError:
        return info
    for line in blob.decode("utf-8", "replace").split("\n"):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if info["session_id"] is None and record.get("sessionId"):
            info["session_id"] = str(record["sessionId"])
        for field in CLAUDE_META_FIELDS:
            if info[field] is None and record.get(field):
                info[field] = str(record[field])
        if all(value is not None for value in info.values()):
            break
    _CLAUDE_META[path] = (stamp, info)
    return info
```

Bind it into the Claude `Format` (`meta=claude_meta`) and use it for a main's `cwd` in `claude_discover`. Sidecar reads already re-stat per discovery pass, so a late sidecar is picked up; keep `_sidecar()` uncached.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: collect Claude session metadata field-by-field with growth-aware caching

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Four-state session status for both formats

**Files:**
- Modify: `codex-human-transcript` — state section (~line 384-391, 455-478), `TranscriptCache`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `Format`, `SessionRef`, constants `STATE_*`.
- Produces:
  - `claude_advance(state: str, record: dict) -> str`
  - `scan_state(path: Path, fmt: Format) -> str` — record-aligned backward scan, `SCAN_CHUNK` steps to `MAX_SCAN_BYTES`, returning `STATE_UNKNOWN` when undecided.
  - `has_completed(path)` keeps its signature, now `scan_state(path, CODEX) == STATE_DONE`.
  - `TranscriptCache.state` may now be `"unknown"`; `GLYPHS` gains `"unknown": ("◌", "90")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_claude_end_turn_is_done_until_a_hook_continues_the_turn(tmp_path):
    path = write_jsonl(tmp_path / "s3.jsonl", [
        claude_rec("user", message={"role": "user", "content": "go"}),
        claude_rec("assistant", message={"role": "assistant", "stop_reason": "end_turn",
                                         "content": [{"type": "text", "text": "done"}]}),
    ])
    cache = cht.TranscriptCache(cht.SessionRef(cht.FORMATS["claude"], path, "main", "s3", "s3", None,
                                               "s3", "session", None, 0.0, "k1"))
    cache.update()
    assert cache.state == "done"
    with path.open("a", encoding="utf-8") as handle:                     # Stop hook forced more work
        handle.write(json.dumps(claude_rec("assistant", message={
            "role": "assistant", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]})) + "\n")
    cache.update()
    assert cache.state == "working"


def test_claude_api_error_is_error_then_working_again(tmp_path):
    path = write_jsonl(tmp_path / "s4.jsonl", [
        claude_rec("assistant", isApiErrorMessage=True,
                   message={"role": "assistant", "content": [{"type": "text", "text": "API Error"}]}),
    ])
    assert cht.scan_state(path, cht.FORMATS["claude"]) == "error"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(claude_rec("user", message={"role": "user", "content": "retry"})) + "\n")
    assert cht.scan_state(path, cht.FORMATS["claude"]) == "working"


def test_scan_state_is_unknown_when_no_marker_is_in_range(tmp_path):
    path = tmp_path / "s5.jsonl"
    filler = json.dumps(claude_rec("attachment", attachment={"type": "pad", "blob": "q" * 4000})) + "\n"
    path.write_text(filler * 400, encoding="utf-8")                      # > MAX_SCAN_BYTES of bookkeeping
    assert cht.scan_state(path, cht.FORMATS["claude"]) == "unknown"


def test_scan_state_ignores_marker_text_nested_in_tool_data(tmp_path):
    path = write_jsonl(tmp_path / "s6.jsonl", [
        claude_rec("user", message={"role": "user", "content": "go"}),
        claude_rec("user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": '{"type":"turn_duration","note":"this is data, not a marker"}'}]}),
    ])
    assert cht.scan_state(path, cht.FORMATS["claude"]) == "working"


def test_advance_and_scan_state_agree_on_the_same_fixture(tmp_path):
    path = write_jsonl(tmp_path / "s7.jsonl", [
        claude_rec("user", message={"role": "user", "content": "go"}),
        claude_rec("system", subtype="turn_duration", durationMs=1200),
    ])
    live = "unknown"
    for line in path.read_text(encoding="utf-8").splitlines():
        live = cht.claude_advance(live, json.loads(line))
    assert live == cht.scan_state(path, cht.FORMATS["claude"]) == "done"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "end_turn or api_error or scan_state or advance_and_scan"`
Expected: FAIL — `module 'cht' has no attribute 'claude_advance'`.

- [ ] **Step 3: Implement state classification and the aligned scan**

```python
SCAN_CHUNK = 65536
MAX_SCAN_BYTES = 1048576
CLAUDE_SKIP_STATE = CLAUDE_BOOKKEEPING | {"system"}


def _claude_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    content = (record.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def claude_advance(state: str, record: dict[str, Any]) -> str:
    """Conservative observation of the transcript, never a lifecycle protocol."""
    kind = record.get("type")
    if kind == "system":
        return STATE_DONE if record.get("subtype") == "turn_duration" else state
    if kind in CLAUDE_BOOKKEEPING or record.get("__oversized__"):
        return state
    if record.get("isApiErrorMessage"):
        return STATE_ERROR
    if kind == "user":
        if record.get("isMeta") or record.get("isCompactSummary"):
            return state
        return STATE_WORKING
    if kind == "assistant":
        message = record.get("message") or {}
        if any(b.get("type") == "tool_use" for b in _claude_blocks(record)):
            return STATE_WORKING
        if message.get("stop_reason") == "end_turn":
            return STATE_DONE
        return STATE_WORKING
    return state


def scan_state(path: Path, fmt: Format) -> str:
    """Backward, record-aligned scan: parse records, never substring-match markers."""
    try:
        size = path.stat().st_size
    except OSError:
        return STATE_UNKNOWN
    window = 0
    while window < min(size, MAX_SCAN_BYTES):
        window = min(size, MAX_SCAN_BYTES, window + SCAN_CHUNK)
        try:
            with path.open("rb") as handle:
                handle.seek(size - window)
                if window < size:
                    handle.readline()                 # align to the next record boundary
                blob = handle.read()
        except OSError:
            return STATE_UNKNOWN
        state = STATE_UNKNOWN
        for record in records(blob.decode("utf-8", "replace").split("\n")):
            state = fmt.advance(state, record)
        if state != STATE_UNKNOWN:
            return state
    return STATE_UNKNOWN


def has_completed(path: Path) -> bool:
    return scan_state(path, CODEX) == STATE_DONE
```

Give Codex the same contract so both formats agree:

```python
def codex_advance(state: str, record: dict[str, Any]) -> str:
    kind = payload_kind(record)
    if kind == "task_started":
        return STATE_WORKING
    if kind == "task_complete":
        return STATE_DONE
    if kind in {"error", "task_failed"}:
        return STATE_ERROR
    return state
```

Replace Task 6's provisional `codex_scan_state` with the shared scan so both formats use one
classifier, and rebind it on the `CODEX` adapter:

```python
def codex_scan_state(path: Path) -> str:
    return scan_state(path, CODEX)
```

`TranscriptCache` starts at `STATE_UNKNOWN`, uses `self.ref.fmt.advance`, resets to `STATE_UNKNOWN` in `reset()` and on truncation, and returns `True` from `update()` when the state changed even if no lines were added. Add `GLYPHS["unknown"] = ("◌", "90")` and `"error": ("✖", "1;31")` stays as is.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS, including the existing `has_completed` and cache-state tests.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: four-state session status with a record-aligned tail scan

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Claude rendering

**Files:**
- Modify: `codex-human-transcript` — new renderer section after the Codex renderers
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `block()`, `prose()`, `clean()`, `redact()`, `kv_lines()`, `plan_lines()`, `STATE_*`.
- Produces:
  - `claude_render(record: dict, opts: dict[str, Any]) -> str | None`, where `opts` carries `{"thinking": bool}`.
  - `FORMATS["claude"]` fully populated (`discover`, `meta`, `render`, `advance`, `scan_state`, `dedupe=False`).

- [ ] **Step 1: Write the failing tests**

```python
def claude_plain(record, **opts):
    text = cht.claude_render(record, {"thinking": True, **opts})
    assert text is not None
    return cht.strip_ansi(text)


def test_claude_render_user_and_assistant_text():
    assert "YOU" in claude_plain(claude_rec("user", message={"role": "user", "content": "hello there"}))
    assert "hello there" in claude_plain(claude_rec("user", message={"role": "user", "content": "hello there"}))
    out = claude_plain(assistant_text("**bold** answer"))
    assert "CLAUDE" in out and "bold" in out


def test_claude_render_thinking_respects_the_toggle():
    record = claude_rec("assistant", message={"role": "assistant", "content": [
        {"type": "thinking", "thinking": "let me reason"}]})
    assert "THINKING" in claude_plain(record)
    assert cht.claude_render(record, {"thinking": False}) is None


def test_claude_render_tool_uses():
    bash = claude_rec("assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "git status", "description": "Show status"}}]})
    assert "EXEC" in claude_plain(bash) and "$ git status" in claude_plain(bash)
    edit = claude_rec("assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "t2", "name": "Edit",
         "input": {"file_path": "/repo/a.py", "old_string": "foo", "new_string": "bar"}}]})
    out = claude_plain(edit)
    assert "Edit" in out and "/repo/a.py" in out and "-foo" in out and "+bar" in out
    agent = claude_rec("assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "t3", "name": "Agent",
         "input": {"subagent_type": "Explore", "description": "find callers", "prompt": "search"}}]})
    assert "AGENT" in claude_plain(agent) and "Explore" in claude_plain(agent)
    mcp = claude_rec("assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "t4", "name": "mcp__gmail__search_threads", "input": {"q": "x"}}]})
    assert "MCP" in claude_plain(mcp) and "gmail/search_threads" in claude_plain(mcp)
    todo = claude_rec("assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "t5", "name": "TodoWrite",
         "input": {"todos": [{"content": "step one", "status": "completed"}]}}]})
    assert "PLAN" in claude_plain(todo) and "✔" in cht.claude_render(todo, {"thinking": True})


def test_claude_render_tool_results_and_errors():
    ok = claude_rec("user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "total 4\n"}]})
    assert "TOOL" in claude_plain(ok) and "total 4" in claude_plain(ok)
    bad = claude_rec("user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": "boom", "is_error": True}]})
    assert "✘" in cht.claude_render(bad, {"thinking": True})


def test_claude_render_skips_noise():
    for record in (claude_rec("attachment", attachment={"type": "hook_success"}),
                   claude_rec("file-history-snapshot"),
                   claude_rec("system", subtype="stop_hook_summary"),
                   claude_rec("user", isMeta=True, message={"role": "user", "content": "<meta>"})):
        assert cht.claude_render(record, {"thinking": True}) is None


def test_claude_render_compact_summary_and_slash_command():
    summary = claude_rec("user", isCompactSummary=True,
                         message={"role": "user", "content": "summary of prior work"})
    assert "COMPACTED" in claude_plain(summary)
    cmd = claude_rec("user", message={"role": "user",
                                      "content": "<command-name>/model</command-name>"})
    assert "/model" in claude_plain(cmd)


def test_claude_render_oversized_placeholder():
    assert "oversized" in claude_plain({"__oversized__": 5_000_000})


def test_claude_tool_input_secrets_are_redacted():
    record = claude_rec("assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "t9", "name": "Deploy",
         "input": {"config": {"api_key": "sk-live-555"}, "target": "prod"}}]})
    out = claude_plain(record)
    assert "sk-live-555" not in out and "prod" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "claude_render or claude_tool"`
Expected: FAIL — `module 'cht' has no attribute 'claude_render'`.

- [ ] **Step 3: Implement the Claude renderers**

```python
CLAUDE_FILE_TOOLS = {"Read", "Write", "Edit", "Grep", "Glob", "NotebookEdit"}
CLAUDE_AGENT_TOOLS = {"Agent", "Task", "SendMessage", "ListAgents"}
CLAUDE_PLAN_TOOLS = {"TodoWrite", "ExitPlanMode"}
CLAUDE_SKIP_TYPES = CLAUDE_BOOKKEEPING
CLAUDE_SKIP_SUBTYPES = {"stop_hook_summary", "informational", "away_summary",
                        "local_command", "permission_retry"}
COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>(?:.*?<command-args>(.*?)</command-args>)?",
                        re.S)
STDOUT_RE = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)


def claude_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [str(b.get("text", "")) for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def claude_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False) if content else ""
    chunks = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image":
            chunks.append("⟦image⟧")
        elif item.get("text"):
            chunks.append(str(item["text"]))
    return "\n".join(chunks)


def render_claude_tool_use(stamp: str, blk: dict[str, Any]) -> str | None:
    name = str(blk.get("name", "tool"))
    args = redact(blk.get("input") if isinstance(blk.get("input"), dict) else {})
    if name == "Bash":
        body = "$ " + clean(str(args.get("command", "")))
        if args.get("description"):
            body += "\n" + STYLE.paint("90", str(args["description"]))
        return block(stamp, "33", "⚙ EXEC ⇢", body)
    if name in CLAUDE_PLAN_TOOLS:
        steps = [{"step": t.get("content", ""), "status": t.get("status", "pending")}
                 for t in args.get("todos") or [] if isinstance(t, dict)]
        return block(stamp, "34", "📋 PLAN", plan_lines({"plan": steps}) if steps else kv_lines(args))
    if name in CLAUDE_AGENT_TOOLS:
        body = "\n".join(filter(None, [
            clean(str(args.get("description", "")), 200),
            clean(str(args.get("prompt", args.get("message", ""))), 400)]))
        return block(stamp, "95", f"👥 AGENT ⇢ {args.get('subagent_type', name)}", body)
    if name.startswith("mcp__"):
        bits = name.split("__")
        label = f"{bits[1]}/{'__'.join(bits[2:])}" if len(bits) > 2 else name
        return block(stamp, "33", f"⚙ MCP ⇢ {label}", kv_lines(args))
    if name in CLAUDE_FILE_TOOLS:
        target = args.get("file_path") or args.get("path") or args.get("pattern") or ""
        body = clean(str(target), 300)
        if name == "Edit":
            body += ("\n" + STYLE.paint("31", "-" + clean(str(args.get("old_string", "")), 200))
                     + "\n" + STYLE.paint("32", "+" + clean(str(args.get("new_string", "")), 200)))
        return block(stamp, "33", f"⚙ TOOL ⇢ {name}", body)
    return block(stamp, "33", f"⚙ TOOL ⇢ {name}", kv_lines(args) if args else "")


def claude_render(record: dict[str, Any], opts: dict[str, Any]) -> str | None:
    if record.get("__oversized__"):
        return block("", "90", "⟦oversized⟧", f"oversized record: {record['__oversized__']} bytes, skipped")
    kind = record.get("type")
    stamp = str(record.get("timestamp", ""))[:19].replace("T", " ")
    if kind in CLAUDE_SKIP_TYPES:
        return None
    if kind == "system":
        subtype = record.get("subtype")
        if subtype == "compact_boundary":
            return block(stamp, "90", "⟲ COMPACTED", "context compacted")
        if subtype == "turn_duration":
            ms = record.get("durationMs")
            return block(stamp, "32", "✔ DONE", STYLE.paint("90", f"turn took {int(ms) / 1000:.1f}s")
                         if isinstance(ms, (int, float)) else "turn complete")
        return None
    if record.get("isApiErrorMessage"):
        return block(stamp, "31", "✘ ERROR", clean(claude_text((record.get("message") or {}).get("content"))))
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    if kind == "user":
        if record.get("isMeta"):
            return None
        if record.get("isCompactSummary"):
            return block(stamp, "90", "⟲ COMPACTED", prose(clean(claude_text(message.get("content")))))
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            first = results[0]
            body = clean(claude_result_text(first.get("content")))
            return block(stamp, "31" if first.get("is_error") else "33",
                         "✘ TOOL ⇠" if first.get("is_error") else "⚙ TOOL ⇠", body)
        text = claude_text(content)
        command = COMMAND_RE.search(text)
        if command:
            args = (command.group(2) or "").strip()
            return block(stamp, "90", "⌘ COMMAND", clean(f"{command.group(1)} {args}".strip(), 200))
        stdout = STDOUT_RE.search(text)
        if stdout:
            return block(stamp, "90", "⌘ OUTPUT", clean(stdout.group(1).strip(), 300))
        return block(stamp, "35", "❯ YOU", prose(clean(text))) if text.strip() else None
    if kind == "assistant":
        out = []
        for blk in _claude_blocks(record):
            btype = blk.get("type")
            if btype == "text" and str(blk.get("text", "")).strip():
                out.append(block(stamp, "33", "✦ CLAUDE", prose(clean(str(blk["text"])))))
            elif btype == "thinking" and opts.get("thinking", True):
                out.append(block(stamp, "90", "💭 THINKING", clean(str(blk.get("thinking", "")))))
            elif btype == "tool_use":
                rendered = render_claude_tool_use(stamp, blk)
                if rendered:
                    out.append(rendered)
        return "".join(out) or None
    return None
```

Complete the registration:

```python
CLAUDE = Format("claude", "CL", claude_discover, claude_meta, claude_render,
                claude_advance, lambda p: scan_state(p, CLAUDE), dedupe=False)
FORMATS = {"codex": CODEX, "claude": CLAUDE}
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: render Claude messages, thinking, tool calls, and results

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: History fidelity on real resumed and compacted sessions

**Files:**
- Create: `tests/fixtures/claude/resumed.jsonl`, `tests/fixtures/claude/compacted.jsonl`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `claude_render`, `TranscriptCache`, `SessionRef`.
- Produces: fixtures proving the spec's write-order semantics (§3.4). No production code changes are expected; if a fixture fails, fix `claude_render`/`claude_advance`.

- [ ] **Step 1: Build the sanitized fixtures**

Run this to produce fixtures from a real session, replacing prose with placeholders and dropping every attachment record:

```bash
cd ~/github/codex-human-transcript
mkdir -p tests/fixtures/claude
python3 - <<'PY'
import json, glob, os, pathlib, re
out = pathlib.Path("tests/fixtures/claude")
def sanitize(rec, n):
    msg = rec.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = f"text-{n}"
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text": b["text"] = f"text-{n}"
                if b.get("type") == "thinking": b["thinking"] = f"thinking-{n}"
                if b.get("type") == "tool_result": b["content"] = f"result-{n}"
                if b.get("type") == "tool_use": b["input"] = {"command": f"cmd-{n}"}
    for key in ("cwd", "gitBranch"):
        if key in rec: rec[key] = "/fixture"
    return rec
def pick(pred, dest, limit=60):
    for path in sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")),
                       key=os.path.getmtime, reverse=True):
        lines = [json.loads(l) for l in open(path) if l.strip()]
        if not pred(lines):
            continue
        keep = [sanitize(r, i) for i, r in enumerate(lines) if r.get("type") != "attachment"][:limit]
        (out / dest).write_text("".join(json.dumps(r) + "\n" for r in keep), encoding="utf-8")
        print("wrote", dest, "from", path)
        return
    raise SystemExit(f"no source session found for {dest}")
pick(lambda ls: any(r.get("isCompactSummary") or (r.get("type") == "system" and r.get("subtype") == "compact_boundary") for r in ls), "compacted.jsonl")
pick(lambda ls: len({r.get("sessionId") for r in ls if r.get("sessionId")}) > 1
               or any(r.get("parentUuid") is None for r in ls[1:]), "resumed.jsonl")
PY
```

Expected: both files written. If `resumed.jsonl` finds no source, create it by hand: copy `compacted.jsonl` and duplicate its first 5 records at the end so replayed records and reused `message.id` values are present.

- [ ] **Step 2: Write the failing tests**

```python
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude"


def claude_ref(path: Path):
    return cht.SessionRef(cht.CLAUDE, path, "main", "fix", "fix", None, "fix", "session", None, 0.0, "kf")


def test_compacted_fixture_renders_summary_as_compacted_not_you():
    path = FIXTURES / "compacted.jsonl"
    cache = cht.TranscriptCache(claude_ref(path))
    cache.update()
    body = "\n".join(cht.strip_ansi(line) for line in cache.lines)
    assert "COMPACTED" in body
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    summaries = [r for r in records if r.get("isCompactSummary")]
    if summaries:
        assert cht.claude_advance("working", summaries[0]) == "working"


def test_resumed_fixture_keeps_write_order_and_repeats_replayed_records():
    path = FIXTURES / "resumed.jsonl"
    cache = cht.TranscriptCache(claude_ref(path))
    cache.update()
    body = "\n".join(cht.strip_ansi(line) for line in cache.lines)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rendered = [r for r in records if cht.claude_render(r, {"thinking": True})]
    assert body.strip(), "the fixture must render something"
    assert len(rendered) >= 2
    ids = [(r.get("message") or {}).get("id") for r in records if isinstance(r.get("message"), dict)]
    repeated = [i for i in set(ids) if i and ids.count(i) > 1]
    for dup in repeated:                      # shared message ids are separate blocks, never deduped
        texts = [cht.strip_ansi(cht.claude_render(r, {"thinking": True}) or "")
                 for r in records if (r.get("message") or {}).get("id") == dup]
        assert len([t for t in texts if t.strip()]) == len(texts)
```

- [ ] **Step 3: Run the tests**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "fixture"`
Expected: PASS. If the compacted fixture renders `YOU` for the summary, fix the `isCompactSummary` branch in `claude_render` and re-run.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add tests codex-human-transcript
git commit -m "test: pin Claude history semantics with sanitized resumed and compacted fixtures

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

# Stage 4 — config, picker, dashboard, entry points

## Task 14: Multi-root config and one resolver

**Files:**
- Modify: `codex-human-transcript` — config section (~line 594-617), `main()`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `RootSel`, `FORMATS`, `detect_root_format`.
- Produces:
  - `load_roots(config: dict) -> list[RootSel] | None` — `None` means "no selection stored" (use discovery); `[]` means an intentional empty selection.
  - `roots_payload(roots: list[RootSel]) -> dict` — `{"v": 2, "roots": [...]}`.
  - `resolve_roots(cli_root, cli_format, config, discovered) -> list[RootSel]`.
  - `resolve_root()` kept as a compatibility wrapper returning the first enabled path or `None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_load_roots_reads_legacy_single_root_and_new_list():
    legacy = cht.load_roots({"root": "/tmp/sessions"})
    assert legacy == [cht.RootSel("codex", Path("/tmp/sessions"), True)]
    modern = cht.load_roots({"v": 2, "roots": [{"format": "claude", "path": "/tmp/projects",
                                                "enabled": False}]})
    assert modern == [cht.RootSel("claude", Path("/tmp/projects"), False)]
    assert cht.load_roots({}) is None                      # nothing stored → use discovery
    assert cht.load_roots({"v": 2, "roots": []}) == []      # intentional empty selection


def test_roots_wins_over_legacy_key_when_both_present():
    loaded = cht.load_roots({"root": "/tmp/old", "v": 2,
                             "roots": [{"format": "codex", "path": "/tmp/new", "enabled": True}]})
    assert loaded == [cht.RootSel("codex", Path("/tmp/new"), True)]


def test_resolve_roots_cli_replaces_saved_selection(tmp_path):
    saved = {"v": 2, "roots": [{"format": "codex", "path": str(tmp_path / "saved"), "enabled": True}]}
    out = cht.resolve_roots(tmp_path / "cli", "claude", saved, [])
    assert out == [cht.RootSel("claude", tmp_path / "cli", True)]


def test_resolve_roots_falls_back_to_discovery_only_when_nothing_is_stored(tmp_path):
    discovered = [cht.RootSel("codex", tmp_path / "found", True)]
    assert cht.resolve_roots(None, None, {}, discovered) == discovered
    assert cht.resolve_roots(None, None, {"v": 2, "roots": []}, discovered) == []


def test_roots_payload_round_trips_through_config(tmp_path):
    path = tmp_path / "config.json"
    roots = [cht.RootSel("codex", tmp_path / "a", True), cht.RootSel("claude", tmp_path / "b", False)]
    cht.save_config(path, cht.roots_payload(roots))
    assert cht.load_roots(cht.load_config(path)) == roots
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "roots"`
Expected: FAIL — `module 'cht' has no attribute 'load_roots'`.

- [ ] **Step 3: Implement the resolver**

```python
CONFIG_VERSION = 2


def load_roots(config: dict[str, Any]) -> list[RootSel] | None:
    """None = nothing stored (use discovery); [] = an intentional empty selection."""
    raw = config.get("roots")
    if isinstance(raw, list):
        out = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            fmt_name = str(item.get("format", "codex"))
            if fmt_name not in FORMATS:
                continue
            out.append(RootSel(fmt_name, Path(str(item["path"])), bool(item.get("enabled", True))))
        return out
    saved = config.get("root")
    if isinstance(saved, str) and saved:
        return [RootSel("codex", Path(saved), True)]
    return None


def roots_payload(roots: list[RootSel]) -> dict[str, Any]:
    return {"v": CONFIG_VERSION,
            "roots": [{"format": r.fmt_name, "path": str(r.path), "enabled": r.enabled} for r in roots]}


def resolve_roots(cli_root: Path | None, cli_format: str | None, config: dict[str, Any],
                  discovered: list[RootSel]) -> list[RootSel]:
    if cli_root is not None:
        fmt = FORMATS[cli_format] if cli_format else detect_root_format(cli_root)
        return [RootSel((fmt or CODEX).name, cli_root, True)]
    stored = load_roots(config)
    if stored is not None:
        return stored
    return discovered
```

`--remember` now writes `roots_payload(resolved)`; `change_root()` writes the picker's full selection through the same helper. `resolve_root()` becomes:

```python
def resolve_root(cli_root: Path | None, config: dict[str, Any],
                 discovered: list[RootInfo]) -> Path | None:
    roots = resolve_roots(cli_root, None, config,
                          [RootSel("codex", info.path, True) for info in discovered])
    return next((r.path for r in roots if r.enabled), None)
```

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS, including the existing `resolve_root` precedence test.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: store and resolve multiple transcript roots through one resolver

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: Multi-select root picker

**Files:**
- Modify: `codex-human-transcript` — `discover_roots()`, `pick_root()` (~line 1056-1068), `print_roots()`, `--list-roots`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `RootSel`, `claude_candidate_roots`, `candidate_roots`, `roots_payload`.
- Produces:
  - `RootInfo` gains `fmt_name: str` as its first field: `RootInfo(fmt_name, path, count, newest)`.
  - `discover_all_roots() -> list[RootInfo]` — Codex + Claude candidates ranked by last activity.
  - `merge_root_choices(saved: list[RootSel], discovered: list[RootInfo]) -> list[RootSel]` — saved entries keep their `enabled` flag and their place; newly discovered roots are appended **disabled**.
  - `pick_roots(stdscr, current) -> list[RootSel] | None` replaces `pick_root`.

- [ ] **Step 1: Write the failing tests**

```python
def test_merge_root_choices_keeps_custom_roots_and_disables_new_ones(tmp_path):
    saved = [cht.RootSel("codex", tmp_path / "custom", True)]
    discovered = [cht.RootInfo("claude", tmp_path / "found", 3, time.time())]
    merged = cht.merge_root_choices(saved, discovered)
    assert merged[0] == cht.RootSel("codex", tmp_path / "custom", True)
    assert merged[1] == cht.RootSel("claude", tmp_path / "found", False)


def test_merge_root_choices_does_not_duplicate_a_saved_root(tmp_path):
    saved = [cht.RootSel("codex", tmp_path / "same", False)]
    discovered = [cht.RootInfo("codex", tmp_path / "same", 1, time.time())]
    merged = cht.merge_root_choices(saved, discovered)
    assert merged == [cht.RootSel("codex", tmp_path / "same", False)]


def test_discover_all_roots_ranks_both_formats(tmp_path, monkeypatch):
    codex_root = tmp_path / "sessions"
    claude_root = tmp_path / "projects"
    cx = write_rollout(codex_root / "2026/09/17/rollout-2026-09-17T09-00-00-aaaa.jsonl", [meta()])
    cl = write_jsonl(claude_root / "slug" / "11111111-2222-3333-4444-555555555555.jsonl",
                     [assistant_text("hi")])
    now = time.time()
    os.utime(cx, (now - 100, now - 100))
    os.utime(cl, (now, now))
    monkeypatch.setattr(cht, "candidate_roots", lambda *a, **k: [codex_root])
    monkeypatch.setattr(cht, "claude_candidate_roots", lambda *a, **k: [claude_root])
    infos = cht.discover_all_roots()
    assert [info.fmt_name for info in infos] == ["claude", "codex"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "merge_root or discover_all"`
Expected: FAIL — `module 'cht' has no attribute 'merge_root_choices'`.

- [ ] **Step 3: Implement the multi-select picker**

```python
class RootInfo(NamedTuple):
    fmt_name: str
    path: Path
    count: int
    newest: float


def discover_all_roots() -> list[RootInfo]:
    infos: list[RootInfo] = []
    for fmt_name, candidates in (("codex", candidate_roots()), ("claude", claude_candidate_roots())):
        fmt = FORMATS[fmt_name]
        for root in dict.fromkeys(candidates):
            with contextlib.suppress(OSError):
                refs = fmt.discover(root)
                if refs:
                    infos.append(RootInfo(fmt_name, root, len(refs), max(r.mtime for r in refs)))
    return sorted(infos, key=lambda info: info.newest, reverse=True)


def merge_root_choices(saved: list[RootSel], discovered: list[RootInfo]) -> list[RootSel]:
    out = list(saved)
    known = {(sel.fmt_name, sel.path.resolve() if sel.path.exists() else sel.path) for sel in saved}
    for info in discovered:
        key = (info.fmt_name, info.path.resolve() if info.path.exists() else info.path)
        if key not in known:
            out.append(RootSel(info.fmt_name, info.path, False))   # never auto-enable
    return out


def pick_roots(stdscr: Any, current: list[RootSel]) -> list[RootSel] | None:
    """Multi-select over saved ∪ discovered roots; space toggles, enter saves, esc cancels."""
    choices = merge_root_choices(current, discover_all_roots())
    selected = 0
    stdscr.timeout(-1)
    try:
        while True:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            safe_add(stdscr, 0, 0, " TRANSCRIPT ROOTS — space toggles · a adds a path · enter saves ".ljust(
                width - 1), width - 1, curses.A_REVERSE | curses.A_BOLD)
            for i, sel in enumerate(choices[: max(1, height - 4)]):
                mark = "✔" if sel.enabled else " "
                tag = FORMATS[sel.fmt_name].tag
                row = f"[{mark}] {tag}  {sel.path}"
                safe_add(stdscr, 2 + i, 0, row.ljust(width - 1), width - 1,
                         curses.A_REVERSE if i == selected else 0)
            key = stdscr.getch()
            if key in (27, ord("q")):
                return None
            if key in ENTER_KEYS:
                return choices
            if key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(choices) - 1, selected + 1)
            elif key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key == ord(" ") and choices:
                sel = choices[selected]
                choices[selected] = sel._replace(enabled=not sel.enabled)
            elif key == ord("a"):
                typed = read_line(stdscr, "root path: ")
                if typed:
                    path = Path(typed).expanduser()
                    fmt = detect_root_format(path)
                    if fmt is None:
                        answer = read_line(stdscr, "format (codex/claude): ")
                        fmt = FORMATS.get((answer or "").strip(), CODEX)
                    choices.append(RootSel(fmt.name, path, True))
    finally:
        stdscr.timeout(80)
```

In the dashboard, `r` calls `pick_roots`, saves `roots_payload(chosen)`, resets the snapshot, and shows any save error in `status_msg`. `print_roots()` prints the format tag per row; `--list-roots` uses `discover_all_roots()`.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS. Update the existing `test_discover_roots_ranks_by_newest_rollout_and_skips_empty` to the new `RootInfo` field order if it constructs one positionally.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: multi-select root picker across Codex and Claude roots

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 16: Thinking as a display-only preference

**Files:**
- Modify: `codex-human-transcript` — global render options, `TranscriptCache`, `TextLog`, key handling, `build_parser()`, `HELP`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `claude_render` opts.
- Produces:
  - `DISPLAY_OPTS: dict[str, Any]` — `{"thinking": True}`, mutated by `t` and `--no-thinking`.
  - `MIRROR_OPTS: dict[str, Any]` — `{"thinking": True}`, constant; mirrors always include thinking.
  - `TranscriptCache` renders with `DISPLAY_OPTS`; `TextLog` renders with `MIRROR_OPTS`.
  - `--no-thinking` help text states it does not affect `--mirror`.

- [ ] **Step 1: Write the failing tests**

```python
def test_mirror_always_includes_thinking_even_when_display_hides_it(tmp_path, monkeypatch):
    src = write_jsonl(tmp_path / "s8.jsonl", [
        claude_rec("assistant", message={"role": "assistant", "content": [
            {"type": "thinking", "thinking": "private reasoning"}]}),
        assistant_text("public answer"),
    ])
    monkeypatch.setitem(cht.DISPLAY_OPTS, "thinking", False)
    ref = cht.SessionRef(cht.CLAUDE, src, "main", "s8", "s8", None, "s8", "session", None, 0.0, "k8")
    log = cht.TextLog(tmp_path / "logs", ref)
    log.sync()
    text = log.path.read_text(encoding="utf-8")
    assert "private reasoning" in text and "public answer" in text
    cache = cht.TranscriptCache(ref)
    cache.update()
    body = "\n".join(cht.strip_ansi(line) for line in cache.lines)
    assert "private reasoning" not in body and "public answer" in body


def test_toggling_thinking_rerenders_the_cache_from_disk(tmp_path, monkeypatch):
    src = write_jsonl(tmp_path / "s9.jsonl", [
        claude_rec("assistant", message={"role": "assistant", "content": [
            {"type": "thinking", "thinking": "visible reasoning"}]})])
    ref = cht.SessionRef(cht.CLAUDE, src, "main", "s9", "s9", None, "s9", "session", None, 0.0, "k9")
    cache = cht.TranscriptCache(ref)
    cache.update()
    assert "visible reasoning" in "\n".join(cht.strip_ansi(x) for x in cache.lines)
    monkeypatch.setitem(cht.DISPLAY_OPTS, "thinking", False)
    cache.reset()
    cache.update()
    assert "visible reasoning" not in "\n".join(cht.strip_ansi(x) for x in cache.lines)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "thinking"`
Expected: FAIL — `module 'cht' has no attribute 'DISPLAY_OPTS'`.

- [ ] **Step 3: Implement the toggle**

```python
DISPLAY_OPTS: dict[str, Any] = {"thinking": True}     # TUI + stdout only
MIRROR_OPTS: dict[str, Any] = {"thinking": True}      # constant: logs are stable artifacts
```

`TranscriptCache.update()` renders with `self.ref.fmt.render(record, DISPLAY_OPTS)`; `TextLog.sync()` uses `self.ref.fmt.render(record, MIRROR_OPTS)` and passes `render_opts=MIRROR_OPTS` to its checkpoint.

Dashboard key handling, next to the existing `m` branch:

```python
            elif key == ord("t"):
                DISPLAY_OPTS["thinking"] = not DISPLAY_OPTS["thinking"]
                for c in caches.values():
                    c.reset()
                if sessions:
                    cache_for(sessions[selected]).update()
                status_msg = f"thinking {'on' if DISPLAY_OPTS['thinking'] else 'off'}"
```

CLI:

```python
    parser.add_argument("--no-thinking", action="store_true",
                        help="Hide Claude thinking blocks in the TUI and on stdout "
                             "(text logs always include them)")
```

In `main()`: `DISPLAY_OPTS["thinking"] = not args.no_thinking`. Update `HELP` to
`"tab/1-9 sess · j/k d/u g/G scroll · f follow · / n N find · w wrap · m md · t think · r roots · q quit"`.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: thinking visibility as a display-only preference

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 17: Dashboard status freshness and the merged list

**Files:**
- Modify: `codex-human-transcript` — `interactive_dashboard()` (~line 1097-1341)
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `discovery_snapshot`, `SessionRef`, `scan_state`, `TranscriptCache.state`.
- Produces:
  - `refresh_states(refs, caches, states, now, checked) -> bool` — extracted, testable helper that updates a `states` dict on the `BG_UPDATE_SECS` cadence and returns whether anything changed. `checked: dict[Path, tuple[float, float]]` holds `(last_check_monotonic, last_mtime)` per path.
  - The dashboard keys `caches`, `states`, and rows on `SessionRef`; rows show the `CX`/`CL` tag.

- [ ] **Step 1: Write the failing tests**

```python
def test_refresh_states_notices_a_background_completion(tmp_path):
    path = write_jsonl(tmp_path / "bg.jsonl", [claude_rec("user", message={"role": "user", "content": "go"})])
    ref = cht.SessionRef(cht.CLAUDE, path, "main", "bg", "bg", None, "bg", "session", None, 0.0, "kb")
    states, checked = {}, {}
    assert cht.refresh_states([ref], {}, states, 100.0, checked) is True
    assert states[path] == "working"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(claude_rec("system", subtype="turn_duration", durationMs=900)) + "\n")
    os.utime(path, (time.time() + 2, time.time() + 2))
    assert cht.refresh_states([ref], {}, states, 200.0, checked) is True
    assert states[path] == "done"


def test_refresh_states_skips_unchanged_files_within_the_cadence(tmp_path):
    path = write_jsonl(tmp_path / "bg2.jsonl", [claude_rec("user", message={"role": "user", "content": "go"})])
    ref = cht.SessionRef(cht.CLAUDE, path, "main", "bg2", "bg2", None, "bg2", "session", None, 0.0, "kc")
    states, checked = {}, {}
    cht.refresh_states([ref], {}, states, 100.0, checked)
    assert cht.refresh_states([ref], {}, states, 100.5, checked) is False


def test_cache_update_reports_change_on_state_transition_alone(tmp_path):
    path = write_jsonl(tmp_path / "bg3.jsonl", [claude_rec("user", message={"role": "user", "content": "go"})])
    ref = cht.SessionRef(cht.CLAUDE, path, "main", "bg3", "bg3", None, "bg3", "session", None, 0.0, "kd")
    cache = cht.TranscriptCache(ref)
    cache.update()
    with path.open("a", encoding="utf-8") as handle:      # renders nothing, but changes state
        handle.write(json.dumps(claude_rec("system", subtype="stop_hook_summary")) + "\n")
        handle.write(json.dumps(claude_rec("system", subtype="turn_duration", durationMs=10)) + "\n")
    assert cache.update() is True
    assert cache.state == "done"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "refresh_states or state_transition_alone"`
Expected: FAIL — `module 'cht' has no attribute 'refresh_states'`.

- [ ] **Step 3: Implement status refresh**

```python
def refresh_states(refs: list[SessionRef], caches: dict[Path, TranscriptCache],
                   states: dict[Path, str], now: float,
                   checked: dict[Path, tuple[float, float]]) -> bool:
    """Keep every displayed row's status current without rendering it."""
    changed = False
    for ref in refs:
        cached = caches.get(ref.path)
        if cached is not None:
            if states.get(ref.path) != cached.state:
                states[ref.path] = cached.state
                changed = True
            continue
        try:
            mtime = ref.path.stat().st_mtime
        except OSError:
            continue
        seen = checked.get(ref.path)
        if seen and now - seen[0] < BG_UPDATE_SECS and mtime == seen[1]:
            continue
        checked[ref.path] = (now, mtime)
        state = ref.fmt.scan_state(ref.path)
        if states.get(ref.path) != state:
            states[ref.path] = state
            changed = True
    return changed
```

In `TranscriptCache.update()`, track the state transition as a change:

```python
        before = self.state
        ...  # existing per-record loop
        if self.state != before:
            changed = True
```

In the dashboard loop, replace the redraw-time state block with a call on the `BG_UPDATE_SECS` cadence:

```python
        if sessions and now - last_bg >= BG_UPDATE_SECS:
            last_bg = now
            if refresh_states(sessions, caches, states, now, checked):
                dirty = True
            for ref in sessions:
                if ref.path != sessions[selected].path and ref.path in caches and caches[ref.path].update():
                    dirty = True
            if mirrors is not None:
                mirrors.sync(sessions)
```

Session rows render `f"{ref.fmt.tag} {ref.name:<14.14} {ref.role:<10.10} {states[ref.path]:<8}"`, and rescans use `discovery_snapshot(roots)` compared on `(path, name, role)` rather than paths alone.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "fix: refresh session status independently of redraw and list membership

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 18: Entry-point selection across formats

**Files:**
- Modify: `codex-human-transcript` — `follow()`, `agent_footer()`, `mirror()`, `newest_transcript()`, `main()`
- Test: `tests/test_codex_human_transcript.py`

**Interfaces:**
- Consumes: `discovery_snapshot`, `SessionRef`, `RootSel`.
- Produces:
  - `newest_main(refs: list[SessionRef]) -> SessionRef | None` — newest `kind == "main"`; `newest_transcript(root)` keeps its signature, now `newest_main(CODEX.discover(root))`.
  - `agent_footer(refs: list[SessionRef], current: SessionRef | None) -> str` — filters subagents by `parent_id`.
  - `mirror(root, log_dir, poll, once)` accepts a `Path` (Codex, as today) or a `list[RootSel]`.
  - `latest.log` points at the newest main, matching `follow()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_newest_main_never_picks_a_subagent(tmp_path):
    main = cht.SessionRef(cht.CLAUDE, tmp_path / "m.jsonl", "main", "m", "m", None, "m",
                          "session", None, 100.0, "km")
    sub = cht.SessionRef(cht.CLAUDE, tmp_path / "a.jsonl", "subagent", None, "m", "agent-1", "a",
                         "Explore", None, 500.0, "ka")
    assert cht.newest_main([sub, main]) is main


def test_agent_footer_lists_only_this_session_family(tmp_path):
    parent = cht.SessionRef(cht.CLAUDE, tmp_path / "p.jsonl", "main", "p1", "p1", None, "p1",
                            "session", None, 10.0, "kp")
    mine = cht.SessionRef(cht.CLAUDE, tmp_path / "a1.jsonl", "subagent", None, "p1", "agent-1",
                          "mine", "Explore", None, 11.0, "k1")
    other = cht.SessionRef(cht.CLAUDE, tmp_path / "a2.jsonl", "subagent", None, "p2", "agent-2",
                           "other", "Explore", None, 12.0, "k2")
    text = cht.strip_ansi(cht.agent_footer([parent, mine, other], parent))
    assert "mine" in text and "other" not in text


def test_mirror_once_across_both_roots_writes_logs_and_points_latest_at_newest_main(tmp_path):
    codex_root = tmp_path / "sessions"
    claude_root = tmp_path / "projects"
    cx = write_rollout(codex_root / "2026/09/17/rollout-2026-09-17T08-00-00-aaaa.jsonl",
                       [meta(), rec("agent_message", top="event_msg", message="codex says hi")])
    cl = write_jsonl(claude_root / "slug" / "11111111-2222-3333-4444-555555555555.jsonl",
                     [assistant_text("claude says hi")])
    now = time.time()
    os.utime(cx, (now - 60, now - 60))
    os.utime(cl, (now, now))
    log_dir = tmp_path / "logs"
    cht.mirror([cht.RootSel("codex", codex_root, True), cht.RootSel("claude", claude_root, True)],
               log_dir, poll=0.01, once=True)
    bodies = {p.name: p.read_text(encoding="utf-8") for p in log_dir.glob("*.log")}
    assert any("codex says hi" in b for b in bodies.values())
    assert any("claude says hi" in b for b in bodies.values())
    assert "claude says hi" in (log_dir / "latest.log").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q -k "newest_main or agent_footer_lists or both_roots"`
Expected: FAIL — `module 'cht' has no attribute 'newest_main'`.

- [ ] **Step 3: Implement shared selection**

```python
def newest_main(refs: list[SessionRef]) -> SessionRef | None:
    mains = [r for r in refs if r.kind == "main"]
    return max(mains, key=lambda r: r.mtime, default=None)


def newest_transcript(root: Path) -> Path | None:
    ref = newest_main(CODEX.discover(root))
    return ref.path if ref else None


def agent_footer(refs: list[SessionRef], current: SessionRef | None) -> str:
    parent = current.parent_id if current else None
    entries = [(r.name, r.role, r.path.name) for r in refs
               if r.kind == "subagent" and (parent is None or r.parent_id == parent)]
    if not entries:
        return ""
    lines = [STYLE.paint("1;90", "╰─ active sub-agents")]
    for name, role, filename in sorted(set(entries), key=lambda item: item[0].lower()):
        lines.append(STYLE.paint("90", f"   {name:<12} {role:<8} {filename}"))
    return "\n".join(lines)
```

`follow()` and `mirror()` normalize their root argument once:

```python
def as_roots(root: "Path | list[RootSel] | None") -> list[RootSel]:
    if root is None:
        return []
    if isinstance(root, Path):
        return [RootSel("codex", root, True)]
    return list(root)
```

Both then build their session list from one `discovery_snapshot(as_roots(root))` per `RESCAN_SECS`, `follow()` switches to `newest_main(...)`, and `MirrorSet.sync` receives the full snapshot with the newest main first so `latest.log` matches. `main()` passes `resolve_roots(...)` to both.

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/codex-human-transcript && python3 -m pytest tests -q && uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`
Expected: PASS. Update the existing `test_mirror_once_writes_all_sessions_and_latest_symlink` only if it fails — its `Path` argument must keep working.

- [ ] **Step 5: Commit**

```bash
cd ~/github/codex-human-transcript
git add codex-human-transcript tests
git commit -m "feat: one selection contract across follow, mirror, footer, and latest.log

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 19: Docs, smoke run, and deploy

**Files:**
- Modify: `codex-human-transcript` — module docstring (~lines 1-18)
- Test: manual smoke runs

**Interfaces:**
- Consumes: everything.
- Produces: an installed `~/.local/bin/codex-human-transcript` identical to the tested copy.

- [ ] **Step 1: Update the module docstring**

Replace the "Codex roots" paragraph and add a Claude paragraph:

```
Roots: Codex sessions dirs ($CODEX_HOME, ~/.codex, WSL-mounted Windows profiles) and
Claude Code project dirs ($CLAUDE_CONFIG_DIR, ~/.claude/projects, WSL profiles) are both
auto-discovered. Press `r` in the TUI to pick which roots to watch; the selection is saved
in ~/.config/codex-human-transcript/config.json. Sessions from both agents share one list,
tagged CX / CL. Press `t` to hide Claude thinking blocks (text logs always keep them).
```

- [ ] **Step 2: Run the whole suite and the linter one final time**

```bash
cd ~/github/codex-human-transcript
python3 -m pytest tests -q
uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests
```

Expected: all tests pass; ruff clean.

- [ ] **Step 3: Smoke-run against real transcripts**

```bash
cd ~/github/codex-human-transcript
CLAUDE_FILE=$(ls -t ~/.claude/projects/*/*.jsonl | head -1)
CODEX_FILE=$(ls -t ~/.codex/sessions/*/*/*/rollout-*.jsonl 2>/dev/null | head -1)
./codex-human-transcript --once --file "$CLAUDE_FILE" | head -40
[ -n "$CODEX_FILE" ] && ./codex-human-transcript --once --file "$CODEX_FILE" | head -20
./codex-human-transcript --list-roots
```

Expected: the Claude file renders `YOU` / `CLAUDE` / `EXEC` / `TOOL` blocks with no traceback; the Codex file renders as before; `--list-roots` lists both formats with tags.

- [ ] **Step 4: Smoke-run the mirror over both roots**

```bash
cd ~/github/codex-human-transcript
timeout 20 ./codex-human-transcript --mirror --log-dir /tmp/cht-smoke || true
ls /tmp/cht-smoke | head
grep -c . /tmp/cht-smoke/latest.log
```

Expected: logs appear for both formats, `latest.log` resolves to a main session's log, and no secret strings appear (`grep -riE 'sk-[a-z0-9]{8}|bearer ' /tmp/cht-smoke | head` prints nothing).

- [ ] **Step 5: Deploy and commit**

```bash
cd ~/github/codex-human-transcript
cp codex-human-transcript ~/.local/bin/codex-human-transcript
chmod +x ~/.local/bin/codex-human-transcript
cmp codex-human-transcript ~/.local/bin/codex-human-transcript && echo INSTALLED
git add codex-human-transcript docs
git commit -m "docs: document Claude roots, the merged list, and the thinking toggle

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

Expected: `INSTALLED` printed, commit created.

---

## Spec coverage map

| Spec section | Task |
|---|---|
| 1.1 one framing implementation | 1, 4 |
| 1.2 record-aligned checkpoints | 2 |
| 1.3 recursive redaction | 3 |
| 1.4 bounded reads, oversized policy, backfill | 1, 5 |
| 1.5 per-source failure isolation | 5 |
| 2.1 `SessionRef` | 6 |
| 2.2 `Format` adapters | 6 |
| 2.3 mirror identity + Codex log compat | 6, 7 |
| 2.4 format detection + `--format` | 8 |
| 3.1 Claude roots, discovery, snapshot | 9 |
| 3.2 metadata and sidecars | 10 |
| 3.3 four-state status | 11 |
| 3.4 history semantics | 12, 13 |
| 3.5 rendering table | 12 |
| 4.1 roots config + resolver | 14 |
| 4.2 picker | 15 |
| 4.3 thinking display-only | 16 |
| 4.4 status freshness | 17 |
| 4.5 selection semantics | 18 |
| Testing invariants 1-16 | 1-18 (each task's Step 1) |
| Deployment | 19 |
