# vidchat

Terminal chat over a YouTube video via a **temporary, dedicated
Gemini Notebook** (formerly NotebookLM) workspace.

```console
$ vidchat "https://www.youtube.com/watch?v=..."
Creating temporary notebook 'vidchat/tmp-20261013-120000 https://…'…
Importing video…
Waiting for source processing…
Source ready: …

# Strategy summary
[structured financial-strategy analysis]

Commands:
  <question>       Ask a follow-up grounded in the video source
  ask <question>   Same as above
  summary          Redisplay the strategy summary
  save             Save a local Markdown snapshot, keep chatting
  keep             Keep the remote notebook + save the archive, then exit
  delete           Delete the temporary remote notebook, then exit
  quit             Exit via the retention prompt (never silent)
  help             Show this help

you › What evidence supports the claimed edge?
…
you › keep
Kept notebook nb-abc123 and saved the archive.
```

## Terminal comfort

- The chat prompt supports full line editing: **Left/Right arrows** move
  through what you typed, **Up/Down** recall history, **Tab** completes
  commands, `Ctrl-A`/`Ctrl-E` jump to line ends.
- Command history persists in `~/.vidchat/research/history`.
- The `you ›` prompt is highlighted, assistant answers print **bold**,
  errors red, hints dim — only on interactive terminals. Piped output,
  logs, and `NO_COLOR=1` always stay plain text.

## How it works

1. Creates a fresh notebook per video (exact notebook id is tracked, so
   cleanup can never touch your other notebooks).
2. Imports the YouTube URL as a source and waits for processing.
3. Asks for a **financial-strategy summary** (thesis, rules, performance
   claims labelled as claims, evidence quality, failure modes, missing
   info, research ideas) with timestamps/citations where exposed.
4. Interactive follow-up chat, grounded in the video source with
   server-side conversation context.
5. Explicit retention decision — `quit` never silently deletes or keeps:
   `keep` (remote + local archive), `archive` (local only, remote
   deleted), `delete` (remote deleted), `leave` (decide later).

The local Markdown archive under `~/.vidchat/research/` (+ `index.json`)
is the permanent record; the remote notebook is the workspace. Either can
be retained without the other.

## Prerequisites

- Python ≥ 3.10
- A personal Google account with Gemini Notebook access
- One-time login (the app uses your saved session, it never asks for a password):

```console
pip install -e .
pip install "notebooklm-py[browser]"   # needed once for `notebooklm login`
playwright install chromium            # browser used by the login flow
notebooklm login
notebooklm auth check --test     # expect "status": "ok"
```

`vidchat` itself is a thin wrapper over the unofficial
[`notebooklm-py`](https://github.com/teng-lin/notebooklm-py) integration.
Google can change the underlying service; `vidchat` reports auth, import
and processing failures explicitly instead of presenting incomplete
output as success.

## Install

```console
pip install -e .        # puts `vidchat` on PATH
# optional, for development:
pip install -e ".[dev]"
```

## Usage

```console
vidchat "https://www.youtube.com/watch?v=..." [--profile NAME] [--backend web|android]
        [--research-dir DIR] [--notebook-title TITLE]
        [--wait-timeout SECONDS] [--chat-timeout SECONDS]
        [--on-exit prompt|keep|archive|delete|leave] [--yes] [--demo]

vidchat list [--json]                 # archived sessions from index.json
vidchat show <session-id>             # one archived session
vidchat delete-notebook <notebook-id> # delete one remote notebook by exact id
```

Environment: `VIDCHAT_RESEARCH_DIR` overrides the default research dir.

Try the whole loop without credentials:

```console
vidchat --demo "https://www.youtube.com/watch?v=dQw4w9WgXcQ" < /dev/null --on-exit archive
```

## Failure handling

- **Expired auth** → clear `notebooklm login` instructions, exit 2.
- **Video rejected / no transcript** → explicit error, no fake summary;
  the half-created notebook is deleted unless `--no-cleanup-on-failure`.
- **Processing timeout** → explicit error with `--wait-timeout` hint;
  same orphan cleanup.
- **Ctrl-C / EOF** → interruption is reported with the notebook id and a
  `vidchat delete-notebook <id>` command; nothing is silently deleted.
- Every run records a `pending` entry in `index.json` once the notebook
  exists, so abandoned notebooks stay visible via `vidchat list`.

---

Built and maintained by [Fabien Vernaz](https://solvefortime.com/), AI engineering expert, who helps small businesses apply AI with the checks and accountability it needs ([Solve for Time](https://solvefortime.com/)).
