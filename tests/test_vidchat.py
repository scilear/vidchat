"""Unit tests: pure logic + fake-backend loop. No network, no Google auth."""

import asyncio

from vidchat.cli import parse_repl_line
from vidchat.notebook import FakeBackend, VidchatIngestError
from vidchat.prompts import SUMMARY_PROMPT
from vidchat.store import (
    Session,
    Turn,
    find_record,
    load_index,
    make_session_id,
    render_markdown,
    save_session,
    slugify,
)


def test_summary_prompt_has_required_sections():
    for section in (
        "Executive summary",
        "Strategy specification",
        "Performance claims",
        "Evidence quality",
        "Potential failure modes",
        "Missing information",
        "Research opportunities",
    ):
        assert section in SUMMARY_PROMPT
    assert "claimed in the video" in SUMMARY_PROMPT


def test_repl_parsing():
    assert parse_repl_line("help") == ("help", "")
    assert parse_repl_line("/help") == ("help", "")
    assert parse_repl_line("summary") == ("summary", "")
    assert parse_repl_line("save") == ("save", "")
    assert parse_repl_line("keep") == ("keep", "")
    assert parse_repl_line("delete") == ("delete", "")
    assert parse_repl_line("quit") == ("quit", "")
    assert parse_repl_line("ask What is the edge?") == ("ask", "What is the edge?")
    assert parse_repl_line("What is the edge?") == ("ask", "What is the edge?")
    assert parse_repl_line("   ") == ("empty", "")


def test_slugify_prefers_youtube_id():
    assert slugify("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert slugify("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    slug = slugify("https://example.com/some article!")
    assert slug and "/" not in slug


def test_session_id_starts_with_timestamp():
    sid = make_session_id("https://www.youtube.com/watch?v=abc123XYZ_-")
    assert sid[:9].replace("-", "").isdigit()
    assert "abc123XYZ_" in sid


def test_render_markdown_marks_claims_and_transcript():
    session = Session(
        session_id="20261013-120000-abc",
        video_url="https://www.youtube.com/watch?v=abc",
        title="https://www.youtube.com/watch?v=abc",
        notebook_id="nb-1",
        created_at="2026-10-13T12:00:00+00:00",
        summary="## 1. Executive summary\n\nClaimed 40% CAGR (claimed in the video).",
        turns=[Turn(question="What is the edge?", answer="Edge answer.")],
    )
    md = render_markdown(session)
    assert "notebook_id: nb-1" in md
    assert "What is the edge?" in md
    assert "Edge answer." in md
    assert "not verified results" in md


def test_save_and_index_roundtrip(tmp_path):
    session = Session(
        session_id="20261013-120000-abc",
        video_url="https://www.youtube.com/watch?v=abc",
        title="t",
        notebook_id="nb-1",
        created_at="2026-10-13T12:00:00+00:00",
        summary="summary text",
        turns=[Turn(question="q", answer="a")],
    )
    md_path = save_session(session, tmp_path)
    assert md_path.exists()
    records = load_index(tmp_path)
    assert len(records) == 1
    assert records[0]["session_id"] == session.session_id
    assert records[0]["turn_count"] == 1
    assert records[0]["decision"] == "pending"
    # save again → upsert, not duplicate
    save_session(session, tmp_path)
    assert len(load_index(tmp_path)) == 1
    assert find_record(tmp_path, session.session_id[:8]) is not None


def test_fake_backend_loop():
    async def go():
        async with FakeBackend() as backend:
            nb = await backend.create_notebook("title")
            src = await backend.add_youtube_source(nb.id, "https://www.youtube.com/watch?v=x", 5)
            assert src.status == "READY"
            summary = await backend.ask(nb.id, "__summary__")
            assert "demo summary" in summary.text
            await backend.delete_notebook(nb.id)
            assert nb.id in backend.deleted

    asyncio.run(go())


def test_fake_backend_rejects_bad_video():
    async def go():
        async with FakeBackend() as backend:
            nb = await backend.create_notebook("title")
            try:
                await backend.add_youtube_source(nb.id, "https://example.com/invalid", 5)
            except VidchatIngestError:
                return
            raise AssertionError("expected VidchatIngestError")

    asyncio.run(go())


class _StubSources:
    """Mimics the real client's sources namespace (SimpleNamespace shaped
    like notebooklm.types.Source, with a real SourceStatus enum member)."""

    def __init__(self, status):
        self._status = status

    async def add_url(self, notebook_id, url, wait=False):
        from types import SimpleNamespace

        assert wait is False
        return SimpleNamespace(id="src-1", title="Pending", status=None)

    async def wait_until_ready(self, notebook_id, source_id, timeout=120.0):
        from types import SimpleNamespace

        return SimpleNamespace(id=source_id, title="Real video title", status=self._status)


class _StubNotebooks:
    async def create(self, title):
        from types import SimpleNamespace

        return SimpleNamespace(id="nb-9", title=title)

    async def delete(self, notebook_id):
        return None


class _StubChat:
    async def ask(self, notebook_id, question):
        from types import SimpleNamespace

        return SimpleNamespace(answer="stub answer", conversation_id="conv-1")


class _StubClient:
    def __init__(self, status):
        from vidchat.notebook import NotebookLMBackend  # noqa: F401  (import check)

        self.notebooks = _StubNotebooks()
        self.sources = _StubSources(status)
        self.chat = _StubChat()


def test_real_backend_ingest_ready_builds_source_info():
    """Regression test for: SourceInfo() missing 'status' on every success."""
    from notebooklm import SourceStatus

    from vidchat.notebook import NotebookLMBackend

    async def go():
        async with NotebookLMBackend(client=_StubClient(SourceStatus.READY)) as backend:
            info = await backend.add_youtube_source("nb-9", "https://www.youtube.com/watch?v=x", 5)
            assert info.id == "src-1"
            assert info.title == "Real video title"
            assert info.status == "READY"
            answer = await backend.ask("nb-9", "q?")
            assert answer.text == "stub answer"
            await backend.delete_notebook("nb-9")  # must not raise

    asyncio.run(go())


def test_real_backend_ingest_error_status_raises():
    from notebooklm import SourceStatus

    from vidchat.notebook import NotebookLMBackend

    async def go():
        async with NotebookLMBackend(client=_StubClient(SourceStatus.ERROR)) as backend:
            try:
                await backend.add_youtube_source("nb-9", "https://www.youtube.com/watch?v=x", 5)
            except VidchatIngestError:
                return
            raise AssertionError("expected VidchatIngestError")

    asyncio.run(go())
