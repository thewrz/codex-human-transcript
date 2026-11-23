# codex-human-transcript: Claude Code transcript support — design

Date: 2026-09-17 · Status: revised after Codex (gpt-6-astra, xhigh) adversarial review

## Goal

Teach `codex-human-transcript` to discover, render, tail, and mirror **Claude Code** session
transcripts alongside Codex rollouts, in one merged session list.

The adversarial review found four defects in the *existing* code that Claude transcripts would
hit constantly (record loss while tailing, colliding mirror filenames, key-blind redaction,
unbounded reads). They are fixed as part of this work, in its first stage — not deferred.

## Constraints

- Stays a **single, stdlib-only Python file**, keeps its name, config path
  (`~/.config/codex-human-transcript/config.json`) and log dir
  (`~/.local/state/codex-human-transcript/logs/`). Existing Codex mirrors keep working:
  their log filenames and offset files are unchanged.
- All existing Codex *behavior* is preserved; existing Codex tests pass unchanged.
- The viewer never crashes on unexpected input, and one bad source never stops the others.

## Stages

Each stage lands green (tests + ruff) before the next starts.

1. **Shared plumbing fixes** — one incremental reader everywhere, record-aligned checkpoints,
   recursive key-aware redaction, bounded reads.
2. **Format seam** — `Format` adapters + `SessionRef`, every call site migrated; Codex-only
   behavior otherwise identical.
3. **Claude discovery, state, rendering.**
4. **Multi-root config + picker, thinking toggle, dashboard status refresh.**

---

## Stage 1 — shared plumbing fixes

### 1.1 One framing implementation (review finding: record loss)

`follow()` currently uses text `readline()`, so a record split across two writes is consumed as
two malformed fragments and permanently lost. Claude writes large records (tool results,
thinking), so splits are common.

- `follow()` uses `NdjsonTail` like every other live path. `--once` reads through the same tail
  in a loop to EOF.
- `NdjsonTail` keeps its incremental UTF-8 decoder (already correct across multibyte splits).
- New: `NdjsonTail.committed` — the byte offset of the end of the **last complete record**
  (i.e. `offset - len(partial_bytes)`). `offset` remains the raw read position.

### 1.2 Record-aligned checkpoints (review finding: restart loses a record)

`TextLog` persists `tail.offset`, which includes a half-read record held only in memory; a
restart resumes past its prefix and loses it.

- `TextLog` persists `tail.committed` instead.
- The offset file becomes JSON: `{"offset": N, "format": "...", "render": {...}, "v": 2}`.
  A bare integer (v1) is still read, treated as a Codex committed offset.
- Checkpoint validity requires a matching `format` and rendering options (see 4.3); a mismatch
  forces a full rebuild of that log.

### 1.3 Key-aware, recursive redaction (review finding: secrets leak into mirrors)

`kv_lines()` passes each *value* to `clean()`, discarding the key, and `clean()` only redacts
dict keys at the outermost level. `{"api_key": "..."}` therefore prints unredacted — into
persistent text logs.

- New `redact(value)`: walks dicts/lists recursively; any key matching `SECRET_KEYS` has its
  whole subtree replaced with `[REDACTED]`; strings still pass the existing pattern scrubbers.
- `kv_lines()` redacts the structure first and keeps key context; `clean()` calls `redact()` for
  containers. Claude `tool_use.input` goes through `redact()` before any formatting.

### 1.4 Bounded reads (review finding: TUI freeze / dropped oversized record)

- `NdjsonTail.read(max_bytes=...)`: each pass reads at most `READ_CHUNK_BYTES` (1 MiB) and
  reports `more_pending`. The dashboard and mirror loops keep polling while `more_pending`, so a
  large backlog is absorbed across iterations instead of one blocking read.
- `TextLog.sync()` streams blocks to the open file as they are rendered rather than
  accumulating the whole batch.
- Oversized-record policy: a single record whose raw bytes exceed `MAX_RECORD_BYTES` (4 MiB) is
  rendered as a one-line `⟦oversized record: N bytes, skipped⟧` placeholder and its bytes are
  committed, so the tail never stalls and memory stays bounded.
- Display backfill: after seeking back `BACKFILL_BYTES`, the discarded partial line is counted;
  if the seek lands inside a record so large that no complete record remains in the window, the
  window is extended once to the start of the last complete record (bounded by
  `MAX_RECORD_BYTES`), instead of yielding an empty cache.

### 1.5 Per-source failure isolation (review finding: one bad file kills the run)

- Every per-source operation (open, stat, read, render, mirror write) is wrapped so an `OSError`
  or malformed-shape failure marks *that* source `error` with a short reason shown in its row,
  and never propagates.
- `save_config()` and mirror writes failing (unwritable dir, ENOSPC) set a status message; they
  do not terminate the dashboard.
- `records()` additionally guards nested shapes: every consumer (detection, metadata, state,
  render) validates types before use.

---

## Stage 2 — the format seam

### 2.1 `SessionRef` (review finding: entry points aren't format-neutral)

A normalized descriptor replaces the ad-hoc `(name, role, path)` tuples:

```
SessionRef(format, path, kind: "main"|"subagent", session_id, parent_id, agent_id,
           name, role, cwd, mtime, source_key)
```

`source_key` is the stable mirror identity (see 2.3). `agent_footer()` filters by `parent_id`;
`MirrorSet`, `TranscriptCache`, `follow()` and the dashboard all key on `SessionRef`.

### 2.2 `Format` adapters

Two instances, `CODEX` and `CLAUDE`:

| Member | Responsibility |
|---|---|
| `name` / `tag` | `"codex"`/`CX`, `"claude"`/`CL` |
| `looks_like(path/dir)` | positive signature for detection (2.4) |
| `discover(root)` | → `list[SessionRef]` |
| `meta(ref)` | session id, cwd, branch, identity (2.5 / Codex `session_meta`) |
| `render(record, opts)` | record → ANSI block or `None` |
| `advance(state, record)` | → `SessionState` (2.6) |
| `scan_state(path)` | tail scan → `SessionState` for unopened files (2.6) |
| `dedupe` | `Deduper` applies (Codex only) |

Existing Codex functions become `CODEX` members unchanged in behavior. Note the signature
difference the review flagged: Codex's `next_state()` takes a *payload kind*; the adapter method
takes a *record* and extracts the kind itself.

### 2.3 Mirror identity (review finding: log filename collisions)

`TextLog` names logs and offsets from `source.stem` alone, so two roots holding the same session
filename — or two parent sessions each holding `agent-abc.jsonl` — share one log and one offset
file: overwrites, interleaving, wrong resume points.

- `source_key` = `<format>-<root-hash8>-<parent-or-session-id>[-<agent-id>]`, where `root-hash8`
  is 8 hex chars of a SHA-256 over the resolved root path.
- **Codex compatibility:** a Codex *main* session whose current log name (`<stem>.log`) already
  exists keeps that name; new Codex logs and all Claude logs use `source_key`. A one-line index
  file `logs/index.json` maps `source_key → log filename`.
- Aliases of one physical source (e.g. the same dir reachable via two roots) are deduplicated by
  `(st_dev, st_ino)` before mirroring.

### 2.4 Format detection (review finding: "otherwise Codex" is wrong)

- `detect_format(path)` reads at most `SNIFF_BYTES` (256 KiB) / 50 records and returns
  `CODEX`, `CLAUDE`, or `None`:
  - Codex signature: a record with `payload.type`, or `type == "session_meta"`, or the filename
    matches `rollout-*.jsonl` under a `YYYY/MM/DD` path.
  - Claude signature: a record with `sessionId` **and** (`message` or a known Claude bookkeeping
    `type`), or the path matches `projects/*/<uuid>.jsonl` / `*/subagents/agent-*.jsonl`.
- `None` (empty, bookkeeping-only, partially written) is **not** a render failure: the file is
  marked `unknown`, shown as pending, and re-sniffed after it grows.
- A format learned from discovery or config is authoritative and never re-sniffed.
- Root layout inference works the same way; an ambiguous or empty `--root` is an error telling
  the user to pass `--format codex|claude` (new flag, applies to `--root` and `--file`).

---

## Stage 3 — Claude discovery, state, rendering

### 3.1 Roots and discovery

- Claude root candidates: `$CLAUDE_CONFIG_DIR/projects`, `~/.claude/projects`,
  `/mnt/*/Users/*/.claude/projects`, other local users' `~/.claude/projects`. Existing,
  readable dirs only.
- Mains: `projects/<slug>/<uuid>.jsonl`, mtime within `RECENT_DAYS` (14).
- Subagents: `projects/<slug>/<uuid>/subagents/agent-*.jsonl`, own mtime within `RECENT_DAYS` —
  **independent of the 3-main display limit**, so a recently resumed agent under an older parent
  still appears. `parent_id` comes from the containing `<uuid>` directory; `agent_id` from the
  filename. These come from the path, never from file contents.
- **Cost control (review finding):** one `discovery snapshot` (all formats, all roots) is built
  on the `RESCAN_SECS` cadence and *shared* by the dashboard, `follow()`, `agent_footer()`,
  `MirrorSet`, and root counts. `follow()` no longer rescans per loop iteration. Candidate
  selection uses `os.scandir` mtime only; no file is opened before it is selected.

### 3.2 Metadata (review finding: first record may lack cwd)

- Fields are collected independently over the first `SNIFF_BYTES`: `session_id`, `cwd`,
  `gitBranch`, `version`. Missing fields stay `None` rather than freezing a wrong value.
- The metadata cache is keyed by `(path, st_size, st_mtime)`; an entry with missing fields is
  re-read when the file grows. Sidecar `agent-*.meta.json` is cached the same way, so a sidecar
  written after the transcript is picked up; an absent/invalid/non-object sidecar falls back to
  name = agent id, role = `subagent`.
- Dashboard rescans compare full `SessionRef` identity (name, role, state), not just paths, so a
  late-arriving description refreshes the row.

### 3.3 State (review findings: end_turn isn't proof; boolean can't express error)

`SessionState` is one of `working`, `done`, `error`, `unknown`, and is explicitly a
*conservative observation of the transcript*, not a lifecycle protocol.

- `unknown`: no decisive evidence yet (empty file, bookkeeping only, markers outside the scan
  window).
- `working`: a non-meta user prompt, a `tool_use`, or any record after the last done marker.
- `done`: `system/turn_duration`, **or** an assistant `stop_reason: "end_turn"` *not followed by*
  further activity. Because Stop hooks can force continuation, `end_turn` alone demotes to
  `working` as soon as any later non-bookkeeping record appears.
- `error`: an API-error record or `stop_reason` indicating failure; a later user prompt or
  `tool_use` returns it to `working`.
- Interruptions produce no marker; the session stays `working` until a later marker or, in the
  dashboard, is shown as stale by its mtime.

`scan_state(path)` replaces `has_completed()` for both formats and returns the same
`SessionState`, using the **same classifier** as `advance()` over records parsed from a tail
window. It reads backwards in `SCAN_CHUNK` (64 KiB) steps up to `MAX_SCAN_BYTES` (1 MiB),
always aligning to a record boundary and parsing JSON — never substring-matching, which today
can match marker-like text nested inside tool data. No decisive marker within the limit →
`unknown`. `has_completed()` remains as a thin compatibility wrapper (`state == "done"`).

### 3.4 History semantics (review finding: resumed/forked/compacted sessions)

The viewer shows the **physical append history** of one file, in write order. It does not
reconstruct the active `parentUuid` chain. Consequences, stated so renderers don't have to
guess:

- Replayed or branched records render again; identical `message.id` values are **not**
  deduplicated (distinct content blocks legitimately share one id).
- `isCompactSummary` user records render as `⟲ COMPACTED` with the summary body, not `❯ YOU`,
  and do not count as a working marker.
- A forked session is a separate file and therefore a separate row.

### 3.5 Rendering

**Rendered**

| Claude record | Block |
|---|---|
| `user` string / `text` blocks (non-meta, non-summary) | `❯ YOU` |
| `assistant` `text` | `✦ CLAUDE` (orange/yellow rail, markdown) |
| `assistant` `thinking` | `💭 THINKING`, dim; hidden when thinking display is off |
| `tool_use` `Bash` | `⚙ EXEC ⇢ $ <command>` + dim description |
| `tool_use` `Read`/`Write`/`Edit`/`Grep`/`Glob` | `⚙ TOOL ⇢ <name>` + compact path/pattern; `Edit` shows truncated `-old` / `+new` |
| `tool_use` `Agent`/`Task` | `👥 AGENT ⇢ <subagent_type>` + description + truncated prompt |
| `tool_use` `TodoWrite` and task-list tools | `📋 PLAN` with ✔ ▶ ○ |
| `tool_use` `mcp__<server>__<tool>` | `⚙ MCP ⇢ server/tool` |
| other `tool_use` | `⚙ TOOL ⇢ name` + redacted `kv_lines` |
| `tool_result` | `⚙ TOOL ⇠`; red `✘ TOOL ⇠` when `is_error`; text from string or `text` blocks; images → `⟦image⟧` |
| `system` `compact_boundary`, `isCompactSummary` user record | `⟲ COMPACTED` |
| `system` `turn_duration` | `✔ DONE` + dim duration |
| API error (`isApiErrorMessage`) | `✘ ERROR` |
| `<command-name>` user content | one dim line `⌘ /cmd args` |
| `<local-command-stdout>` | one dim truncated line |
| oversized record | `⟦oversized record: N bytes, skipped⟧` |

**Skipped**: all `attachment` records; `file-history-*`, `permission-mode`, `mode`,
`last-prompt`, `ai-title`, `queue-operation`, `atis-latch`; `system` subtypes
`stop_hook_summary`, `informational`, `away_summary`, `local_command`, `permission_retry`;
`isMeta: true` user messages; unknown record/block types.

**Shared rules**: all rendered text passes `redact()` + `clean()`. `Deduper` stays Codex-only.
Brand segment becomes `⚡ AGENTS`; help line gains `t thinking`.

---

## Stage 4 — config, picker, dashboard status

### 4.1 Roots config and precedence (review finding: migration underspecified)

One resolver owns every reader and writer (`main()`, `--remember`, `change_root()`,
`resolve_root()`, `pick_root()`):

- Stored as `roots: [{"format": ..., "path": ..., "enabled": true}]`, plus `"v": 2`.
- Legacy `{"root": "..."}` → one enabled Codex root; rewritten as `roots` on the next save.
  If both keys exist, `roots` wins and `root` is dropped.
- Precedence: `--root`/`--file` (with optional `--format`) > saved `roots` > discovery.
  CLI roots do not merge with saved ones; they replace them for that run.
- `roots: []` is an *intentional empty selection* (nothing watched, picker opens with discovery
  suggestions) and is distinct from a missing key (use discovery).
- Manually typed roots are validated, stored, and retained in the picker even when discovery
  doesn't find them. Newly discovered roots appear **unchecked** and never silently enable
  themselves.
- `--remember` persists the resolved CLI selection through the same resolver.

### 4.2 Picker

`r` opens a multi-select over saved ∪ discovered roots, each row showing format tag, path,
recent-session count, last activity, and a checkbox. Manual entry asks for a format when the
layout is ambiguous.

### 4.3 Thinking visibility (review finding: mirrors would be inconsistent)

Thinking is a **display-only** preference:

- The `t` key and `--no-thinking` affect the TUI and plain stdout only.
- **Mirrors always include thinking**, so a text log is a stable artifact and toggling never
  produces a half-and-half file. (`--mirror` ignores `--no-thinking`; the flag's help says so.)
- The checkpoint still records `render` options (1.2); if a future option does affect mirror
  output, a mismatch forces a rebuild rather than an inconsistent append.

### 4.4 Dashboard status freshness (review finding: background completion missed)

- Status for every *displayed* session refreshes on the `BG_UPDATE_SECS` cadence, independent of
  redraw and of whether the path list changed: cached sessions use their live state, uncached
  ones a cheap `scan_state()` gated on mtime change.
- A `SessionState` transition marks the screen dirty even when no new lines rendered
  (`TranscriptCache.update()` returns changed on state change, not only on output).
- `TranscriptCache.reset()` and truncation reset state to `unknown` and re-derive it, instead of
  preserving the stale previous value.

### 4.5 Selection semantics across entry points (review finding)

Defined explicitly, from the shared snapshot:

| Path | Selection |
|---|---|
| dashboard | merged list: 3 newest mains, then ≤20 subagents, newest first, `CX`/`CL` tagged |
| `follow()` / plain | newest **main** session across enabled roots (today's `newest_transcript()` can pick a subagent — that is corrected); switching across formats prints the existing `── switched to … ──` banner |
| `--once` | the same newest main, or `--file` |
| `--file` | that file, format from `--format` or detection |
| `--mirror` | every session in the snapshot |
| `latest.log` | points at the newest main, matching `follow()` |
| `agent_footer()` | subagents whose `parent_id` matches the current session's |

---

## Testing

TDD in `tests/test_codex_human_transcript.py`. Fixtures are written to a real temp dir; Claude
fixtures are modeled on real records, including at least one captured **real** resumed session
and one compacted session (sanitized). Invariants:

**Stage 1**
1. A record split across two writes survives; a multibyte character split across writes survives.
2. Restart between writes loses nothing: a complete record followed by an incomplete one commits
   only the complete one; the incomplete one appears after the next write.
3. `redact()` masks nested secret keys; `kv_lines({"api_key": ...})` is redacted; a plain-text
   mirror of the same input contains no secret.
4. A single record larger than `MAX_RECORD_BYTES` yields the placeholder and does not stall; a
   600 KiB final record still renders under the display backfill; a large append burst is
   absorbed over multiple bounded passes.
5. An unreadable/deleted transcript, a non-object sidecar, and an unwritable log dir each mark
   one source `error` while other sources keep updating.

**Stage 2**
6. `detect_format` returns Codex/Claude/None correctly, including empty, bookkeeping-only,
   partial-first-record, and misleadingly-named files; an unknown file resolves after it grows.
7. Two sources with the same basename under different roots/parents mirror to distinct logs with
   independent offsets across a restart; an existing Codex log keeps its name.
8. All existing Codex tests pass unchanged.

**Stage 3**
9. Every rendered record type yields its badge; every skipped type yields `None`.
10. Discovery returns mains and subagents with correct name/role/parent, including sidecar
    fallbacks and a late-arriving sidecar; a recent subagent under an old parent is listed.
11. `advance()` and `scan_state()` agree on the same fixture for all four states, including
    `end_turn` followed by hook-driven continuation (→ `working`), an error tail, markers beyond
    `MAX_SCAN_BYTES` (→ `unknown`), and marker-like text nested in tool data (ignored).
12. A resumed/forked/compacted fixture renders in write order, repeats replayed records, does not
    dedupe shared `message.id`, and renders the compact summary as `⟲ COMPACTED`.
13. Discovery over a tree of many historical project dirs stays within a bounded
    filesystem-operation count and opens no unselected file.

**Stage 4**
14. Config round-trips: legacy load, `--remember`, picker save, `roots: []`, custom root absent
    from discovery, new discovered root defaults to unchecked, CLI precedence.
15. Thinking: TUI toggle changes display only; a mirror written under `--no-thinking` still
    contains thinking; reusing an existing log under either flag stays internally consistent.
16. A background session completing with no keypress marks the screen dirty and updates its row.

Verification: `python3 -m pytest tests -q`;
`uvx ruff check --select E,F,W,B,UP,SIM --line-length 120 codex-human-transcript tests`;
manual `--once --file` smoke run against one real Claude transcript and one real Codex rollout,
plus a `--mirror` run over both roots.

## Deployment

Edit `~/github/codex-human-transcript/codex-human-transcript` (canonical, tested copy). After all checks
pass, copy it over `~/.local/bin/codex-human-transcript`. No git repo or project board applies.

## Out of scope

Package split; renaming the tool; filtering sessions by project/cwd; rendering Claude
`file-history` diffs or hook output; reconstructing the active message chain (3.4); async or
threaded I/O.

## Review

Adversarial review: Codex `gpt-6-astra`, effort xhigh, read-only isolated sandbox, spec + current
script only (no real transcripts). Verdict `rework`, 4 high / 11 medium findings — all folded in
above. Raw verdict kept with the session scratchpad
(`codex-spec-review/verdict.json`).
