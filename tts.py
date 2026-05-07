
import asyncio
import threading

from cartesia import Cartesia

from config import CARTESIA_API_KEY, CARTESIA_VOICE_ID, CARTESIA_MODEL_ID, CARTESIA_RATE
from config import debug_print


class   CartesiaTTSHandler:
    # Characters that trigger a sentence flush
    FLUSH_CHARS = {'.', '!', '?', '\n'}

    def __init__(self, audio_output_queue: asyncio.Queue):
        self.q      = audio_output_queue          # shared output queue → VoBiz send loop
        self.client = Cartesia(api_key=CARTESIA_API_KEY)
        self._buf   = ""                          # accumulates text until a sentence boundary
        self._tts_q: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._interrupted = False
        self._lock  = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Kick off the background synthesis coroutine."""
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        """Cancel the synthesis task on session close."""
        if self._task and not self._task.done():
            self._task.cancel()

    # ── Barge-in support ──────────────────────────────────────────────────────

    def interrupt(self):
        """
        Called when the user starts speaking while the assistant is talking.
        Clears all pending text and audio immediately.
        """
        with self._lock:
            self._interrupted = True
        self._buf = ""
        for q in (self._tts_q, self.q):
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    def _clear_interrupted(self):
        with self._lock:
            self._interrupted = False

    def _is_interrupted(self) -> bool:
        with self._lock:
            return self._interrupted

    # ── Text ingestion ────────────────────────────────────────────────────────

    def push_text(self, text: str):
        """
        Append text to the internal buffer.
        When a sentence boundary is detected the sentence is queued for synthesis.
        """
        if self._is_interrupted():
            return
        self._buf += text
        last = -1
        for i, ch in enumerate(self._buf):
            if ch in self.FLUSH_CHARS:
                last = i
        if last >= 0:
            sentence = self._buf[: last + 1].strip()
            self._buf = self._buf[last + 1 :]
            if sentence:
                self._tts_q.put_nowait(sentence)

    def flush(self):
        """Force-synthesise whatever text remains in the buffer (end of turn)."""
        if self._is_interrupted():
            self._buf = ""
            return
        remainder = self._buf.strip()
        self._buf = ""
        if remainder:
            self._tts_q.put_nowait(remainder)

    # ── Synthesis loop ────────────────────────────────────────────────────────

    async def _loop(self):
        while True:
            try:
                sentence = await self._tts_q.get()
                if not self._is_interrupted():
                    await self._synthesize(sentence)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Cartesia] loop error: {e}")

    async def _synthesize(self, text: str):
        debug_print(f"Synthesising: {text!r}")
        self._clear_interrupted()
        loop = asyncio.get_event_loop()
        try:
            chunks = await loop.run_in_executor(None, self._call_cartesia, text)
            for chunk in chunks:
                if self._is_interrupted():
                    break
                await self.q.put(chunk)
        except Exception as e:
            if not self._is_interrupted():
                print(f"[Cartesia] synthesis error: {e}")

    def _call_cartesia(self, text: str) -> list[bytes]:
        """Synchronous Cartesia SSE call — runs in a thread executor."""
        out = []
        try:
            for ev in self.client.tts.sse(
                model_id=CARTESIA_MODEL_ID,
                transcript=text,
                voice={"id": CARTESIA_VOICE_ID},
                output_format={
                    "container":   "raw",
                    "encoding":    "pcm_s16le",
                    "sample_rate": CARTESIA_RATE,
                },
            ):
                if self._is_interrupted():
                    break
                audio = getattr(ev, "audio", None)
                if audio:
                    out.append(audio)
        except Exception:
            if not self._is_interrupted():
                raise
        return out