
import asyncio
import base64
import datetime
import json
import uuid

from fastapi import WebSocket, WebSocketDisconnect

import db
from call_analyzer import BedrockAnalyzer
from bedrock import BedrockStreamManager
from config import CARTESIA_RATE, VOBIZ_RATE, debug_print, VOICE_CLONE
from prompts import build_system_prompt
from session_pool import session_pool

# PCM resampling — audioop or audioop-lts fallback
try:
    import audioop
except ModuleNotFoundError:
    try:
        import audioop_lts as audioop
    except ImportError:
        raise SystemExit("audioop not available. Run: pip install audioop-lts")


class VoBizCallSession:

    def __init__(self, ws: WebSocket):
        self.ws         = ws
        self.mgr        = BedrockStreamManager()
        self.stream_id  = ""
        self.call_uuid  = ""

        self._downsample_state = None   # 24 kHz → 16 kHz resampler state (Cartesia → VoBiz)
        # NOTE: No upsample state needed — VoBiz input is already 16 kHz

        self._send_task: asyncio.Task | None = None
        self._active = False

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self):
        try:
            self._active = True

            # ── Step 1: Handshake FIRST to get call_uuid (fast, ~100ms) ──────
            await self._handshake()

            # ── Step 2: Try to claim a pre-warmed Bedrock session ─────────────
            pre_warmed = await session_pool.claim(self.call_uuid, timeout=9.0)

            if pre_warmed:
                self.mgr = pre_warmed
                print(f"[VoBiz] ✅ Using pre-warmed session for {self.call_uuid}")
            else:
                # Cold-start fallback (should rarely happen)
                print(f"[VoBiz] ⚠️  Cold-starting session for {self.call_uuid}")
                system_prompt = build_system_prompt()
                await self.mgr.initialize(system_prompt)
                
            # 🔥 Start the time filler "Hello" via Cartesia TTS if cloning
            if VOICE_CLONE:
                self.mgr.tts_handler.push_text("Hello.")
                self.mgr.tts_handler.flush()

            # ── Step 3: Send audio content start (cheap, ~50ms) ───────────────
            await self.mgr.send_audio_content_start()

            # ── Step 4: Attach call metadata to whichever mgr we're using ─────
            self.mgr.tool_processor.call_uuid = self.call_uuid
            self.mgr.call_uuid       = self.call_uuid
            self.mgr.call_id         = await db.create_call_record(self.call_uuid)
            self.mgr.call_start_time = datetime.datetime.now()

            self._send_task = asyncio.create_task(self._send_audio_loop())
            await self._receive_loop()

        finally:
            await self._cleanup()

    # ── VoBiz handshake ───────────────────────────────────────────────────────

    async def _handshake(self):
        """Wait for VoBiz 'start' event to get stream and call IDs."""
        try:
            while True:
                raw   = await asyncio.wait_for(self.ws.receive_text(), timeout=15)
                msg   = json.loads(raw)
                event = msg.get("event")

                if event == "start":
                    self.stream_id = msg["start"]["streamId"]
                    self.call_uuid = msg["start"].get("callId", str(uuid.uuid4()))
                    # NOTE: Don't set self.mgr.* here — mgr may be swapped below
                    print(f"[VoBiz] Stream started | callId: {self.call_uuid} | streamId: {self.stream_id}")
                    print(f"[VoBiz] Media format: {msg['start'].get('mediaFormat', {})}")
                    break

                elif event == "connected":
                    debug_print("VoBiz: connected")

        except asyncio.TimeoutError:
            print("[VoBiz] Timeout waiting for start event")

    # ── Inbound audio (VoBiz → Nova Sonic) ───────────────────────────────────

    async def _receive_loop(self):
        """
        Receive PCM 16 kHz audio from VoBiz and feed directly to Nova Sonic.
        No mu-law decode. No upsampling. VoBiz already sends 16 kHz PCM.
        """
        try:
            while self._active:
                raw   = await self.ws.receive_text()
                msg   = json.loads(raw)
                event = msg.get("event")

                if event == "media":
                    # VoBiz sends base64-encoded PCM 16 kHz — decode and pass directly
                    pcm16 = base64.b64decode(msg["media"]["payload"])
                    self.mgr.add_audio_chunk(pcm16)

                elif event == "stop":
                    debug_print(f"VoBiz stop | reason: {msg.get('reason', 'unknown')}")
                    self._active = False
                    break

        except WebSocketDisconnect:
            debug_print("WebSocket disconnected")
        except Exception as e:
            if self._active:
                print(f"[VoBizSession] receive error: {e}")
        finally:
            self._active = False

    # ── Outbound audio (Cartesia → VoBiz) ────────────────────────────────────

    async def _send_audio_loop(self):
        """
        Reads 24 kHz PCM from the Cartesia queue, resamples to 16 kHz,
        and sends to VoBiz as playAudio events.
        Handles barge-in by sending clearAudio and draining the queue.
        """
        try:
            while self._active:

                # ── Barge-in: clear outbound audio and drain queue ─────────────
                if self.mgr.barge_in_event.is_set():
                    self.mgr.barge_in_event.clear()
                    self.mgr.is_assistant_speaking = False

                    # Drain Cartesia queue
                    while not self.mgr.audio_output_queue.empty():
                        try:
                            self.mgr.audio_output_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                    # Tell VoBiz to stop playing buffered audio immediately
                    try:
                        await self.ws.send_text(json.dumps({
                            "event":    "clearAudio",
                            "streamId": self.stream_id,
                        }))
                    except Exception:
                        pass

                    self._downsample_state = None   # reset resampler state
                    await asyncio.sleep(0.02)
                    continue

                # ── Pull next audio chunk from Cartesia queue ──────────────────
                try:
                    pcm24 = await asyncio.wait_for(
                        self.mgr.audio_output_queue.get(), timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if self.mgr.is_assistant_speaking:
                        self.mgr.is_assistant_speaking = False
                    continue

                if not pcm24 or not self._active:
                    continue

                self.mgr.is_assistant_speaking = True

                # Resample: Cartesia 24 kHz → VoBiz 16 kHz
                pcm16, self._downsample_state = audioop.ratecv(
                    pcm24, 2, 1, CARTESIA_RATE, VOBIZ_RATE, self._downsample_state
                )

                # Check barge-in again after resample (may have fired during await)
                if self.mgr.barge_in_event.is_set():
                    continue

                # Send playAudio to VoBiz
                payload = base64.b64encode(pcm16).decode()
                msg = json.dumps({
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-l16",
                        "sampleRate":  VOBIZ_RATE,
                        "payload":     payload,
                    },
                })
                try:
                    await self.ws.send_text(msg)
                except Exception:
                    break

                if self.mgr.audio_output_queue.empty():
                    self.mgr.is_assistant_speaking = False

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self._active:
                print(f"[VoBizSession] send error: {e}")

    # ── Cleanup & post-call analysis ──────────────────────────────────────────

    async def _cleanup(self):
        self._active = False
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
        await self.mgr.close()

        if self.mgr.call_id and self.mgr.call_start_time:
            call_duration = (datetime.datetime.now() - self.mgr.call_start_time).total_seconds()
            analyzer      = BedrockAnalyzer()
            lead_saved    = any(
                t.get("tool", "").lower() == "createleadtool"
                for t in self.mgr.transcript if t.get("role") == "TOOL"
            )
            try:
                analysis = await analyzer.analyze_call(
                    self.mgr.transcript, self.mgr.call_start_time,
                    datetime.datetime.now(), self.mgr.call_uuid,
                    call_duration, lead_saved,
                )
                if call_duration < 60:
                    await db.insert_short_call(
                        self.mgr.call_id, self.mgr.call_uuid, call_duration, analysis)
                else:
                    await db.insert_call_summary(
                        self.mgr.call_id, self.mgr.call_uuid, call_duration, analysis)
                    if not lead_saved:
                        lead = await analyzer.extract_lead_from_transcript(
                            self.mgr.transcript, call_duration, self.mgr.call_uuid)
                        if lead and lead.get("should_save"):
                            await db.insert_lead(lead, self.mgr.call_id)
            except Exception as e:
                print(f"[Cleanup] Analysis error: {e}")

            turns = len([t for t in self.mgr.transcript if t.get("role") in ("USER", "ASSISTANT")])
            await db.finalize_call_record(self.mgr.call_id, turns, None, self.mgr.transcript)

        print("[VoBizSession] Session cleaned up")