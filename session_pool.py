import asyncio
from bedrock import BedrockStreamManager
from prompts import build_system_prompt

class SessionPool:
    """
    Maintains pre-warmed BedrockStreamManager sessions.

    Outgoing calls: warm a specific session keyed by call_uuid during ring time.
    Incoming calls: keep a pool of generic ready sessions; claim one on connect.
    """

    def __init__(self, pool_size: int = 1):
        self._pool_size = pool_size
        # Generic pool for incoming calls
        self._pool: list[BedrockStreamManager] = []
        # call_uuid → pre-warmed session (outgoing calls)
        self._specific: dict[str, BedrockStreamManager] = {}
        self._specific_ready: dict[str, asyncio.Event] = {}
        self._maintain_task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        """Call once at app startup (inside an async context)."""
        self._maintain_task = asyncio.create_task(self._maintain_pool())
        print(f"[SessionPool] Started — pool_size={self._pool_size}")

    async def stop(self):
        if self._maintain_task:
            self._maintain_task.cancel()
        for mgr in self._pool:
            await mgr.close()
        for mgr in self._specific.values():
            await mgr.close()

    # ── Pool maintenance (incoming calls) ─────────────────────────────────────

    async def _maintain_pool(self):
        """Keep self._pool filled to pool_size at all times."""
        while True:
            try:
                while len(self._pool) < self._pool_size:
                    mgr = await self._warm_one()
                    if mgr:
                        self._pool.append(mgr)
                        print(f"[SessionPool] Pool ready ({len(self._pool)}/{self._pool_size})")
            except Exception as e:
                print(f"[SessionPool] Maintain error: {e}")
            await asyncio.sleep(1)

    # ── Outgoing call: warm a specific session ─────────────────────────────────

    def warm_for_call(self, call_uuid: str):
        """
        Start warming a session for a known call_uuid (outgoing calls).
        Non-blocking — the session warms in the background while the phone rings.
        """
        if call_uuid in self._specific_ready:
            return  # already warming
        event = asyncio.Event()
        self._specific_ready[call_uuid] = event
        asyncio.create_task(self._warm_specific(call_uuid, event))
        print(f"[SessionPool] Warming specific session for {call_uuid}")

    async def _warm_specific(self, call_uuid: str, event: asyncio.Event):
        mgr = await self._warm_one()
        if mgr:
            self._specific[call_uuid] = mgr
        event.set()  # always unblock claim, even on failure

    # ── Claim a session ────────────────────────────────────────────────────────

    async def claim(self, call_uuid: str, timeout: float = 9.0) -> BedrockStreamManager | None:
        """
        Try to get a pre-warmed session for call_uuid.
        1. Check specific sessions (outgoing calls, keyed by UUID).
        2. Fall back to generic pool (incoming calls).
        3. Return None if nothing is available within timeout.
        """
        # Wait for specific session if warming is in progress
        if call_uuid in self._specific_ready:
            event = self._specific_ready[call_uuid]
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                print(f"[SessionPool] Timed out waiting for specific session {call_uuid}")

            mgr = self._specific.pop(call_uuid, None)
            self._specific_ready.pop(call_uuid, None)
            if mgr:
                print(f"[SessionPool] ✅ Specific session claimed for {call_uuid}")
                return mgr

        # Fall back to pool (used by incoming calls)
        if self._pool:
            mgr = self._pool.pop(0)
            print(f"[SessionPool] ✅ Pool session claimed for {call_uuid} (pool now {len(self._pool)})")
            return mgr

        print(f"[SessionPool] ⚠️  No warm session for {call_uuid} — cold start fallback")
        return None

    # ── Internal warm-up helper ───────────────────────────────────────────────

    async def _warm_one(self) -> BedrockStreamManager | None:
        try:
            mgr = BedrockStreamManager()
            system_prompt = build_system_prompt()
            await mgr.initialize(system_prompt)
            # NOTE: Don't call send_audio_content_start() here —
            # we call it after claim() so Nova Sonic doesn't idle-timeout
            # during ring time. It's cheap and fast.
            return mgr
        except Exception as e:
            print(f"[SessionPool] Warm-one failed: {e}")
            return None


# Singleton — import this everywhere
session_pool = SessionPool(pool_size=1)
