"""Thin async adapter over ``notebooklm-py`` with vidchat-specific errors.

Keeping all direct ``notebooklm`` imports in this module means the CLI stays
testable without Google credentials: tests substitute :class:`FakeBackend`.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Protocol


class VidchatError(Exception):
    """Base for all vidchat failures."""


class VidchatAuthError(VidchatError):
    """Session cookies missing/expired. Fix: ``notebooklm login``."""

    def __str__(self) -> str:
        return (
            "Not authenticated with Gemini Notebook. "
            "Run `notebooklm login` (then `notebooklm auth check --test`), "
            "and retry vidchat."
        )


class VidchatIngestError(VidchatError):
    """The video source will not become ready (rejected, no transcript...)."""


class VidchatIngestTimeout(VidchatError):
    """The video was still processing when the wait budget ran out."""


class VidchatBackendError(VidchatError):
    """Rate limit / network / server failure from the unofficial API."""


@dataclass
class NotebookInfo:
    id: str
    title: str


@dataclass
class SourceInfo:
    id: str
    title: str
    status: str


@dataclass
class Answer:
    text: str
    conversation_id: str | None = None


class Backend(Protocol):
    async def create_notebook(self, title: str) -> NotebookInfo: ...
    async def add_youtube_source(self, notebook_id: str, url: str, timeout: float) -> SourceInfo: ...
    async def ask(self, notebook_id: str, question: str) -> Answer: ...
    async def delete_notebook(self, notebook_id: str) -> None: ...


def temp_notebook_title(video_url: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    short = video_url.strip()
    if len(short) > 48:
        short = short[:48] + "…"
    return f"vidchat/tmp-{stamp} {short}"


class NotebookLMBackend:
    """Real backend. Use as ``async with NotebookLMBackend(...) as backend``."""

    def __init__(self, profile: str | None = None, backend: str | None = None,
                 chat_timeout: float | None = None, client=None) -> None:
        self._profile = profile
        self._backend_name = backend
        self._chat_timeout = chat_timeout
        # Injected client (tests / embedding). When None, __aenter__ opens a
        # real NotebookLMClient from local storage.
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "NotebookLMBackend":
        if not self._owns_client:
            return self
        try:
            from notebooklm import NotebookLMClient
        except ImportError as exc:
            raise VidchatBackendError(
                "The `notebooklm-py` package is not installed. "
                "Run `pip install notebooklm-py` (or `pip install -e .`)."
            ) from exc
        kwargs: dict = {}
        if self._profile:
            kwargs["profile"] = self._profile
        if self._backend_name:
            kwargs["backend"] = self._backend_name
        if self._chat_timeout:
            kwargs["chat_timeout"] = self._chat_timeout
        try:
            client_ctx = NotebookLMClient.from_storage(**kwargs)
            self._client = await client_ctx.__aenter__()
            self._ctx = client_ctx
        except Exception as exc:
            _raise_mapped(exc)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._owns_client and self._client is not None:
            await self._ctx.__aexit__(*exc_info)
            self._client = None

    async def create_notebook(self, title: str) -> NotebookInfo:
        try:
            nb = await self._client.notebooks.create(title)
            return NotebookInfo(id=nb.id, title=getattr(nb, "title", title))
        except Exception as exc:
            _raise_mapped(exc)

    async def add_youtube_source(self, notebook_id: str, url: str, timeout: float) -> SourceInfo:
        """Add a YouTube URL and wait until it is ready.

        Raises VidchatIngestError (terminal) or VidchatIngestTimeout (retryable)
        instead of leaking library-specific exceptions to the CLI.
        """
        try:
            from notebooklm import SourceStatus
        except ImportError as exc:  # pragma: no cover
            raise VidchatBackendError("notebooklm-py is not installed.") from exc
        try:
            source = await self._client.sources.add_url(notebook_id, url, wait=False)
            ready = await self._client.sources.wait_until_ready(
                notebook_id, source.id, timeout=timeout
            )
            raw_status = getattr(ready, "status", None)
            if raw_status is not None and raw_status is not SourceStatus.READY:
                raise VidchatIngestError(
                    f"Video source finished with status {raw_status!r} instead of READY. "
                    "The video may lack an accessible transcript or be age/restricted. "
                    f"(source_id={source.id})"
                )
            status_name = getattr(raw_status, "name", None) or str(raw_status or "READY")
            return SourceInfo(
                id=source.id,
                title=getattr(ready, "title", "") or getattr(source, "title", ""),
                status=status_name,
            )
        except (VidchatIngestError, VidchatIngestTimeout):
            raise
        except Exception as exc:
            _raise_mapped(exc, context="importing the YouTube video")

    async def ask(self, notebook_id: str, question: str) -> Answer:
        try:
            result = await self._client.chat.ask(notebook_id, question)
            return Answer(text=result.answer or "", conversation_id=getattr(result, "conversation_id", None))
        except Exception as exc:
            _raise_mapped(exc, context="asking a follow-up question")

    async def delete_notebook(self, notebook_id: str) -> None:
        try:
            await self._client.notebooks.delete(notebook_id)
        except Exception as exc:
            _raise_mapped(exc, context="deleting the temporary notebook")


def _raise_mapped(exc: Exception, context: str = "") -> "None":
    """Map notebooklm-py exceptions onto vidchat errors. Never returns."""
    where = f" while {context}" if context else ""
    try:
        from notebooklm import exceptions as E  # type: ignore
    except ImportError:
        raise VidchatBackendError(f"Operation failed{where}: {exc}") from exc

    auth_types = tuple(
        t for t in (
            getattr(E, "AuthError", None),
            getattr(E, "HeadlessLoginRequiredError", None),
            getattr(E, "HeadlessReauthError", None),
            getattr(E, "AuthExtractionError", None),
        )
        if isinstance(t, type)
    )
    if auth_types and isinstance(exc, auth_types):
        raise VidchatAuthError() from exc
    # Heuristic: expired-session messages surface as generic RPC/auth failures.
    msg = str(exc).lower()
    if any(s in msg for s in ("login", "auth", "expired session", "401", "403", "signin", "sign-in")) and isinstance(
        exc, getattr(E, "NotebookLMError", Exception)
    ):
        # Only treat as auth when the library itself signals it, plus common markers.
        if isinstance(exc, getattr(E, "RPCError", ())) and any(
            s in msg for s in ("auth", "login", "expired", "401", "403")
        ):
            raise VidchatAuthError() from exc

    if isinstance(exc, getattr(E, "SourceProcessingError", ())):
        raise VidchatIngestError(
            f"The video could not be processed{where}. It may lack an accessible "
            f"transcript or be restricted. Detail: {exc}"
        ) from exc
    if isinstance(exc, getattr(E, "SourceTimeoutError", ())) or isinstance(
        exc, getattr(E, "WaitTimeoutError", ())
    ):
        # wait_until_ready raises SourceTimeoutError; fallthrough for other waits.
        if "source" in msg or "wait" in msg or not context or "import" in context or "video" in context:
            raise VidchatIngestTimeout(
                f"The video was still processing after the wait budget{where}. "
                "Retry with a larger --wait-timeout, or delete the temporary "
                f"notebook. Detail: {exc}"
            ) from exc
    if isinstance(exc, getattr(E, "NotebookNotFoundError", ())):
        raise VidchatBackendError(f"Notebook not found{where}: {exc}") from exc
    if isinstance(exc, getattr(E, "NotebookLMError", ())):
        raise VidchatBackendError(f"Gemini Notebook request failed{where}: {exc}") from exc
    raise VidchatBackendError(f"Unexpected failure{where}: {exc}") from exc


class FakeBackend:
    """In-memory backend for tests and --demo runs. No network."""

    def __init__(self, summary: str = "# Strategy summary\n\ndemo summary\n") -> None:
        self._summary = summary
        self.notebooks: dict[str, str] = {}
        self.deleted: list[str] = []
        self.questions: list[str] = []

    async def __aenter__(self) -> "FakeBackend":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def create_notebook(self, title: str) -> NotebookInfo:
        nb_id = f"nb-{len(self.notebooks) + 1}"
        self.notebooks[nb_id] = title
        return NotebookInfo(id=nb_id, title=title)

    async def add_youtube_source(self, notebook_id: str, url: str, timeout: float) -> SourceInfo:
        if "invalid" in url:
            raise VidchatIngestError("demo: video could not be processed")
        return SourceInfo(id="src-1", title="Demo video", status="READY")

    async def ask(self, notebook_id: str, question: str) -> Answer:
        self.questions.append(question)
        if question == "__summary__" or question.startswith("You are analysing a YouTube video"):
            return Answer(text=self._summary)
        return Answer(text=f"Demo answer to: {question}")

    async def delete_notebook(self, notebook_id: str) -> None:
        self.deleted.append(notebook_id)
        self.notebooks.pop(notebook_id, None)
