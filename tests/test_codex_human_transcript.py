"""Behavioral tests for the codex-human-transcript script (extensionless, loaded by path)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "codex-human-transcript"


def _load():
    loader = importlib.machinery.SourceFileLoader("cht", str(SCRIPT))
    spec = importlib.util.spec_from_loader("cht", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cht = _load()

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude"


def test_terminal_follows_transcript_without_curses(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "curses", None)
    module = _load()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--root", str(FIXTURES), "--file",
                                     str(FIXTURES / "resumed.jsonl"), "--no-ledger"])

    def stop_after_reading(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", stop_after_reading)
    assert module.main() == 0
    assert "text-0" in capsys.readouterr().out


def rec(kind: str, ts: str = "2026-08-20T10:00:00.000Z", top: str = "response_item", **payload):
    return {"timestamp": ts, "type": top, "payload": {"type": kind, **payload}}


def meta(thread_source: str = "user", **extra):
    return {"timestamp": "2026-08-20T10:00:00.000Z", "type": "session_meta",
            "payload": {"id": "abc", "thread_source": thread_source, **extra}}


def write_rollout(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def plain(text: str | None) -> str:
    assert text is not None
    return cht.strip_ansi(text)


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


# ── rendering gaps found in real rollouts ───────────────────────────────────

def test_render_exec_command_shows_command_and_workdir():
    out = plain(cht.render(rec("function_call", name="exec_command",
                               arguments=json.dumps({"cmd": "git status --short", "workdir": "/repo"}))))
    assert "EXEC" in out
    assert "$ git status --short" in out
    assert "/repo" in out


def test_render_function_call_output_collapses_chunk_header():
    output = ("Chunk ID: 7431c9\nWall time: 0.0123 seconds\nProcess exited with code 0\n"
              "Original token count: 12\nOutput:\nhello world\n")
    out = plain(cht.render(rec("function_call_output", call_id="c1", output=output)))
    assert "hello world" in out
    assert "Chunk ID" not in out
    assert "exit 0" in out


def test_render_agent_tool_calls_get_agent_badge():
    out = plain(cht.render(rec("function_call", name="spawn_agent",
                               arguments=json.dumps({"agent_role": "worker", "nickname": "Raman"}))))
    assert "AGENT" in out and "spawn_agent" in out and "Raman" in out


def test_render_update_plan_lists_steps():
    plan = {"plan": [{"step": "Read code", "status": "completed"},
                     {"step": "Write tests", "status": "in_progress"}]}
    out = plain(cht.render(rec("function_call", name="update_plan", arguments=json.dumps(plan))))
    assert "PLAN" in out
    assert "Read code" in out and "Write tests" in out
    assert "in_progress" in out or "▶" in out


def test_render_user_role_message_is_labelled_you():
    out = plain(cht.render(rec("message", role="user",
                               content=[{"type": "input_text", "text": "fix the bug"}])))
    assert "YOU" in out and "CODEX" not in out


def test_render_developer_role_message_is_hidden():
    assert cht.render(rec("message", role="developer",
                          content=[{"type": "input_text", "text": "giant system prompt"}])) is None


def test_render_patch_apply_end_lists_files():
    out = plain(cht.render(rec("patch_apply_end", top="event_msg", success="true",
                               stdout="Success. Updated the following files:\nA src/x.ts\n", stderr="")))
    assert "PATCH" in out and "A src/x.ts" in out


def test_render_web_search_end_shows_query():
    out = plain(cht.render(rec("web_search_end", top="event_msg", query="stagelinq changelog")))
    assert "SEARCH" in out and "stagelinq changelog" in out


def test_render_turn_aborted_shows_reason():
    out = plain(cht.render(rec("turn_aborted", top="event_msg", reason="interrupted")))
    assert "ABORTED" in out and "interrupted" in out


def test_render_context_compacted_is_marked():
    assert "COMPACT" in plain(cht.render(rec("context_compacted", top="event_msg")))


def test_clean_preserves_literal_backslash_n_inside_strings():
    # A code snippet containing the two characters backslash + n must not turn into a newline.
    assert cht.clean('print("a\\nb")') == 'print("a\\nb")'


def test_clean_still_unescapes_dumped_json_values():
    assert "line1\nline2" in cht.clean({"text": "line1\nline2"})


OSC52_PAYLOAD = "\x1b]52;c;aGVsbG8=\x1b\\"


def test_clean_strips_osc52_clipboard_sequence():
    cleaned = cht.clean(f"click here {OSC52_PAYLOAD} thanks")
    assert "\x1b" not in cleaned
    assert "52;c;" not in cleaned
    assert "click here" in cleaned and "thanks" in cleaned


def test_render_does_not_leak_osc52_from_untrusted_message():
    out = cht.render(rec("agent_message", top="event_msg", message=f"pwn {OSC52_PAYLOAD} done"))
    assert "\x1b]" not in out
    assert "52;c;" not in out


def test_render_keeps_its_own_color_styling_when_enabled():
    cht.STYLE.enabled = True
    try:
        out = cht.render(rec("agent_message", top="event_msg", message="hi"))
    finally:
        cht.STYLE.enabled = False
    assert "\x1b[" in out  # the tool's own SGR codes still render


# ── transcript cache ────────────────────────────────────────────────────────

def test_cache_dedupes_agent_message_and_assistant_message(tmp_path):
    path = write_rollout(tmp_path / "rollout-a.jsonl", [
        meta(),
        rec("agent_message", top="event_msg", message="The answer is 42."),
        rec("message", role="assistant", content=[{"type": "output_text", "text": "The answer is 42."}]),
    ])
    cache = cht.TranscriptCache(path)
    cache.update()
    body = "\n".join(cht.strip_ansi(line) for line in cache.lines)
    assert body.count("The answer is 42.") == 1


def test_cache_state_returns_to_working_when_new_turn_starts(tmp_path):
    path = write_rollout(tmp_path / "rollout-b.jsonl", [
        meta(),
        rec("task_complete", top="event_msg", last_agent_message="done"),
    ])
    cache = cht.TranscriptCache(path)
    cache.update()
    assert cache.state == "done"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec("task_started", top="event_msg")) + "\n")
    cache.update()
    assert cache.state == "working"


def test_cache_handles_multibyte_char_split_across_reads(tmp_path):
    path = tmp_path / "rollout-c.jsonl"
    line = json.dumps(rec("agent_message", top="event_msg", message="héllo wörld ⟦✓⟧"), ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    cut = data.index("ö".encode()) + 1  # split inside the 2-byte ö
    path.write_bytes(data[:cut])
    cache = cht.TranscriptCache(path)
    cache.update()
    path.write_bytes(data)
    cache.update()
    assert "héllo wörld ⟦✓⟧" in "\n".join(cht.strip_ansi(line) for line in cache.lines)


def test_has_completed_is_false_once_a_new_turn_starts(tmp_path):
    path = write_rollout(tmp_path / "rollout-d.jsonl", [
        meta(),
        rec("task_complete", top="event_msg", last_agent_message="done"),
        rec("task_started", top="event_msg"),
    ])
    assert cht.has_completed(path) is False


# ── human-readable text log ─────────────────────────────────────────────────

def test_text_log_writes_plain_text_without_ansi(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-e.jsonl", [
        meta(),
        rec("user_message", top="event_msg", message="hello **bold**"),
        rec("agent_message", top="event_msg", message="hi there"),
    ])
    log = cht.TextLog(tmp_path / "logs", src)
    log.sync()
    text = log.path.read_text(encoding="utf-8")
    assert log.path.name == f"{log.ref.source_key}.log"   # new log: collision-free identity
    assert "\033[" not in text
    assert "YOU" in text and "hello" in text and "CODEX" in text and "hi there" in text


def test_text_log_does_not_mirror_osc52_clipboard_sequence(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-osc.jsonl", [
        meta(),
        rec("agent_message", top="event_msg", message=f"pwn {OSC52_PAYLOAD} done"),
    ])
    log = cht.TextLog(tmp_path / "logs", src)
    log.sync()
    text = log.path.read_text(encoding="utf-8")
    assert "\x1b" not in text
    assert "52;c;" not in text
    assert "pwn" in text and "done" in text


def test_text_log_resumes_from_offset_without_duplicating(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-f.jsonl", [
        meta(), rec("agent_message", top="event_msg", message="first"),
    ])
    cht.TextLog(tmp_path / "logs", src).sync()
    with src.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec("agent_message", top="event_msg", message="second")) + "\n")
    log = cht.TextLog(tmp_path / "logs", src)  # fresh object = process restart
    log.sync()
    text = log.path.read_text(encoding="utf-8")
    assert text.count("first") == 1 and text.count("second") == 1


def test_text_log_rebuilds_when_source_is_truncated(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-g.jsonl", [
        meta(), rec("agent_message", top="event_msg", message="old long message " * 10),
    ])
    cht.TextLog(tmp_path / "logs", src).sync()
    write_rollout(src, [meta(), rec("agent_message", top="event_msg", message="fresh")])
    log = cht.TextLog(tmp_path / "logs", src)
    log.sync()
    text = log.path.read_text(encoding="utf-8")
    assert "old long" not in text and "fresh" in text


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


def test_mirror_once_writes_all_sessions_and_latest_symlink(tmp_path):
    root = tmp_path / "sessions"
    older = write_rollout(root / "2026/08/19/rollout-2026-08-19T09-00-00-aaaa.jsonl",
                          [meta(id="session-aaaa"), rec("agent_message", top="event_msg", message="older")])
    newer = write_rollout(root / "2026/08/20/rollout-2026-08-20T09-00-00-bbbb.jsonl",
                          [meta(id="session-bbbb"), rec("agent_message", top="event_msg", message="newer")])
    sub = write_rollout(root / "2026/08/20/rollout-2026-08-20T09-01-00-cccc.jsonl",
                        [meta("subagent", agent_nickname="Raman", agent_role="worker"),
                         rec("agent_message", top="event_msg", message="sub says hi")])
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    os.utime(sub, (now - 10, now - 10))
    log_dir = tmp_path / "logs"
    cht.mirror(root, log_dir, poll=0.01, once=True)
    # new (non-pre-existing) logs use the collision-free source_key, not the raw rollout stem.
    older_key = cht.source_key("codex", root, "session-aaaa")
    newer_key = cht.source_key("codex", root, "session-bbbb")
    assert (log_dir / f"{older_key}.log").read_text().count("older") == 1
    bodies = {p.name: p.read_text(encoding="utf-8") for p in log_dir.glob("*.log")}
    assert any("sub says hi" in b for b in bodies.values())
    latest = log_dir / "latest.log"
    assert latest.is_symlink()
    assert latest.resolve() == (log_dir / f"{newer_key}.log").resolve()
    assert "newer" in latest.read_text()


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


# ── codex root discovery & settings ─────────────────────────────────────────

def test_candidate_roots_cover_env_home_wsl_and_other_users(tmp_path):
    home = tmp_path / "home" / "user"
    mnt = tmp_path / "mnt"
    env = {"CODEX_HOME": str(tmp_path / "custom-codex")}
    for p in (home / ".codex" / "sessions", mnt / "c" / "Users" / "ExampleUser" / ".codex" / "sessions",
              mnt / "d" / "Users" / "Other" / ".codex" / "sessions",
              tmp_path / "home" / "bob" / ".codex" / "sessions",
              tmp_path / "custom-codex" / "sessions"):
        p.mkdir(parents=True)
    found = cht.candidate_roots(home=home, env=env, mnt=mnt, home_parent=tmp_path / "home")
    assert tmp_path / "custom-codex" / "sessions" in found
    assert home / ".codex" / "sessions" in found
    assert mnt / "c" / "Users" / "ExampleUser" / ".codex" / "sessions" in found
    assert mnt / "d" / "Users" / "Other" / ".codex" / "sessions" in found
    assert tmp_path / "home" / "bob" / ".codex" / "sessions" in found
    assert len(found) == len(set(found))


def test_discover_roots_ranks_by_newest_rollout_and_skips_empty(tmp_path):
    quiet = tmp_path / "quiet" / "sessions"
    busy = tmp_path / "busy" / "sessions"
    empty = tmp_path / "empty" / "sessions"
    empty.mkdir(parents=True)
    now = time.time()
    q = write_rollout(quiet / "2026/08/01/rollout-q.jsonl", [meta()])
    b = write_rollout(busy / "2026/08/20/rollout-b.jsonl", [meta()])
    os.utime(q, (now - 5000, now - 5000))
    os.utime(b, (now, now))
    infos = cht.discover_roots([quiet, empty, busy, busy])  # duplicate on purpose
    assert [i.path for i in infos] == [busy, quiet]
    assert infos[0].count == 1 and infos[0].newest >= infos[1].newest


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


def test_main_auto_discovers_claude_only_layout_with_no_saved_config(tmp_path, monkeypatch, capsys):
    """Regression: main() used to seed its default-root discovery from the Codex-only
    discover_roots(candidate_roots()), so a machine with Claude transcripts but no Codex sessions
    dir and no saved config resolved `root` to None instead of the Claude root."""
    claude_root = tmp_path / "projects"
    write_jsonl(claude_root / "slug" / "11111111-2222-3333-4444-555555555555.jsonl",
               [assistant_text("hi")])
    monkeypatch.setattr(cht, "candidate_roots", lambda *a, **k: [])          # no Codex sessions dir
    monkeypatch.setattr(cht, "claude_candidate_roots", lambda *a, **k: [claude_root])
    monkeypatch.setattr(cht, "DEFAULT_CONFIG", tmp_path / "config.json")     # nothing saved
    monkeypatch.setattr("sys.argv", ["codex-human-transcript", "--list-roots"])
    rc = cht.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "No Codex or Claude session roots found" not in out
    assert str(claude_root) in out
    assert out.strip().startswith("▶")   # the Claude root was chosen as the default `root`


def test_main_dashboard_uses_cli_root_not_saved_config(tmp_path, monkeypatch):
    """Regression: interactive_dashboard used to re-derive its root list from the saved config
    (load_roots(load_config(config_path))) instead of using the roots main() already resolved
    from the CLI. With root A saved in config, launching --root B must show/mirror B, not A."""
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()
    config_path = tmp_path / "config.json"
    cht.save_config(config_path, {**cht.load_config(config_path),
                                  **cht.roots_payload([cht.RootSel("codex", root_a, True)])})
    monkeypatch.setattr(cht, "DEFAULT_CONFIG", config_path)
    monkeypatch.setattr(cht, "discover_all_roots", lambda *a, **k: [])
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    captured: dict[str, Any] = {}

    def fake_wrapper(func, *args, **kwargs):
        captured["args"] = args

    monkeypatch.setattr(cht.curses, "wrapper", fake_wrapper)
    monkeypatch.setattr("sys.argv", ["codex-human-transcript", "--root", str(root_b), "--format", "codex"])

    rc = cht.main()
    assert rc == 0
    assert "args" in captured, "curses.wrapper was never invoked"
    # Find the list[RootSel]-shaped argument among what main() passed to the dashboard.
    root_lists = [a for a in captured["args"] if isinstance(a, list)]
    assert root_lists, "interactive_dashboard was not given a resolved roots list"
    resolved_paths = [sel.path for sel in root_lists[0]]
    assert resolved_paths == [root_b], (
        f"dashboard root list {resolved_paths} should be [{root_b}] (the CLI --root), "
        f"not the config-saved {root_a}"
    )


def test_config_roundtrip_and_missing_file(tmp_path):
    cfg = tmp_path / "cfg" / "config.json"
    assert cht.load_config(cfg) == {}
    cht.save_config(cfg, {"root": "/some/where"})
    assert cht.load_config(cfg) == {"root": "/some/where"}


def test_load_config_tolerates_corrupt_file(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json")
    assert cht.load_config(cfg) == {}


def test_resolve_root_prefers_cli_then_config_then_discovered(tmp_path):
    cli, saved, found = tmp_path / "cli", tmp_path / "saved", tmp_path / "found"
    for p in (cli, saved, found):
        p.mkdir()
    infos = [cht.RootInfo("codex", found, 1, 0.0)]
    assert cht.resolve_root(cli, {"root": str(saved)}, infos) == cli
    assert cht.resolve_root(None, {"root": str(saved)}, infos) == saved
    assert cht.resolve_root(None, {"root": str(tmp_path / "gone")}, infos) == found
    assert cht.resolve_root(None, {}, []) is None


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


def test_recent_rollouts_limits_scan_to_latest_day_dirs(tmp_path):
    root = tmp_path / "sessions"
    for day in range(1, 11):
        write_rollout(root / f"2026/08/{day:02d}/rollout-{day}.jsonl", [meta()])
    names = {p.name for p in cht.recent_rollouts(root, days=2)}
    assert names == {"rollout-10.jsonl", "rollout-9.jsonl"}


def test_recent_rollouts_falls_back_to_full_scan_for_flat_layout(tmp_path):
    root = tmp_path / "sessions"
    for i in range(3):
        write_rollout(root / f"rollout-{i}.jsonl", [meta()])
    assert len(cht.recent_rollouts(root, days=1)) == 3


def test_newest_transcript_ignores_files_that_vanish_mid_scan(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    keep = write_rollout(root / "rollout-keep.jsonl", [meta()])
    ghost = write_rollout(root / "rollout-ghost.jsonl", [meta()])
    real_stat = Path.stat

    def flaky_stat(self, *a, **k):
        if self.name == ghost.name:
            raise FileNotFoundError(self)
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert cht.newest_transcript(root) == keep


# ── session_meta shapes seen in the wild ────────────────────────────────────

@pytest.mark.parametrize("source, expected", [
    ({"subagent": {"thread_spawn": {"agent_nickname": "Raman", "agent_role": "worker"}}}, ("Raman", "worker")),
    ({"subagent": "review"}, ("unnamed", "review")),
    ({"subagent": {"other": {}}}, ("unnamed", "other")),
    ("exec", None),
])
def test_subagent_identity_handles_every_source_shape(source, expected):
    meta = {"thread_source": "subagent" if source != "exec" else "user", "source": source}
    assert cht.subagent_identity(meta) == expected


def test_discover_sessions_does_not_crash_on_review_subagent(tmp_path):
    root = tmp_path / "sessions"
    write_rollout(root / "rollout-main.jsonl", [meta()])
    write_rollout(root / "rollout-review.jsonl", [meta("subagent", source={"subagent": "review"})])
    names = {(name, role) for name, role, _ in cht.discover_sessions(root)}
    assert ("unnamed", "review") in names


def test_mirror_set_reports_only_freshly_built_logs(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-h.jsonl",
                        [meta(), rec("agent_message", top="event_msg", message="x")])
    sessions = [("main", "session", src)]
    expected = tmp_path / "logs" / f"{cht.source_key('codex', src.parent, src.stem)}.log"
    assert cht.MirrorSet(tmp_path / "logs").sync(sessions) == [expected]
    assert cht.MirrorSet(tmp_path / "logs").sync(sessions) == []  # restart → resumed, not rebuilt


def test_render_tool_output_accepts_list_shaped_output():
    out = plain(cht.render(rec("custom_tool_call_output", output=[
        {"type": "input_text", "text": "Script completed\nWall time 0.3 seconds\nOutput:\n"},
        {"type": "input_text", "text": "real content here"},
    ])))
    assert "real content here" in out
    assert '"type"' not in out


def test_cli_survives_broken_pipe(tmp_path):
    import subprocess
    src = write_rollout(tmp_path / "rollout-pipe.jsonl",
                        [meta()] + [rec("agent_message", top="event_msg", message=f"line {i} " * 50)
                                    for i in range(300)])
    proc = subprocess.run(f"{SCRIPT} --file {src} --once --plain --no-color | head -1",
                          shell=True, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    assert "Traceback" not in proc.stderr and "BrokenPipe" not in proc.stderr


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


def test_tail_committed_stays_before_a_chunk_ending_mid_multibyte_char(tmp_path):
    path = tmp_path / "rollout-t4.jsonl"
    # second record is mid-write and cut off one byte into a two-byte UTF-8 sequence (0xc3 0xa9 'e').
    data = b'{"a":1}\n' + b'{"b":"' + b"x" * 20 + b"\xc3"
    path.write_bytes(data)
    tail = cht.NdjsonTail(path)
    result = tail.read()
    assert len(result.records) == 1
    assert tail.committed == 8            # end of the first, complete record only
    assert tail.offset == len(data)


def test_end_to_end_restart_does_not_lose_a_record_split_mid_multibyte_char(tmp_path):
    log_dir = tmp_path / "logs"
    src = tmp_path / "rollout-t5.jsonl"
    first_line = json.dumps(rec("agent_message", top="event_msg", message="one")) + "\n"
    second_full = json.dumps(rec("agent_message", top="event_msg",
                                  message="café " * 5 + "END_MARKER_XYZ"),
                              ensure_ascii=False).encode("utf-8")
    cut = second_full.index("é".encode()) + 1     # mid-way through the 2-byte 'é' sequence
    src.write_bytes(first_line.encode("utf-8") + second_full[:cut])

    log = cht.TextLog(log_dir, src)
    log.sync()
    assert log.tail.committed == len(first_line.encode("utf-8"))   # never inside record 2

    # simulate a mirror restart: a fresh TextLog resuming from the persisted checkpoint.
    resumed = cht.TextLog(log_dir, src)
    assert resumed.tail.offset == len(first_line.encode("utf-8"))

    # the source finishes writing the record that was mid-write at restart time.
    with src.open("ab") as handle:
        handle.write(second_full[cut:] + b"\n")
    resumed.sync()

    body = resumed.path.read_text(encoding="utf-8")
    assert body.count("END_MARKER_XYZ") == 1


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


def test_follow_once_renders_a_bare_claude_file_without_a_root(tmp_path, capsys):
    path = tmp_path / "projects" / "slug" / "11111111-2222-3333-4444-555555555555.jsonl"
    write_jsonl(path, [assistant_text("hello from claude")])
    cht.follow(path, None, once=True, poll=0.01)
    out = plain(capsys.readouterr().out)
    assert "hello from claude" in out
    assert "CLAUDE" in out


def test_as_ref_detects_claude_format_from_a_bare_path(tmp_path):
    path = tmp_path / "projects" / "slug" / "11111111-2222-3333-4444-555555555555.jsonl"
    write_jsonl(path, [assistant_text("hi")])
    assert cht.as_ref(path).fmt is cht.CLAUDE


# ── recursive, key-aware redaction ──────────────────────────────────────────

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
    expected_key = cht.source_key("codex", src.parent, src.stem)
    text = (tmp_path / "logs" / f"{expected_key}.log").read_text(encoding="utf-8")
    assert "sk-live-987" not in text
    assert "prod" in text


# ── per-source failure isolation ────────────────────────────────────────────

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
    good_key = cht.source_key("codex", good.parent, good.stem)
    assert (tmp_path / "logs" / f"{good_key}.log").read_text().count("good one") == 1


def test_backfill_window_extends_past_one_huge_final_record(tmp_path):
    path = tmp_path / "rollout-b1.jsonl"
    big = json.dumps(rec("agent_message", top="event_msg", message="w" * 600_000)) + "\n"
    path.write_text(big, encoding="utf-8")
    cache = cht.TranscriptCache(path)
    cache.update()
    assert cache.lines, "a single 600 KB record must still render under the display backfill"


# ── SessionRef, Format, and the Codex adapter ───────────────────────────────

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
    assert log.path.name == f"{cht.source_key('codex', src.parent, src.stem)}.log"


# ── collision-free mirror identity ──────────────────────────────────────────

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


def test_source_key_is_injective_for_ids_that_sanitize_alike(tmp_path):
    # "a/b" and "ab" both strip down to the same safe characters; the key must still differ.
    a = cht.source_key("codex", tmp_path, "a/b")
    b = cht.source_key("codex", tmp_path, "ab")
    assert a != b


def test_mirror_preserves_both_logs_for_ids_that_sanitize_alike(tmp_path):
    # "a/b" and "ab" strip down to the same safe characters but must not collapse onto
    # one mirror log (nor share a checkpoint, which would let one erase the other).
    root = tmp_path / "sessions"
    src_a = write_rollout(root / "rollout-slash.jsonl",
                          [meta(), rec("agent_message", top="event_msg", message="from slash id")])
    src_b = write_rollout(root / "rollout-plain.jsonl",
                          [meta(), rec("agent_message", top="event_msg", message="from plain id")])
    ref_a = cht.SessionRef(cht.CODEX, src_a, "main", "a/b", "a/b", None, "a/b", "session",
                           None, 0.0, cht.source_key("codex", root, "a/b"))
    ref_b = cht.SessionRef(cht.CODEX, src_b, "main", "ab", "ab", None, "ab", "session",
                           None, 0.0, cht.source_key("codex", root, "ab"))
    log_dir = tmp_path / "logs"
    mirrors = cht.MirrorSet(log_dir)
    mirrors.sync([ref_a, ref_b])
    logs = sorted(log_dir.glob("*.log"))
    bodies = {p.name: p.read_text(encoding="utf-8") for p in logs}
    assert any("from slash id" in b for b in bodies.values())
    assert any("from plain id" in b for b in bodies.values())
    # the two sources must not have collapsed onto one mirror log.
    assert sum("from slash id" in b for b in bodies.values()) == 1
    assert sum("from plain id" in b for b in bodies.values()) == 1


def test_source_key_sanitizes_traversal_in_owner_and_agent_id(tmp_path):
    evil_owner = "x/../../../../../../tmp/pwned"
    key = cht.source_key("codex", tmp_path, evil_owner, "../../etc/evil")
    assert "/" not in key
    log_dir = tmp_path / "logs"
    resolved = (log_dir / f"{key}.log").resolve()
    assert log_dir.resolve() in resolved.parents


def test_textlog_refuses_to_escape_log_dir_for_malicious_source_key(tmp_path):
    src = write_rollout(tmp_path / "sessions" / "rollout-evil.jsonl",
                        [meta(), rec("agent_message", top="event_msg", message="hi")])
    log_dir = tmp_path / "logs"
    malicious_key = "../../../../../../tmp/pwned"
    ref = cht.SessionRef(cht.CODEX, src, "subagent", "s", "s", "agent-x", "s", "worker",
                         None, 0.0, malicious_key)
    with pytest.raises(OSError):
        cht.TextLog(log_dir, ref)
    assert not (tmp_path / "tmp").exists()


def test_same_stem_codex_mains_from_different_roots_get_different_logs_when_neither_preexists(tmp_path):
    log_dir = tmp_path / "logs"
    src_a = write_rollout(tmp_path / "rootA" / "rollout-dup.jsonl",
                          [meta(), rec("agent_message", top="event_msg", message="root a")])
    src_b = write_rollout(tmp_path / "rootB" / "rollout-dup.jsonl",
                          [meta(), rec("agent_message", top="event_msg", message="root b")])
    ref_a = cht.SessionRef(cht.CODEX, src_a, "main", "dup", "dup", None, "dup", "session",
                           None, 0.0, cht.source_key("codex", tmp_path / "rootA", "dup"))
    ref_b = cht.SessionRef(cht.CODEX, src_b, "main", "dup", "dup", None, "dup", "session",
                           None, 0.0, cht.source_key("codex", tmp_path / "rootB", "dup"))
    log_a = cht.TextLog(log_dir, ref_a)
    log_b = cht.TextLog(log_dir, ref_b)
    assert log_a.path != log_b.path
    log_a.sync()
    log_b.sync()
    assert "root a" in log_a.path.read_text(encoding="utf-8")
    assert "root b" in log_b.path.read_text(encoding="utf-8")


def test_preexisting_codex_stem_log_is_still_reused(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "rollout-dup2.log").write_text("═══ old header ═══\n\n", encoding="utf-8")
    src = write_rollout(tmp_path / "rootA" / "rollout-dup2.jsonl",
                        [meta(), rec("agent_message", top="event_msg", message="reused")])
    ref = cht.SessionRef(cht.CODEX, src, "main", "dup2", "dup2", None, "dup2", "session",
                         None, 0.0, cht.source_key("codex", tmp_path / "rootA", "dup2"))
    log = cht.TextLog(log_dir, ref)
    assert log.path.name == "rollout-dup2.log"


# ── format detection ─────────────────────────────────────────────────────

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


# ── Claude roots and discovery ───────────────────────────────────────────────

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
    write_jsonl(root / "slug" / session / "subagents" / "agent-new.jsonl", [assistant_text("new")])
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


# ── four-state session status ────────────────────────────────────────────────

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


# ── Claude rendering ─────────────────────────────────────────────────────────

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


# ── thinking as a display-only preference ────────────────────────────────

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


# ── dashboard status freshness ───────────────────────────────────────────────

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


# ── history fidelity on real resumed and compacted sessions ─────────────────

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


# ── spec deviations in Claude handling ───────────────────────────────────────

def test_claude_transcript_cache_does_not_dedupe_genuinely_distinct_records(tmp_path):
    """Deduper keys on head-without-timestamp + body, so two real Bash: ls runs render alike.

    Spec §3.5: `Deduper` stays Codex-only. Claude's Format is built with dedupe=False, but
    TranscriptCache.update() calls self.dedupe.fresh() unconditionally, so the second, genuinely
    distinct record is silently dropped.
    """
    bash_use = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
    path = write_jsonl(tmp_path / "dd1.jsonl", [
        claude_rec("assistant", message={"role": "assistant", "content": [bash_use]}),
        claude_rec("assistant", message={"role": "assistant", "content": [bash_use]}),
    ])
    ref = cht.SessionRef(cht.CLAUDE, path, "main", "dd1", "dd1", None, "dd1", "session", None, 0.0, "kdd1")
    cache = cht.TranscriptCache(ref)
    cache.update()
    body = "\n".join(cht.strip_ansi(line) for line in cache.lines)
    assert body.count("EXEC") == 2, "both Bash: ls calls must render; dedupe must not apply to Claude"


def test_claude_text_log_sync_does_not_dedupe_genuinely_distinct_records(tmp_path):
    bash_use = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
    path = write_jsonl(tmp_path / "dd2.jsonl", [
        claude_rec("assistant", message={"role": "assistant", "content": [bash_use]}),
        claude_rec("assistant", message={"role": "assistant", "content": [bash_use]}),
    ])
    ref = cht.SessionRef(cht.CLAUDE, path, "main", "dd2", "dd2", None, "dd2", "session", None, 0.0, "kdd2")
    log = cht.TextLog(tmp_path / "logs", ref)
    log.sync()
    body = log.path.read_text(encoding="utf-8")
    assert body.count("EXEC") == 2, "both Bash: ls calls must be mirrored; dedupe must not apply to Claude"


def test_claude_render_shows_every_tool_result_in_a_parallel_tool_call_record():
    """A single user record can carry one tool_result per parallel tool_use call (spec §3.5)."""
    record = claude_rec("user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "first output"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "second output"},
    ]})
    out = claude_plain(record)
    assert "first output" in out and "second output" in out


def test_detect_format_recognizes_a_claude_file_containing_only_file_history_records(tmp_path):
    """CLAUDE_BOOKKEEPING hardcodes only file-history-snapshot/-delta; §3.5 skips the whole family.

    detect_format() sniffs content when the path itself gives no signature (a plain filename, not
    under a `projects/<uuid>` or `subagents/agent-*` layout). A file containing only some other
    file-history-* variant (e.g. `file-history-restore`) is real Claude bookkeeping, but the
    hardcoded two-literal set fails to recognize it, so the whole file goes undiscovered.
    """
    path = write_jsonl(tmp_path / "plain.jsonl", [claude_rec("file-history-restore")])
    assert cht.detect_format(path) is cht.FORMATS["claude"]


def test_claude_advance_maps_a_failure_stop_reason_to_error():
    """Spec §3.3: error is 'an API-error record or stop_reason indicating failure'."""
    record = claude_rec("assistant", message={"role": "assistant", "stop_reason": "max_tokens",
                                               "content": [{"type": "text", "text": "truncated..."}]})
    assert cht.claude_advance("working", record) == "error"


# ── follow()/newest_transcript must not be Codex-only ───────────────────────

def test_newest_transcript_finds_a_claude_session(tmp_path, capsys):
    root = tmp_path / "projects" / "proj"
    write_jsonl(root / "11111111-2222-3333-4444-555555555555.jsonl",
                [claude_rec("user", message={"role": "user", "content": [{"type": "text", "text": "hi"}]})])
    assert cht.newest_transcript(tmp_path / "projects") is not None


def test_follow_does_not_dedupe_repeated_claude_blocks(tmp_path, capsys):
    """Two genuinely distinct Claude turns that render alike must both appear."""
    path = write_jsonl(tmp_path / "proj" / "22222222-2222-3333-4444-555555555555.jsonl",
                       [assistant_text("same answer", ts="2026-09-17T06:00:00.000Z"),
                        assistant_text("same answer", ts="2026-09-17T06:05:00.000Z")])
    cht.follow(path, None, True, 0.01, None)
    assert plain(capsys.readouterr().out).count("same answer") == 2


def test_claude_render_skips_a_record_with_a_wrong_typed_discriminator():
    """Defect A: a list `type` must not crash `is_claude_bookkeeping`'s set membership check."""
    assert cht.claude_render({"type": []}, {"thinking": True}) is None


def test_claude_advance_ignores_a_record_with_a_wrong_typed_discriminator():
    assert cht.claude_advance(cht.STATE_UNKNOWN, {"type": []}) == cht.STATE_UNKNOWN


def test_claude_advance_ignores_an_assistant_record_with_a_wrong_typed_message():
    record = claude_rec("assistant", message="bad")
    assert cht.claude_advance(cht.STATE_UNKNOWN, record) == cht.STATE_UNKNOWN


def test_claude_render_skips_an_assistant_record_with_a_wrong_typed_message():
    record = claude_rec("assistant", message="bad")
    assert cht.claude_render(record, {"thinking": True}) is None


def test_one_malformed_claude_record_never_stops_the_rest_of_the_file(tmp_path):
    """Defect A: one bad record in a transcript must not take down the whole reader — the
    records before and after it still render."""
    path = write_jsonl(tmp_path / "proj" / "33333333-2222-3333-4444-555555555555.jsonl",
                       [assistant_text("first message"),
                        {"type": [], "sessionId": "s1", "timestamp": "2026-09-17T06:01:00.000Z"},
                        claude_rec("assistant", message="bad"),
                        assistant_text("last message", ts="2026-09-17T06:02:00.000Z")])
    lines = path.read_text(encoding="utf-8").splitlines()
    texts = [cht.claude_render(r, {"thinking": True}) for r in cht.records(lines)]
    plain_texts = [cht.strip_ansi(t) for t in texts if t]
    assert any("first message" in t for t in plain_texts)
    assert any("last message" in t for t in plain_texts)


def test_redact_masks_a_secret_buried_past_the_depth_limit():
    """Defect B: at the depth limit `redact` must substitute a placeholder, never hand back the
    unredacted subtree verbatim."""
    secret = "sk-proj-SUPERSECRETVALUE1234567890abcdef"
    nested = {"api_key": secret}
    for _ in range(30):
        nested = {"x": nested}
    out = cht.redact(nested)
    assert secret not in json.dumps(out)


def test_mirror_covers_both_roots_when_nothing_is_configured(tmp_path, monkeypatch):
    """Regression: main() built its root selection from the single newest root, so with both a
    Codex and a Claude root present the tool mirrored only whichever happened to be newest —
    nondeterministically — instead of the merged list across both."""
    codex_root = tmp_path / "sessions"
    write_rollout(codex_root / "2026" / "09" / "17" / "rollout-2026-09-17T06-00-00-abc.jsonl",
                  [meta(id="session-abc"), rec("agent_message", top="event_msg", message="from codex")])
    claude_root = tmp_path / "projects"
    write_jsonl(claude_root / "slug" / "11111111-2222-3333-4444-555555555555.jsonl",
                [assistant_text("from claude")])
    monkeypatch.setattr(cht, "candidate_roots", lambda *a, **k: [codex_root])
    monkeypatch.setattr(cht, "claude_candidate_roots", lambda *a, **k: [claude_root])
    monkeypatch.setattr(cht, "DEFAULT_CONFIG", tmp_path / "config.json")
    log_dir = tmp_path / "logs"
    monkeypatch.setattr("sys.argv", ["codex-human-transcript", "--mirror", "--once",
                                     "--log-dir", str(log_dir)])
    assert cht.main() == 0
    names = [p.name for p in log_dir.glob("*.log")]
    bodies = "\n".join(p.read_text(encoding="utf-8") for p in log_dir.glob("*.log"))
    assert any(n.startswith("codex-") or n.startswith("rollout-") for n in names), names
    assert any(n.startswith("claude-") for n in names), names
    assert "from codex" in bodies and "from claude" in bodies


def test_mirror_once_does_not_truncate_a_transcript_larger_than_one_read_chunk(tmp_path):
    """Regression: TextLog.sync() does one bounded NdjsonTail.read() (READ_CHUNK_BYTES) and
    ignores `more_pending`. That is correct for live polling (the next poll continues), but
    `mirror(..., once=True)` syncs exactly once and exits successfully, so a transcript larger
    than READ_CHUNK_BYTES was mirrored with its tail silently missing."""
    root = tmp_path / "sessions"
    big_records = [rec("agent_message", top="event_msg", message=f"msg{i} " + "z" * 3000)
                   for i in range(400)]                  # well past READ_CHUNK_BYTES (1 MiB)
    src = write_rollout(root / "2026/09/17/rollout-2026-09-17T08-00-00-dddd.jsonl",
                        [meta(id="session-dddd"), *big_records,
                         rec("agent_message", top="event_msg", message="FINAL_TAIL_MARKER_XYZ")])
    assert src.stat().st_size > cht.READ_CHUNK_BYTES
    log_dir = tmp_path / "logs"
    cht.mirror(root, log_dir, poll=0.01, once=True)
    bodies = "\n".join(p.read_text(encoding="utf-8") for p in log_dir.glob("*.log"))
    assert "FINAL_TAIL_MARKER_XYZ" in bodies


# ── token usage: counters, ledger, display ──────────────────────────────────

def codex_tokens(input_tokens: int, cached: int, output: int, reasoning: int = 0, window: int = 258400):
    return rec("token_count", top="event_msg",
               info={"total_token_usage": {"input_tokens": input_tokens, "cached_input_tokens": cached,
                                           "cache_write_input_tokens": 0, "output_tokens": output,
                                           "reasoning_output_tokens": reasoning,
                                           "total_tokens": input_tokens + output},
                     "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 900,
                                          "cache_write_input_tokens": 0, "output_tokens": 50,
                                          "reasoning_output_tokens": 0, "total_tokens": 1050},
                     "model_context_window": window})


def claude_assistant(mid: str, blocks: list[dict], usage: dict, model: str = "claude-fable-5-1"):
    return claude_rec("assistant", message={"role": "assistant", "id": mid, "model": model,
                                            "stop_reason": None, "content": blocks, "usage": usage})


CLAUDE_USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 3000,
                "output_tokens": 40, "output_tokens_details": {"thinking_tokens": 15}}


def test_codex_usage_is_last_wins_and_normalizes_fresh_input():
    counter = cht.UsageCounter(cht.CODEX)
    counter.feed(rec("turn_context", model="gpt-6-astra"))
    counter.feed(codex_tokens(5000, 4000, 100, reasoning=20))
    counter.feed(codex_tokens(9000, 8500, 300, reasoning=60))
    usage = counter.snapshot()
    assert (usage.input, usage.cache_read, usage.output, usage.thinking) == (500, 8500, 300, 60)
    assert usage.total == 9300
    assert usage.requests == 2
    assert usage.model == "gpt-6-astra"
    assert usage.context_window == 258400
    assert usage.last_context == 1000


def test_codex_usage_counts_tool_calls_and_nonzero_exits_once():
    counter = cht.UsageCounter(cht.CODEX)
    counter.feed(rec("function_call", name="exec_command", call_id="c1", arguments="{}"))
    counter.feed(rec("function_call_output", call_id="c1",
                     output="Wall time: 0.1 seconds\nProcess exited with code 127\nOutput:\nnope"))
    counter.feed(rec("item_completed", top="event_msg",
                     item={"type": "CommandExecution", "exit_code": 127, "status": "failed"}))
    counter.feed(rec("custom_tool_call", name="apply_patch", call_id="c2", input="x"))
    counter.feed(rec("custom_tool_call_output", call_id="c2",
                     output=[{"type": "input_text", "text": "Process exited with code 0\nOutput:\nok"}]))
    counter.feed(rec("patch_apply_end", top="event_msg", success=False, stdout="", stderr="conflict"))
    usage = counter.snapshot()
    assert usage.tool_calls == 2
    assert usage.tool_errors == 2


def test_claude_usage_sums_each_message_once_despite_repeated_records():
    counter = cht.UsageCounter(cht.CLAUDE)
    counter.feed(claude_assistant("m1", [{"type": "thinking", "thinking": "hm"}], CLAUDE_USAGE))
    counter.feed(claude_assistant("m1", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
                                  CLAUDE_USAGE))
    counter.feed(claude_rec("user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "boom"}]}))
    counter.feed(claude_assistant("m2", [{"type": "text", "text": "done"}], CLAUDE_USAGE))
    usage = counter.snapshot()
    assert (usage.input, usage.cache_write, usage.cache_read, usage.output, usage.thinking) == (20, 400, 6000, 80, 30)
    assert usage.requests == 2
    assert (usage.tool_calls, usage.tool_errors) == (1, 1)
    assert usage.model == "claude-fable-5-1"
    assert usage.last_context == 3210
    assert usage.context_window is None


def test_usage_counter_state_round_trips_and_keeps_deduping_after_restore():
    counter = cht.UsageCounter(cht.CLAUDE)
    counter.feed(claude_assistant("m1", [{"type": "text", "text": "a"}], CLAUDE_USAGE))
    restored = cht.UsageCounter.restore(cht.CLAUDE, json.loads(json.dumps(counter.state())))
    restored.feed(claude_assistant("m1", [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}],
                                   CLAUDE_USAGE))
    assert restored.snapshot().output == 40
    assert restored.snapshot().tool_calls == 1
    assert cht.UsageCounter.restore(cht.CLAUDE, {"garbage": True}).snapshot() == cht.Usage.empty()


def test_format_tokens_abbreviates():
    assert [cht.format_tokens(n) for n in (0, 950, 12_300, 999_949, 1_234_567)] == ["0", "950", "12k", "1.0M", "1.2M"]


def test_usage_line_shows_totals_cache_share_tools_and_errors():
    usage = cht.Usage.empty()._replace(input=200_000, cache_read=1_000_000, output=12_000,
                                       tool_calls=41, tool_errors=3, last_context=146_000,
                                       context_window=258_400)
    line = cht.usage_line(usage)
    assert "↑1.2M" in line and "⇄83%" in line and "↓12k" in line
    assert "41 tools" in line and "3✘" in line and "ctx 146k/258k" in line
    assert cht.usage_line(cht.Usage.empty()) == ""


def test_ledger_writes_one_json_per_session_and_skips_unchanged(tmp_path):
    ref = cht.as_ref(write_rollout(tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl", [meta(cwd="/w")]))
    ledger = cht.Ledger(tmp_path / "ledger")
    usage = cht.Usage.empty()._replace(output=5, requests=1, model="gpt-6-astra")
    assert ledger.record(ref, usage) is True
    entry = json.loads((tmp_path / "ledger" / f"{ref.source_key}.json").read_text())
    assert entry["output"] == 5 and entry["total"] == 5 and entry["format"] == "codex"
    assert entry["session_id"] == "abc" and entry["model"] == "gpt-6-astra" and entry["cwd"] == "/w"
    assert entry["transcript"] == str(ref.path) and entry["v"] == cht.LEDGER_VERSION
    assert ledger.record(ref, usage) is False
    assert ledger.record(ref, usage._replace(output=6)) is True
    with pytest.raises(OSError):
        ledger.record(ref._replace(source_key="../escape"), usage)


def test_cache_usage_is_exact_even_when_backfill_skips_the_start(tmp_path, monkeypatch):
    monkeypatch.setattr(cht, "BACKFILL_BYTES", 2048)
    filler = [claude_rec("user", message={"role": "user", "content": [{"type": "text", "text": "x" * 500}]})
              for _ in range(8)]
    early = claude_assistant("m1", [{"type": "text", "text": "first"}], CLAUDE_USAGE)
    late = claude_assistant("m2", [{"type": "text", "text": "last"}], CLAUDE_USAGE)
    path = write_jsonl(tmp_path / "projects" / "p" / "11111111-2222-3333-4444-555555555555.jsonl",
                       [early, *filler, late])
    cache = cht.TranscriptCache(cht.as_ref(path))
    cache.update()
    assert cache.usage.requests == 2
    assert cache.usage.output == 80
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(claude_assistant("m3", [{"type": "text", "text": "more"}], CLAUDE_USAGE)) + "\n")
    cache.update()
    assert cache.usage.requests == 3


def test_text_log_restores_usage_from_checkpoint_on_resume(tmp_path):
    path = write_rollout(tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl",
                         [meta(), codex_tokens(100, 0, 10)])
    log = cht.TextLog(tmp_path / "logs", path)
    log.sync()
    assert log.usage.output == 10
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(codex_tokens(200, 0, 25)) + "\n")
    resumed = cht.TextLog(tmp_path / "logs", path)
    assert resumed.usage.output == 10
    resumed.sync()
    assert resumed.usage.output == 25 and resumed.usage.requests == 2


def test_mirror_once_records_usage_in_the_ledger(tmp_path):
    root = tmp_path / "sessions"
    write_rollout(root / "2026" / "08" / "20" / "rollout-2026-08-20T10-00-00-abc.jsonl",
                  [meta(), codex_tokens(100, 50, 10)])
    ledger_dir = tmp_path / "ledger"
    cht.mirror(root, tmp_path / "logs", poll=0.01, once=True, ledger_dir=ledger_dir)
    entries = list(ledger_dir.glob("*.json"))
    assert len(entries) == 1
    assert json.loads(entries[0].read_text())["cache_read"] == 50


def test_follow_once_prints_usage_summary_and_records_ledger(tmp_path, capsys):
    path = write_rollout(tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl",
                         [meta(), rec("function_call", name="exec_command", call_id="c1", arguments="{}"),
                          codex_tokens(100_000, 60_000, 2_500)])
    cht.follow(path, None, once=True, poll=0.01, ledger_dir=tmp_path / "ledger")
    out = capsys.readouterr().out
    assert "Σ ↑100k ⇄60% ↓2k · 1 req · 1 tools" in out
    assert len(list((tmp_path / "ledger").glob("*.json"))) == 1


# ── review follow-ups: checkpoint upgrade, restore hygiene, backfill edge, ledger safety ──

def test_text_log_backfills_usage_for_a_checkpoint_written_before_usage_existed(tmp_path):
    path = write_rollout(tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl",
                         [meta(), codex_tokens(100, 0, 10), codex_tokens(200, 0, 25)])
    log = cht.TextLog(tmp_path / "logs", path)
    log.sync()
    saved = json.loads(log.offset_path.read_text())
    del saved["usage"]                                  # a v2 checkpoint from before this feature
    log.offset_path.write_text(json.dumps(saved))
    resumed = cht.TextLog(tmp_path / "logs", path)
    assert resumed.usage.output == 25 and resumed.usage.requests == 2
    resumed.sync()
    assert resumed.usage.requests == 2                  # the offset was honoured: nothing re-counted


def test_usage_counter_survives_malformed_restored_fields():
    counter = cht.UsageCounter.restore(cht.CLAUDE, {"recent_ids": 1, "output": "lots", "model": 7})
    counter.feed(claude_assistant("m1", [{"type": "text", "text": "a"}], CLAUDE_USAGE))
    assert counter.snapshot().output == 40
    assert counter.snapshot().model == "claude-fable-5-1"


def test_claude_usage_dedupes_message_ids_per_session_not_globally():
    counter = cht.UsageCounter(cht.CLAUDE)
    counter.feed(claude_assistant("m1", [{"type": "text", "text": "a"}], CLAUDE_USAGE))
    other = claude_assistant("m1", [{"type": "text", "text": "b"}], CLAUDE_USAGE)
    counter.feed({**other, "sessionId": "s2"})
    assert counter.snapshot().requests == 2


def test_cache_scans_the_prefix_when_the_file_only_grows_after_the_first_empty_read(tmp_path, monkeypatch):
    monkeypatch.setattr(cht, "BACKFILL_BYTES", 2048)
    path = write_jsonl(tmp_path / "projects" / "p" / "11111111-2222-3333-4444-555555555555.jsonl", [])
    cache = cht.TranscriptCache(cht.as_ref(path))
    cache.update()
    filler = [claude_rec("user", message={"role": "user", "content": [{"type": "text", "text": "x" * 500}]})
              for _ in range(8)]
    write_jsonl(path, [claude_assistant("m1", [{"type": "text", "text": "first"}], CLAUDE_USAGE), *filler,
                       claude_assistant("m2", [{"type": "text", "text": "last"}], CLAUDE_USAGE)])
    cache.update()
    assert cache.usage.requests == 2


def test_ledger_never_follows_a_planted_symlink_and_preloads_previous_entries(tmp_path):
    ref = cht.as_ref(write_rollout(tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl", [meta()]))
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    (ledger_dir / f"{ref.source_key}.json.tmp").symlink_to(victim)
    ledger = cht.Ledger(ledger_dir)
    usage = cht.Usage.empty()._replace(output=9, requests=1)
    assert ledger.record(ref, usage) is True
    assert victim.read_text() == "keep me"
    assert [p.name for p in ledger_dir.iterdir() if p.suffix != ".json"] == [f"{ref.source_key}.json.tmp"]
    assert cht.Ledger(ledger_dir).known(ref.source_key) == usage


def test_drain_stops_when_the_tail_makes_no_progress(tmp_path):
    """A source that keeps claiming more_pending without advancing must not spin forever:
    transcripts are untrusted and a concurrent truncation can produce exactly that."""
    src = write_rollout(tmp_path / "sessions" / "2026/09/17" / "rollout-2026-09-17T06-00-00-a.jsonl",
                        [meta(id="a"), rec("agent_message", top="event_msg", message="hi")])
    log = cht.TextLog(tmp_path / "logs", src)

    class StuckTail:
        offset = committed = 0
        def __init__(self): self.calls = 0
        def read(self, *a, **k):
            self.calls += 1
            return cht.TailRead(truncated=False, records=[], more_pending=True)

    log.tail = stuck = StuckTail()
    log.sync(drain=True)                      # must return rather than hang
    assert stuck.calls < 100, f"drain looped {stuck.calls} times without progress"


# ── session status: Codex DB truth, freshness, local commands, names, activity filter ──

def codex_home(tmp_path: Path, turns: list[tuple], threads: list[tuple] = ()) -> Path:
    """A fake $CODEX_HOME: sessions/ plus the two sqlite files Codex keeps beside it."""
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    with sqlite3.connect(home / "thread_history_1.sqlite") as con:
        con.execute("create table thread_turns (thread_id text, turn_id text, rollout_ordinal int, status text,"
                    " started_at int, completed_at int)")
        con.executemany("insert into thread_turns values (?,?,?,?,?,?)", turns)
    with sqlite3.connect(home / "state_5.sqlite") as con:
        con.execute("create table threads (id text primary key, rollout_path text, name text, title text,"
                    " agent_nickname text, agent_role text, thread_source text, updated_at_ms int)")
        con.executemany("insert into threads values (?,?,?,?,?,?,?,?)", threads)
    return home


def markerless_rollout(home: Path, thread_id: str, stamp: str = "2026-09-24T10-00-00") -> Path:
    return write_rollout(home / "sessions" / "2026" / "09" / "24" / f"rollout-{stamp}-{thread_id}.jsonl",
                         [meta(id=thread_id), rec("agent_message", top="event_msg", message="hi")])


def test_codex_status_comes_from_the_turn_table_when_the_scan_cannot_tell(tmp_path):
    home = codex_home(tmp_path, [("t-live", "u1", 1, "inProgress", 100, None),
                                 ("t-done", "u1", 1, "completed", 100, 200),
                                 ("t-done", "u2", 2, "inProgress", 300, None),
                                 ("t-fail", "u1", 1, "failed", 100, 200),
                                 ("t-stop", "u1", 1, "interrupted", 100, 200)])
    live, done, fail, stop = (markerless_rollout(home, t, s) for t, s in
                              (("t-live", "2026-09-24T10-00-01"), ("t-done", "2026-09-24T10-00-02"),
                               ("t-fail", "2026-09-24T10-00-03"), ("t-stop", "2026-09-24T10-00-04")))
    assert cht.scan_state(live, cht.CODEX) == "unknown"          # the transcript alone cannot tell
    now = time.time()
    states = {p: cht.session_state(cht.as_ref(p), now) for p in (live, done, fail, stop)}
    assert states == {live: "working", done: "working", fail: "error", stop: "done"}


def test_codex_status_falls_back_to_the_scan_without_a_database(tmp_path):
    path = write_rollout(tmp_path / "sessions" / "2026/09/24" / "rollout-2026-09-24T10-00-00-abc.jsonl",
                         [meta(id="abc"), rec("task_started", top="event_msg")])
    assert cht.session_state(cht.as_ref(path), time.time()) == "working"


def test_a_working_session_nobody_wrote_to_for_the_window_is_stale():
    assert cht.with_freshness("working", mtime=0.0, now=cht.DEFAULT_ACTIVE_WINDOW_SECS + 1) == "stale"
    assert cht.with_freshness("working", mtime=0.0, now=10.0) == "working"
    assert cht.with_freshness("done", mtime=0.0, now=1e9) == "done"


def test_scan_state_reaches_a_marker_several_megabytes_deep(tmp_path):
    noise = [rec("agent_message", top="event_msg", message="x" * 4000) for _ in range(800)]  # ~3.3 MB
    path = write_rollout(tmp_path / "rollout-2026-09-24T10-00-00-abc.jsonl",
                         [meta(id="abc"), rec("task_started", top="event_msg"), *noise])
    assert cht.scan_state(path, cht.CODEX) == "working"


def test_claude_local_command_records_do_not_start_a_turn():
    cmd = claude_rec("user", message={"role": "user", "content": "<command-name>/model</command-name>\n"
                                      "<command-message>model</command-message><command-args></command-args>"})
    out = claude_rec("user", message={"role": "user",
                                      "content": "<local-command-stdout>Set model</local-command-stdout>"})
    caveat = claude_rec("user", message={"role": "user", "content": [{"type": "text", "text":
                        "<local-command-caveat>Caveat: generated while running commands</local-command-caveat>"}]})
    for record in (cmd, out, caveat):
        assert cht.claude_advance("done", record) == "done"
    prompt = claude_rec("user", message={"role": "user", "content": "please continue"})
    assert cht.claude_advance("done", prompt) == "working"


def test_codex_names_come_from_the_threads_table(tmp_path):
    now_ms = int(time.time() * 1000)
    home = codex_home(tmp_path, [], [
        ("t-main", "x", "Onboard repository", "$agentkit:onboard-repo", None, None, "user", now_ms),
        ("t-titled", "x", None, "You are an adversarial code reviewer. Review ONLY the diff\nmore", None, None,
         "user", now_ms),
        ("t-sub", "x", None, "", "Singer", "worker", "subagent", now_ms)])
    markerless_rollout(home, "t-main", "2026-09-24T10-00-03")
    markerless_rollout(home, "t-titled", "2026-09-24T10-00-02")
    write_rollout(home / "sessions" / "2026/09/24" / "rollout-2026-09-24T10-00-01-t-sub.jsonl",
                  [meta("subagent", id="t-sub",
                        source={"subagent": {"thread_spawn": {"agent_nickname": "Singer", "agent_role": "worker"}}})])
    by_id = {r.session_id: (r.name, r.role) for r in cht.codex_discover(home / "sessions")}
    assert by_id["t-main"] == ("Onboard repository", "session")
    assert by_id["t-titled"][0].startswith("You are an adversarial")
    assert by_id["t-sub"] == ("Singer", "worker")


def test_claude_names_come_from_the_first_prompt_in_history(tmp_path):
    root = tmp_path / "claude" / "projects"
    sid = "11111111-2222-3333-4444-555555555555"
    write_jsonl(root / "-home-user" / f"{sid}.jsonl",
                [claude_rec("user", session=sid, cwd="/home/user",
                            message={"role": "user", "content": [{"type": "text", "text": "hi"}]})])
    write_jsonl(tmp_path / "claude" / "history.jsonl",
                [{"display": "/model opus", "timestamp": 0, "project": "/home/user", "sessionId": sid},
                 {"display": "fix the flaky login test please", "timestamp": 1, "project": "/home/user",
                  "sessionId": sid},
                 {"display": "and now the logout one", "timestamp": 2, "project": "/home/user", "sessionId": sid}])
    ref = next(r for r in cht.claude_discover(root) if r.session_id == sid)
    assert ref.name == "fix the flaky login test please"


def test_active_filter_keeps_recent_or_working_sessions_only(tmp_path):
    old_idle = cht.as_ref(write_rollout(tmp_path / "rollout-2026-09-20T10-00-00-old.jsonl", [meta(id="old")]))
    old_live = cht.as_ref(write_rollout(tmp_path / "rollout-2026-09-20T10-00-00-live.jsonl", [meta(id="live")]))
    fresh = cht.as_ref(write_rollout(tmp_path / "rollout-2026-09-24T10-00-00-new.jsonl", [meta(id="new")]))
    long_ago = time.time() - 3 * 86400
    os.utime(old_idle.path, (long_ago, long_ago))
    os.utime(old_live.path, (long_ago, long_ago))
    refs = [cht.as_ref(r.path) for r in (old_idle, old_live, fresh)]
    states = {old_live.path: "working", old_idle.path: "done"}
    kept = cht.filter_active(refs, states, time.time(), cht.DEFAULT_ACTIVE_WINDOW_SECS)
    assert [r.path for r in kept] == [old_live.path, fresh.path]


def test_parse_duration_accepts_seconds_minutes_and_hours():
    assert [cht.parse_duration(s) for s in ("90", "30m", "2h", "1.5h")] == [90.0, 1800.0, 7200.0, 5400.0]
    with pytest.raises(ValueError):
        cht.parse_duration("soon")


# ── review follow-ups: cache throttling and max age, cached-branch status, freshness order ──

def test_db_query_failures_are_throttled_and_results_expire(tmp_path, monkeypatch):
    home = codex_home(tmp_path, [("t1", "u1", 1, "inProgress", 100, None)])
    calls = {"n": 0}
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        calls["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(cht.sqlite3, "connect", counting_connect)
    cht._DB_CACHE.clear()
    clock = {"t": 1000.0}
    monkeypatch.setattr(cht.time, "monotonic", lambda: clock["t"])
    assert cht.codex_turn_states(home) == {"t1": "working"}
    for _ in range(20):                                   # 20 sessions asking in one refresh
        cht.codex_turn_states(home)
    assert calls["n"] == 1
    clock["t"] += cht.MAX_DB_CACHE_SECS + 1                # same signature, but too old to trust
    cht.codex_turn_states(home)
    assert calls["n"] == 2
    (home / "thread_history_1.sqlite").write_bytes(b"not a database")
    clock["t"] += cht.MAX_DB_CACHE_SECS + 1
    assert cht.codex_turn_states(home) is None
    for _ in range(20):
        cht.codex_turn_states(home)
    assert calls["n"] == 3                                 # the failure is cached like a result
    cht._DB_CACHE.clear()


def test_db_cache_identity_includes_the_query_parameters(tmp_path, monkeypatch):
    home = codex_home(tmp_path, [], [("t1", "x", "Named", "", None, None, "user", 5_000)])
    cht._DB_CACHE.clear()
    db = home / "state_5.sqlite"
    sql = "select id from threads where updated_at_ms > ?"
    assert cht._db_query(db, sql, (1_000,), lambda rows: [r[0] for r in rows]) == ["t1"]
    assert cht._db_query(db, sql, (9_000,), lambda rows: [r[0] for r in rows]) == []
    cht._DB_CACHE.clear()


def test_refresh_states_lets_the_database_override_an_opened_transcript(tmp_path):
    home = codex_home(tmp_path, [("t-int", "u1", 1, "interrupted", 100, 200),
                                 ("t-old", "u1", 1, "inProgress", 100, None)])
    interrupted = write_rollout(home / "sessions/2026/09/24/rollout-2026-09-24T10-00-00-t-int.jsonl",
                                [meta(id="t-int"), rec("task_started", top="event_msg"),
                                 rec("turn_aborted", top="event_msg", reason="interrupted")])
    old = markerless_rollout(home, "t-old", "2026-09-24T10-00-01")
    long_ago = time.time() - 2 * cht.DEFAULT_ACTIVE_WINDOW_SECS
    os.utime(old, (long_ago, long_ago))
    cht._DB_CACHE.clear()
    refs = [cht.as_ref(interrupted), cht.as_ref(old)]
    caches = {r.path: cht.TranscriptCache(r) for r in refs}
    for cache in caches.values():
        cache.update()
    assert caches[interrupted].state == "working"          # the transcript alone still says so
    states: dict = {}
    cht.refresh_states(refs, caches, states, 100.0, {})
    assert states == {interrupted: "done", old: "stale"}
    cht._DB_CACHE.clear()


# ── model + effort row ──────────────────────────────────────────────────────

def turn_context(model: str, effort: str) -> dict:
    """Real rollouts log turn_context as a top-level record type, not a payload type."""
    return {"timestamp": "2026-09-24T10:00:00.000Z", "type": "turn_context",
            "payload": {"turn_id": "t1", "cwd": "/w", "model": model, "effort": effort}}


def test_codex_usage_tracks_the_current_model_and_effort():
    counter = cht.UsageCounter(cht.CODEX)
    counter.feed(turn_context("gpt-5.6-sol", "medium"))
    counter.feed(turn_context("gpt-6-astra", "xhigh"))
    assert (counter.snapshot().model, counter.snapshot().effort) == ("gpt-6-astra", "xhigh")


def test_claude_usage_tracks_the_current_effort_from_assistant_records():
    counter = cht.UsageCounter(cht.CLAUDE)
    first = claude_assistant("m1", [{"type": "text", "text": "a"}], CLAUDE_USAGE, model="claude-opus-5-5")
    counter.feed({**first, "effort": "high", "perTurnEffort": "high"})
    assert (counter.snapshot().model, counter.snapshot().effort) == ("claude-opus-5-5", "high")
    counter.feed({**claude_assistant("m2", [{"type": "text", "text": "b"}], CLAUDE_USAGE), "effort": "low"})
    assert counter.snapshot().effort == "low"
    counter.feed(claude_assistant("m3", [{"type": "text", "text": "c"}], CLAUDE_USAGE))
    assert counter.snapshot().effort == "low"                       # a record without effort keeps the last one
    assert cht.UsageCounter(cht.CLAUDE).snapshot().effort is None


def test_model_line_shows_model_and_effort_when_known():
    usage = cht.Usage.empty()
    assert cht.model_line(usage) == ""
    assert cht.model_line(usage._replace(model="gpt-6-astra")) == "⚙ gpt-6-astra"
    assert cht.model_line(usage._replace(model="gpt-6-astra", effort="xhigh")) == "⚙ gpt-6-astra · xhigh"


def test_effort_round_trips_through_state_and_ledger(tmp_path):
    ref = cht.as_ref(write_rollout(tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl", [meta()]))
    usage = cht.Usage.empty()._replace(output=5, requests=1, model="gpt-6-astra", effort="xhigh")
    assert cht.Usage.from_state({"output": 5, "requests": 1, "model": "gpt-6-astra", "effort": "xhigh"}) == usage
    assert cht.Usage.from_state({"effort": 3}).effort is None
    ledger = cht.Ledger(tmp_path / "ledger")
    ledger.record(ref, usage)
    assert json.loads((tmp_path / "ledger" / f"{ref.source_key}.json").read_text())["effort"] == "xhigh"
    assert cht.Ledger(tmp_path / "ledger").known(ref.source_key) == usage


def test_follow_once_prints_the_model_and_effort_row(tmp_path, capsys):
    path = write_rollout(tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl",
                         [meta(), turn_context("gpt-6-astra", "xhigh"), codex_tokens(1000, 0, 10)])
    cht.follow(path, None, once=True, poll=0.01, ledger_dir=tmp_path / "ledger")
    out = capsys.readouterr().out
    assert "╰─ Σ ↑1k" in out
    assert "╰─ ⚙ gpt-6-astra · xhigh" in out


# ── review follow-ups: label sanitizing, model-row change tracking, list cell ──

def test_model_and_effort_labels_are_sanitized_wherever_they_enter():
    counter = cht.UsageCounter(cht.CODEX)
    counter.feed(turn_context("\x1b]52;c;aGVsbG8=\x1b\\gpt-6-astra\n", "x\x1b[31mhigh"))
    assert (counter.snapshot().model, counter.snapshot().effort) == ("gpt-6-astra", "xhigh")
    restored = cht.Usage.from_state({"model": "\x1b]52;c;x\x1b\\gpt", "effort": " \x1b[2Jlow "})
    assert (restored.model, restored.effort) == ("gpt", "low")
    assert len(cht.Usage.from_state({"model": "x" * 500}).model) <= cht.LABEL_LIMIT   # bounded, however garbled


def test_follow_once_prints_the_model_row_even_without_token_counts(tmp_path, capsys):
    path = write_rollout(tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl",
                         [meta(), turn_context("gpt-6-astra", "xhigh")])
    cht.follow(path, None, once=True, poll=0.01, ledger_dir=tmp_path / "ledger")
    assert "╰─ ⚙ gpt-6-astra · xhigh" in capsys.readouterr().out


def test_model_cell_keeps_the_effort_visible_for_long_model_names():
    usage = cht.Usage.empty()
    assert cht.model_cell(None) == "·"
    assert cht.model_cell(usage) == "·"
    assert cht.model_cell(usage._replace(model="gpt-6-astra", effort="xhigh")) == "gpt-6-astra/xhigh"
    cell = cht.model_cell(usage._replace(model="claude-sonnet-4-5-20250929", effort="high"))
    assert cell.endswith("/high") and len(cell) <= cht.MODEL_CELL_WIDTH


@pytest.fixture
def dashboard_frame(monkeypatch):
    class Screen:
        def __init__(self, width):
            self.width = width
            self.erase()

        def erase(self):
            self.rows = [" " * self.width for _ in range(24)]
            self.attrs = {}

        def getmaxyx(self):
            return len(self.rows), self.width

        def addnstr(self, row, col, value, limit, attr):
            self.attrs[row, col] = attr
            part = value[:limit]
            line = self.rows[row]
            self.rows[row] = line[:col] + part + line[col + len(part):]

        def timeout(self, delay):
            pass

        def getch(self):
            return on_key(self) if on_key else ord("q")

    on_key = None

    def render(refs, width, key_handler=None):
        nonlocal on_key
        on_key = key_handler
        screen = Screen(width)
        monkeypatch.setattr(cht, "discovery_snapshot", lambda roots: refs)
        monkeypatch.setattr(cht, "init_colors", lambda: None)
        monkeypatch.setattr(cht, "curses", SimpleNamespace(
            curs_set=lambda value: None, mousemask=lambda value: None, error=RuntimeError,
            A_BOLD=1, A_DIM=2, A_REVERSE=4, A_UNDERLINE=8,
            KEY_DOWN=258, KEY_UP=259, KEY_NPAGE=338, KEY_PPAGE=339, KEY_HOME=262,
            KEY_END=360, KEY_RESIZE=410, KEY_MOUSE=409,
        ))
        monkeypatch.setattr(cht.STYLE, "enabled", False)
        cht.interactive_dashboard(screen, [cht.RootSel("codex", Path("."), True)], None, 0, ledger_dir=None)
        return screen.rows

    return render


def test_dashboard_populates_unopened_session_on_start(tmp_path, dashboard_frame):
    refs = [cht.as_ref(write_rollout(tmp_path / f"{name}.jsonl", [
        meta(id=name), turn_context(name, "high"), codex_tokens(400, 0, 32)
    ])) for name in ("first", "second")]
    rows = dashboard_frame(refs, 150)
    assert any("432" in row and "second/high" in row for row in rows)


def test_dashboard_shortcuts_below_statistics_at_standard_width(tmp_path, dashboard_frame):
    ref = cht.as_ref(write_rollout(tmp_path / "first.jsonl", [meta(), codex_tokens(400, 0, 32)]))
    rows = dashboard_frame([ref], 80)
    stats = next(i for i, row in enumerate(rows) if "Σ" in row)
    assert "tab/1-9" in " ".join(rows[stats + 1:])
    assert "q quit" in rows[-1]


def test_unopened_usage_across_chunks_append_and_truncation(tmp_path):
    path = write_rollout(tmp_path / "large.jsonl", [
        turn_context("first", "high"), {"padding": "x" * cht.READ_CHUNK_BYTES}, codex_tokens(100, 0, 5)
    ])
    reader = cht.SessionUsageReader(cht.as_ref(path))
    assert reader.update()
    assert reader.usage is None
    assert not reader.update()
    assert (reader.usage.total, reader.usage.model) == (105, "first")
    with path.open("a") as handle:
        handle.write(json.dumps(codex_tokens(200, 0, 10)) + "\n")
    assert not reader.update()
    assert reader.usage.total == 210
    write_rollout(path, [turn_context("second", "low")])
    assert not reader.update()
    assert (reader.usage.total, reader.usage.model, reader.usage.effort) == (0, "second", "low")


@pytest.mark.parametrize("direction,current,target", [(1, None, "high"), (-1, "high", "low")])
def test_worker_effort_steps_using_live_catalog_and_confirms(tmp_path, direction, current, target):
    path = write_rollout(tmp_path / "worker.jsonl", [meta(id="child")])
    ref = cht.as_ref(path)._replace(kind="subagent", session_id="child", parent_id="root")
    thread = {"id": "child", "path": str(path), "status": {"type": "active"}, "model": "worker",
              "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "root"}}},
              "reasoningEffort": current}
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        if method == "model/list":
            return {"data": [{"model": "worker", "defaultReasoningEffort": "low",
                               "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("high", "low")]}]}
        if method == "thread/settings/update":
            assert params == {"threadId": "child", "effort": target}
            thread["reasoningEffort"] = target
            return {}
        assert method == "thread/read" and params == {"threadId": "child", "includeTurns": False}
        return {"thread": thread.copy()}
    assert cht.adjust_worker_effort(ref, direction, rpc) == target
    assert calls[-1][0] == "thread/read"


@pytest.mark.parametrize("problem", ["root", "idle", "other path", "limit", "unconfirmed"])
def test_worker_effort_fails_closed(tmp_path, problem):
    path = write_rollout(tmp_path / "worker.jsonl", [meta(id="child")])
    ref = cht.as_ref(path)._replace(kind="subagent", session_id="child", parent_id="root")
    writes = []
    def rpc(method, params):
        if method == "thread/read":
            return {"thread": {"id": "child", "path": str(path) if problem != "other path" else "/elsewhere",
                               "reasoningEffort": "high" if problem == "limit" else "low", "model": "m",
                               "source": "cli" if problem == "root" else {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}},
                               "status": {"type": "idle" if problem == "idle" else "active"}}}
        if method == "model/list":
            return {"data": [{"model": "m", "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "high")]}]}
        writes.append(params)
        return {}
    with pytest.raises(ValueError):
        cht.adjust_worker_effort(ref, 1, rpc)
    assert len(writes) == (1 if problem == "unconfirmed" else 0)


def test_dashboard_worker_glows_until_socket_readback(tmp_path, monkeypatch, dashboard_frame):
    import tempfile
    import threading

    if not hasattr(__import__("socket"), "AF_UNIX"):
        pytest.skip("requires a local Unix socket")
    server = pytest.importorskip("websockets.sync.server")
    # A short home keeps the socket inside the Unix socket path length limit.
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CODEX_HOME", home)
        socket = Path(home) / "app-server-control" / "app-server-control.sock"
        socket.parent.mkdir()
        path = write_rollout(tmp_path / "worker.jsonl", [
            meta("subagent", id="child", source={"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}),
            turn_context("worker-model", "low")])
        ref = cht.as_ref(path)
        allow_readback = threading.Event()
        writes = []
        def handle(ws):
            effort = "low"
            for line in ws:
                msg = json.loads(line)
                method = msg["method"]
                if method == "initialized":
                    continue
                if method == "initialize":
                    assert msg["params"]["capabilities"]["experimentalApi"]
                    result = {}
                elif method == "model/list":
                    result = {"data": [{"model": "worker-model", "supportedReasoningEfforts":
                              [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]}]}
                elif method == "thread/settings/update":
                    writes.append(msg["params"])
                    effort = msg["params"]["effort"]
                    result = {}
                else:
                    assert method == "thread/read" and msg["params"]["threadId"] == "child"
                    if writes:
                        assert allow_readback.wait(4)
                    result = {"thread": {"id": "child", "path": str(path), "model": "worker-model",
                              "status": {"type": "active"}, "reasoningEffort": effort,
                              "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}}
                ws.send(json.dumps({"id": msg["id"], "result": result}))

        sent = False
        pulse_attrs = set()
        deadline = time.monotonic() + 5
        def key(screen):
            nonlocal sent
            assert time.monotonic() < deadline, screen.rows
            if not sent:
                assert any("[/] next-turn effort" in row for row in screen.rows)
                sent = True
                return ord("]")
            for i, row in enumerate(screen.rows):
                if "Next turn: high…" in row:
                    pulse_attrs.add(screen.attrs[i, 0])
                    if len(pulse_attrs) == 2:
                        allow_readback.set()
                elif "Next turn: high " in row:
                    assert allow_readback.is_set() and "worker-model · low" in row
                    return ord("q")
            time.sleep(0.015)
            return -1

        with server.unix_serve(handle, str(socket)) as daemon:
            thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            thread.start()
            try:
                dashboard_frame([ref], 100, key)
            finally:
                allow_readback.set()
                daemon.shutdown()
                thread.join(2)
        assert writes == [{"threadId": "child", "effort": "high"}]


def test_effort_change_rechecks_worker_after_catalog_lookup(tmp_path):
    path = write_rollout(tmp_path / "worker.jsonl", [meta("subagent", id="child",
        source={"subagent": {"thread_spawn": {"parent_thread_id": "root"}}})])
    status = "active"
    def rpc(method, params):
        nonlocal status
        if method == "thread/read":
            return {"thread": {"id": "child", "path": str(path), "model": "m", "reasoningEffort": "low",
                              "status": {"type": status}, "source": {"subagent": {"thread_spawn":
                              {"parent_thread_id": "root"}}}}}
        assert method == "model/list", "worker finished before the write"
        status = "idle"
        return {"data": [{"model": "m", "supportedReasoningEfforts": [
            {"reasoningEffort": "low"}, {"reasoningEffort": "high"}]}]}
    with pytest.raises(ValueError):
        cht.adjust_worker_effort(cht.as_ref(path), 1, rpc)


def test_spawned_worker_on_disk_reports_daemon_ownership(tmp_path):
    path = write_rollout(tmp_path / "worker.jsonl", [meta("subagent", id="child",
        source={"subagent": {"thread_spawn": {"parent_thread_id": "root"}}})])

    def rpc(method, params):
        assert method == "thread/read"
        return {"thread": {"id": "child", "path": str(path), "status": {"type": "notLoaded"},
                           "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "root"}}}}}

    with pytest.raises(ValueError, match="Worker is not loaded on this daemon"):
        cht.adjust_worker_effort(cht.as_ref(path), 1, rpc)
