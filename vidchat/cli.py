"""vidchat command line: ``vidchat "https://youtube.com/watch?v=..."``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from vidchat import __version__
from vidchat.notebook import (
    FakeBackend,
    NotebookLMBackend,
    VidchatAuthError,
    VidchatBackendError,
    VidchatError,
    VidchatIngestError,
    VidchatIngestTimeout,
    temp_notebook_title,
)
from vidchat.prompts import SUMMARY_PROMPT
from vidchat.style import (
    banner,
    error_text,
    glow,
    hint_text,
    prompt_text,
    setup_readline,
    warn_text,
)
from vidchat.store import (
    DEFAULT_RESEARCH_DIR,
    Session,
    Turn,
    ensure_research_dir,
    find_record,
    load_index,
    make_session_id,
    save_session,
    upsert_index,
    utcnow_iso,
)

ADMIN_COMMANDS = ("list", "show", "delete-notebook")

REPL_COMMANDS = ("ask", "summary", "save", "keep", "delete", "quit", "help")

HELP_TEXT = """\
Commands (type at the you › prompt):
  <question>       Ask a follow-up grounded in the video source
  ask <question>   Same as above
  summary          Redisplay the strategy summary
  save             Save a local Markdown snapshot, keep chatting
  keep             Keep the remote notebook + save the archive, then exit
  delete           Delete the temporary remote notebook, then exit
  quit             Exit via the retention prompt (never silent)
  help             Show this help
  (Arrow keys edit the line, Up recalls history, Tab completes commands.)
"""


def parse_repl_line(line: str) -> tuple[str, str]:
    """Map raw input to (command, argument). Bare text means 'ask'."""
    stripped = line.strip()
    if not stripped:
        return ("empty", "")
    low = stripped.lower()
    for cmd in ("ask ", "ask\t"):
        if low.startswith(cmd):
            return ("ask", stripped[4:].strip())
    first, _, rest = stripped.partition(" ")
    word = first.lower().lstrip("/")
    if word in ("help", "?"):
        return ("help", "")
    if word in ("summary", "redisplay"):
        return ("summary", "")
    if word in ("save", "snapshot"):
        return ("save", "")
    if word in ("keep", "retain"):
        return ("keep", "")
    if word in ("delete", "remove", "forget"):
        return ("delete", rest.strip())
    if word in ("quit", "exit", "q"):
        return ("quit", "")
    if word == "ask" and not rest.strip():
        return ("empty", "")
    return ("ask", stripped)


def build_chat_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vidchat",
        description="Chat with a YouTube video via a temporary Gemini Notebook. "
        "Run `notebooklm login` once before first use.",
    )
    p.add_argument("url", help="YouTube video URL to import as a notebook source.")
    p.add_argument("-p", "--profile", default=None, help="NotebookLM profile (default: active profile).")
    p.add_argument("--backend", choices=["web", "android"], default=None,
                   help="notebooklm-py backend (default: web / NOTEBOOKLM_BACKEND).")
    p.add_argument("--research-dir", default=None,
                   help="Directory for Markdown archives + index.json (default: ~/.vidchat/research).")
    p.add_argument("--notebook-title", default=None, help="Override the temporary notebook title.")
    p.add_argument("--wait-timeout", type=float, default=300.0,
                   help="Seconds to wait for video processing (default: 300).")
    p.add_argument("--chat-timeout", type=float, default=None,
                   help="Per-question read timeout in seconds (default: library default ~180).")
    p.add_argument("--on-exit", choices=["prompt", "keep", "archive", "delete", "leave"],
                   default="prompt",
                   help="Retention decision without prompting (default: prompt). "
                   "'archive' saves locally then deletes the remote notebook.")
    p.add_argument("--no-cleanup-on-failure", action="store_true",
                   help="Leave the temporary notebook behind when import fails (default: delete it).")
    p.add_argument("-y", "--yes", action="store_true", help="Skip delete confirmations.")
    p.add_argument("--demo", action="store_true",
                   help="Run the full loop against a fake backend (no network, no auth).")
    p.add_argument("--version", action="version", version=f"vidchat {__version__}")
    return p


def looks_like_youtube(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def retention_prompt(assume_yes: bool = False, on_exit: str = "prompt") -> str:
    """Ask the explicit retention question. Returns keep|archive|delete|leave."""
    if on_exit != "prompt":
        return on_exit
    print("\nRetention decision (explicit — quit never implies deletion or retention):")
    print("  keep     Keep the remote notebook + save the local Markdown archive")
    print("  archive  Save the local Markdown archive, delete the remote notebook")
    print("  delete   Delete the remote notebook without saving")
    print("  leave    Leave everything as-is; decide later")
    try:
        while True:
            choice = input("keep / archive / delete / leave? ").strip().lower()
            if choice in ("keep", "k"):
                return "keep"
            if choice in ("archive", "a"):
                return "archive"
            if choice in ("delete", "d"):
                return "delete"
            if choice in ("leave", "l", ""):
                if choice == "":
                    print("Please type one of: keep, archive, delete, leave.")
                    continue
                return "leave"
            print("Please type one of: keep, archive, delete, leave.")
    except (EOFError, KeyboardInterrupt):
        print()
        return "leave"


async def best_effort_delete(backend, notebook_id: str) -> bool:
    try:
        await backend.delete_notebook(notebook_id)
        return True
    except VidchatError as exc:
        print(f"Warning: could not delete notebook {notebook_id}: {exc}")
        return False


async def repl_loop(backend, session: Session, args) -> str:
    """Interactive chat. Returns the retention decision."""
    notebook_id = session.notebook_id
    assert notebook_id, "repl requires a notebook id"
    deleted_remote = False
    saved = False

    print("\n" + HELP_TEXT)
    while True:
        try:
            line = input(prompt_text())
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break
        command, argument = parse_repl_line(line)
        if command == "empty":
            continue
        if command == "help":
            print(HELP_TEXT)
            continue
        if command == "summary":
            print(banner("\n# Strategy summary\n"))
            print(glow(session.summary))
            continue
        if command == "save":
            path = save_session(session, args.research_dir)
            saved = True
            print(f"Saved local snapshot to {path}")
            continue
        if command == "keep":
            save_session(session, args.research_dir)
            session.notebook_kept = True
            session.decision = "kept"
            upsert_index(session, ensure_research_dir(args.research_dir))
            print(f"Kept notebook {notebook_id} and saved the archive.")
            return "keep"
        if command == "delete":
            if not confirm(f"Delete temporary notebook {notebook_id}? This removes its source and content.", args.yes):
                print("Delete cancelled.")
                continue
            if await best_effort_delete(backend, notebook_id):
                deleted_remote = True
                print(f"Deleted temporary notebook {notebook_id}.")
            return "deleted-saved" if saved else "deleted"
        if command == "quit":
            break
        if command == "ask":
            if not argument:
                continue
            if deleted_remote:
                print("The temporary notebook was deleted; no further grounded questions are possible. "
                      "Use 'quit' to exit via the retention prompt.")
                continue
            try:
                print(hint_text("Thinking…"), flush=True)
                answer = await backend.ask(notebook_id, argument)
            except VidchatError as exc:
                print(error_text(f"Question failed: {exc}"))
                continue
            print(f"\n{glow(answer.text)}\n")
            session.turns.append(Turn(question=argument, answer=answer.text))
            continue

    # quit / EOF / Ctrl-C → explicit retention prompt (never silent)
    return retention_prompt(args.yes, args.on_exit)


async def apply_final_decision(backend, session: Session, decision: str, args) -> int:
    """Persist local archive and/or delete the remote notebook per decision."""
    notebook_id = session.notebook_id
    research_dir = ensure_research_dir(args.research_dir)

    if decision == "keep":
        session.notebook_kept = True
        session.decision = "kept"
        path = save_session(session, research_dir)
        print(f"Kept notebook {notebook_id}.\nSaved archive to {path}")
        print("Resume later with: vidchat show "
              f"{session.session_id}  (remote Q&A stays in the notebook)")
        return 0
    if decision == "archive":
        session.notebook_kept = False
        session.decision = "archived"
        path = save_session(session, research_dir)
        if notebook_id and not getattr(args, "_deleted_remote", False):
            if await best_effort_delete(backend, notebook_id):
                print(f"Deleted temporary notebook {notebook_id}.")
        print(f"Saved archive to {path} (remote notebook deleted).")
        return 0
    if decision in ("delete", "deleted", "deleted-saved"):
        session.decision = "deleted-saved" if decision == "deleted-saved" else "deleted"
        if notebook_id and not getattr(args, "_deleted_remote", False):
            if await best_effort_delete(backend, notebook_id):
                print(f"Deleted temporary notebook {notebook_id}.")
        elif decision == "deleted":
            print(f"Temporary notebook {notebook_id} was already deleted.")
        if decision == "deleted-saved":
            print("Local snapshot(s) saved earlier were left untouched.")
        else:
            upsert_index(session, research_dir)  # record the session even without md
        return 0
    # leave
    session.decision = "pending"
    upsert_index(session, research_dir)
    print("Left everything as-is (explicit non-decision).")
    if notebook_id:
        print(f"  notebook_id: {notebook_id}")
        print(f"  delete later with: vidchat delete-notebook {notebook_id}")
        print(f"  local record: {research_dir / (session.session_id + '.md')} (saved on next 'save'/'keep')")
    return 0


async def run_chat(args) -> int:
    url = args.url
    if not (url.startswith("http://") or url.startswith("https://")):
        print(error_text(f"Error: {url!r} is not a URL. Usage: vidchat \"https://www.youtube.com/watch?v=...\""))
        return 2
    if not looks_like_youtube(url):
        print(warn_text("Warning: this does not look like a YouTube URL; import will be attempted anyway."))

    if args.on_exit == "prompt" and not sys.stdin.isatty() and not args.demo:
        print("Error: no interactive input available; pass --on-exit keep|archive|delete|leave for scripting.")
        return 2

    session = Session(
        session_id=make_session_id(url),
        video_url=url,
        title=url,
        notebook_id=None,
        created_at=utcnow_iso(),
    )
    research_dir = ensure_research_dir(args.research_dir)
    # Arrow-key editing, Up-history, and Tab completion for the REPL input.
    # History persists next to the archive; no-op where readline is missing.
    setup_readline(research_dir / "history", REPL_COMMANDS)
    title = args.notebook_title or temp_notebook_title(url)

    backend: NotebookLMBackend | FakeBackend
    if args.demo:
        backend = FakeBackend()
    else:
        backend = NotebookLMBackend(profile=args.profile, backend=args.backend,
                                    chat_timeout=args.chat_timeout)

    notebook_id: str | None = None
    try:
        async with backend:
            # 1. Temporary dedicated notebook (track exact id for safe cleanup).
            try:
                print(f"Creating temporary notebook {title!r}…")
                nb = await backend.create_notebook(title)
            except VidchatAuthError as exc:
                print(error_text(f"Authentication failed: {exc}"))
                return 2
            except VidchatError as exc:
                print(error_text(f"Could not create notebook: {exc}"))
                return 1
            notebook_id = nb.id
            session.notebook_id = nb.id
            upsert_index(session, research_dir)  # pending marker → orphans stay visible

            # 2. Import the video.
            print("Importing video…")
            print("Waiting for source processing…")
            try:
                source = await backend.add_youtube_source(nb.id, url, timeout=args.wait_timeout)
                print(f"Source ready: {source.title or source.id}")
            except VidchatAuthError as exc:
                print(error_text(f"Authentication failed: {exc}"))
                await best_effort_delete(backend, nb.id)
                return 2
            except VidchatIngestTimeout as exc:
                print(error_text(f"Import timed out: {exc}"))
                print("No summary was produced — refusing to present an incomplete result as success.")
                if not args.no_cleanup_on_failure:
                    if await best_effort_delete(backend, nb.id):
                        print(f"Deleted temporary notebook {nb.id} to avoid an orphan.")
                else:
                    print(f"Left temporary notebook {nb.id} in place (--no-cleanup-on-failure).")
                return 1
            except VidchatIngestError as exc:
                print(error_text(f"Import failed: {exc}"))
                print("No summary was produced — refusing to present an incomplete result as success.")
                if not args.no_cleanup_on_failure:
                    if await best_effort_delete(backend, nb.id):
                        print(f"Deleted temporary notebook {nb.id} to avoid an orphan.")
                else:
                    print(f"Left temporary notebook {nb.id} in place (--no-cleanup-on-failure).")
                return 1
            except VidchatError as exc:
                print(error_text(f"Import failed: {exc}"))
                if not args.no_cleanup_on_failure:
                    await best_effort_delete(backend, nb.id)
                return 1

            # 3. Structured strategy summary.
            print(hint_text("\nGenerating strategy summary…"))
            try:
                answer = await backend.ask(nb.id, SUMMARY_PROMPT)
            except VidchatAuthError as exc:
                print(error_text(f"Authentication failed: {exc}"))
                print(f"Source import succeeded; notebook {nb.id} left in place for retry.")
                return 2
            except VidchatError as exc:
                print(error_text(f"Summary generation failed: {exc}"))
                print(f"Notebook {nb.id} left in place; delete with: vidchat delete-notebook {nb.id}")
                return 1
            if not answer.text.strip():
                print(error_text("Summary came back empty — refusing to present that as success. "
                      f"Notebook {nb.id} left in place for inspection."))
                return 1
            session.summary = answer.text
            print(banner("\n# Strategy summary\n"))
            print(glow(session.summary))

            # 4. Follow-up chat.
            if args.on_exit != "prompt" and not sys.stdin.isatty():
                decision = args.on_exit  # scripted: apply directly
            else:
                decision = await repl_loop(backend, session, args)
                if decision in ("deleted", "deleted-saved"):
                    # repl already deleted the remote notebook
                    args._deleted_remote = True
                elif decision == "keep":
                    return 0  # repl already saved + flagged
            return await apply_final_decision(backend, session, decision, args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        if notebook_id:
            print(f"Temporary notebook id: {notebook_id} (not silently deleted).")
            print(f"Delete it with: vidchat delete-notebook {notebook_id}")
        else:
            print("No notebook had been created yet; nothing to clean up.")
        return 130
    except VidchatAuthError as exc:  # outside the guarded block (e.g. client open)
        print(error_text(f"Authentication failed: {exc}"))
        return 2
    except VidchatError as exc:
        print(error_text(f"Failed: {exc}"))
        return 1


def run_admin(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="vidchat", description="vidchat archive utilities.")
    p.add_argument("--research-dir", default=None)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List archived sessions from index.json.").add_argument(
        "--json", action="store_true", help="Machine-readable output.")
    show = sub.add_parser("show", help="Show one archived session.")
    show.add_argument("session_id", help="Session id (or unique prefix) from `vidchat list`.")
    delete_nb = sub.add_parser("delete-notebook", help="Delete one remote notebook by exact id.")
    delete_nb.add_argument("notebook_id", help="Exact notebook id to delete (never a wildcard).")
    delete_nb.add_argument("-p", "--profile", default=None)
    delete_nb.add_argument("--backend", choices=["web", "android"], default=None)
    delete_nb.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    delete_nb.add_argument("--version", action="version", version=f"vidchat {__version__}")
    ns = p.parse_args(argv)

    research_dir = ensure_research_dir(ns.research_dir)
    if ns.command == "list":
        records = load_index(research_dir)
        if ns.json:
            print(json.dumps(records, indent=2))
        elif not records:
            print(f"No sessions archived yet in {research_dir}.")
        else:
            for r in records:
                kept = "kept" if r.get("notebook_kept") else r.get("decision", "local")
                print(f"{r.get('session_id')}  [{kept}]  {r.get('video_url')}  "
                      f"turns={r.get('turn_count', 0)}")
        return 0
    if ns.command == "show":
        record = find_record(research_dir, ns.session_id)
        if not record:
            print(f"No session matching {ns.session_id!r} in {research_dir / 'index.json'}.")
            return 1
        print(json.dumps(record, indent=2))
        md = record.get("markdown_path")
        if md and Path(md).exists():
            print(f"--- {md} ---")
            print(Path(md).read_text(encoding="utf-8")[:6000])
        return 0
    if ns.command == "delete-notebook":
        if not confirm(f"Delete remote notebook {ns.notebook_id}? This is permanent.", ns.yes):
            print("Cancelled.")
            return 0

        async def _delete() -> int:
            try:
                async with NotebookLMBackend(profile=ns.profile, backend=ns.backend) as backend:
                    await backend.delete_notebook(ns.notebook_id)
            except VidchatAuthError as exc:
                print(f"Authentication failed: {exc}")
                return 2
            except VidchatError as exc:
                print(f"Delete failed: {exc}")
                return 1
            print(f"Deleted notebook {ns.notebook_id}.")
            return 0

        return asyncio.run(_delete())
    return 2  # unreachable


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ADMIN_COMMANDS:
        return run_admin(argv)
    if argv and argv[0] in ("-h", "--help") and len(argv) == 1:
        build_chat_parser().print_help()
        print("\nArchive utilities:\n"
              "  vidchat list [--json]                 List archived sessions\n"
              "  vidchat show <session-id>             Show one archived session\n"
              "  vidchat delete-notebook <notebook-id> Delete one remote notebook")
        return 0
    args = build_chat_parser().parse_args(argv)
    if not getattr(args, "url", None):
        build_chat_parser().print_help()
        return 2
    try:
        return asyncio.run(run_chat(args))
    except KeyboardInterrupt:
        print("\nInterrupted before startup completed.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
