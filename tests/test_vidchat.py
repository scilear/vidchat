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
