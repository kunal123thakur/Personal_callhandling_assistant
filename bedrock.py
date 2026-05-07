
import asyncio
import base64
import datetime
import json
import uuid

from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import Config
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

import db
from config import AWS_REGION, BEDROCK_MODEL_ID, NOVA_RATE, debug_print, VOICE_CLONE
from tools import ToolProcessor
from tts import CartesiaTTSHandler


# ══════════════════════════════════════════════════════════════════════════════
# BedrockStreamManager
# ══════════════════════════════════════════════════════════════════════════════

class BedrockStreamManager:

    # ── JSON shorthand ────────────────────────────────────────────────────────

    @staticmethod
    def _j(obj: dict) -> str:
        return json.dumps(obj)

    # ── Bedrock event builders ────────────────────────────────────────────────

    def _start_session(self) -> str:
        return self._j({"event": {"sessionStart": {"inferenceConfiguration":
            {"maxTokens": 1024, "topP": 0.9, "temperature": 0.1}}}})

    def _prompt_start(self) -> str:
        """
        Build the promptStart event including all tool specs.

        ╔══════════════════════════════════════════════════════╗
        ║  ADD NEW TOOLS HERE — copy a toolSpec block and      ║
        ║  implement the handler in tools.py → _run_tool().    ║
        ╚══════════════════════════════════════════════════════╝
        """
        tools = [
            {"toolSpec": {
                "name": "getDateAndTimeTool",
                "description": "Get current date and time in IST",
                "inputSchema": {"json": self._j({"type": "object", "properties": {}, "required": []})},
            }},
            {"toolSpec": {
                "name": "trackOrderTool",
                "description": "Track order status by order ID",
                "inputSchema": {"json": self._j({
                    "type": "object",
                    "properties": {
                        "orderId":              {"type": "string"},
                        "requestNotifications": {"type": "boolean", "default": False},
                    },
                    "required": ["orderId"],
                })},
            }},
            {"toolSpec": {
                "name": "getProductInfoTool",
                "description": "Get pricing and features for EPACK products.",
                "inputSchema": {"json": self._j({
                    "type": "object",
                    "properties": {
                        "product": {
                            "type": "string",
                            "description": "peb, prefab_structures, lgsf, sandwich_panels, eps_packaging, turnkey_services",
                        },
                    },
                    "required": ["product"],
                })},
            }},
            {"toolSpec": {
                "name": "scheduleDemoTool",
                "description": "Schedule a product demo or site visit.",
                "inputSchema": {"json": self._j({
                    "type": "object",
                    "properties": {
                        "customerName":  {"type": "string"},
                        "companyName":   {"type": "string"},
                        "product":       {"type": "string"},
                        "preferredDate": {"type": "string"},
                    },
                    "required": ["customerName", "product"],
                })},
            }},
            {"toolSpec": {
                "name": "createLeadTool",
                "description": "Save customer lead info at end of call.",
                "inputSchema": {"json": self._j({
                    "type": "object",
                    "properties": {
                        "customerName": {"type": "string"},
                        "companyName":  {"type": "string"},
                        "phone":        {"type": "string"},
                        "email":        {"type": "string"},
                        "interestedIn": {"type": "string"},
                        "budget":       {"type": "string"},
                        "notes":        {"type": "string"},
                    },
                    "required": ["customerName", "companyName"],
                })},
            }},
            {"toolSpec": {
                "name": "forwardCallTool",
                "description": "Forward the call to a department. Use for: hr, sales, support, manager.",
                "inputSchema": {"json": self._j({
                    "type": "object",
                    "properties": {
                        "department": {
                            "type": "string",
                            "enum": ["hr", "sales", "support", "manager"],
                        },
                    },
                    "required": ["department"],
                })},
            }},
            {"toolSpec": {
                "name": "endCallTool",
                "description": "End/hang up the call AFTER saying final goodbye.",
                "inputSchema": {"json": self._j({
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "enum": [
                                "conversation_complete",
                                "customer_busy_callback",
                                "customer_not_interested",
                                "no_requirement",
                                "call_transferred",
                                "audio_issues",
                            ],
                        },
                    },
                    "required": ["reason"],
                })},
            }},
        ]

        return self._j({"event": {"promptStart": {
            "promptName": self.prompt_name,
            "textOutputConfiguration":    {"mediaType": "text/plain"},
            "audioOutputConfiguration": {
                "mediaType":       "audio/lpcm",
                "sampleRateHertz": 24000,
                "sampleSizeBits":  16,
                "channelCount":    1,
                "voiceId":         "matthew",
                "encoding":        "base64",
                "audioType":       "SPEECH",
            },
            "toolUseOutputConfiguration": {"mediaType": "application/json"},
            "toolConfiguration":          {"tools": tools},
        }}})

    def _text_content_start(self, content_name: str, role: str) -> str:
        return self._j({"event": {"contentStart": {
            "promptName": self.prompt_name, "contentName": content_name,
            "type": "TEXT", "role": role, "interactive": False,
            "textInputConfiguration": {"mediaType": "text/plain"},
        }}})

    def _text_input(self, content_name: str, text: str) -> str:
        return self._j({"event": {"textInput": {
            "promptName": self.prompt_name, "contentName": content_name, "content": text,
        }}})

    def _audio_content_start(self) -> str:
        return self._j({"event": {"contentStart": {
            "promptName":  self.prompt_name,
            "contentName": self.audio_content_name,
            "type":        "AUDIO",
            "interactive": True,
            "role":        "USER",
            "audioInputConfiguration": {
                "mediaType":       "audio/lpcm",
                "sampleRateHertz": NOVA_RATE,   # 16 kHz — matches VoBiz directly
                "sampleSizeBits":  16,
                "channelCount":    1,
                "audioType":       "SPEECH",
                "encoding":        "base64",
            },
        }}})

    def _audio_input(self, b64: str) -> str:
        return self._j({"event": {"audioInput": {
            "promptName":  self.prompt_name,
            "contentName": self.audio_content_name,
            "content":     b64,
        }}})

    def _tool_content_start(self, content_name: str, tool_use_id: str) -> str:
        return self._j({"event": {"contentStart": {
            "promptName": self.prompt_name, "contentName": content_name,
            "interactive": False, "type": "TOOL", "role": "TOOL",
            "toolResultInputConfiguration": {
                "toolUseId": tool_use_id, "type": "TEXT",
                "textInputConfiguration": {"mediaType": "text/plain"},
            },
        }}})

    def _tool_result(self, content_name: str, content) -> str:
        payload = json.dumps(content) if isinstance(content, dict) else content
        return self._j({"event": {"toolResult": {
            "promptName": self.prompt_name, "contentName": content_name, "content": payload,
        }}})

    def _content_end(self, content_name: str) -> str:
        return self._j({"event": {"contentEnd": {
            "promptName": self.prompt_name, "contentName": content_name,
        }}})

    def _prompt_end(self) -> str:
        return self._j({"event": {"promptEnd": {"promptName": self.prompt_name}}})

    def _session_end(self) -> str:
        return self._j({"event": {"sessionEnd": {}}})

    # ── Constructor ───────────────────────────────────────────────────────────

    def __init__(self):
        self.audio_input_queue  = asyncio.Queue()
        self.audio_output_queue = asyncio.Queue()   

        self.is_active       = False
        self.bedrock_client  = None
        self.stream_response = None
        self.response_task   = None

        self.display_assistant_text = False
        self.role = None

        self.barge_in_event        = asyncio.Event()
        self.is_assistant_speaking = False

        self.prompt_name        = str(uuid.uuid4())
        self.content_name       = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        self.toolUseContent = ""
        self.toolUseId      = ""
        self.toolName       = ""

        self.tool_processor     = ToolProcessor()
        self.pending_tool_tasks: dict = {}

        self.tts_handler = CartesiaTTSHandler(self.audio_output_queue)

        self.call_id         = None
        self.call_uuid       = None
        self.call_start_time = None
        self.transcript      = []

    # ── Bedrock client init ───────────────────────────────────────────────────

    def _init_client(self):
        cfg = Config(
            endpoint_uri=f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com",
            region=AWS_REGION,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
        )
        self.bedrock_client = BedrockRuntimeClient(cfg)

    # ── Session lifecycle ─────────────────────────────────────────────────────

    async def initialize(self, system_prompt: str):
        if not self.bedrock_client:
            self._init_client()
        self.stream_response = await self.bedrock_client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=BEDROCK_MODEL_ID)
        )
        self.is_active = True
        print("hi")
        for event in [
            self._start_session(),
            self._prompt_start(),
            self._text_content_start(self.content_name, "SYSTEM"),
            self._text_input(self.content_name, system_prompt),
            self._content_end(self.content_name),
        ]:

            await self._send(event)
            await asyncio.sleep(0.05)
        print("hello")

        self.response_task = asyncio.create_task(self._process_responses())
        asyncio.create_task(self._drain_audio_input())
        if VOICE_CLONE:
            self.tts_handler.start()
        return self

    async def _send(self, event_json: str):
        if not self.stream_response or not self.is_active:
            return
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode())
        )
        try:
            await self.stream_response.input_stream.send(chunk)
        except Exception as e:
            debug_print(f"send error: {e}")

    async def send_audio_content_start(self):
        await self._send(self._audio_content_start())

    # ── Audio ingestion ───────────────────────────────────────────────────────

    def add_audio_chunk(self, pcm16_bytes: bytes):
        """Queue 16 kHz PCM from VoBiz directly to Nova Sonic (no conversion needed)."""
        self.audio_input_queue.put_nowait(pcm16_bytes)

    async def _drain_audio_input(self):
        while self.is_active:
            try:
                pcm = await self.audio_input_queue.get()
                b64 = base64.b64encode(pcm).decode()
                await self._send(self._audio_input(b64))
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_print(f"audio drain error: {e}")

    # ── Barge-in ──────────────────────────────────────────────────────────────

    def trigger_barge_in(self):
        print("[Barge-in] User interrupted — stopping assistant audio.")
        self.is_assistant_speaking = False
        self.tts_handler.interrupt()
        self.barge_in_event.set()

    # ── Response processing ───────────────────────────────────────────────────

    async def _process_responses(self):
        try:
            while self.is_active:
                try:
                    output = await self.stream_response.await_output()
                    result = await output[1].receive()
                    if not (result.value and result.value.bytes_):
                        continue
                    json_data = json.loads(result.value.bytes_.decode())
                    ev = json_data.get("event", {})

                    if "contentStart" in ev:
                        cs = ev["contentStart"]
                        self.role = cs.get("role")
                        if self.role == "ASSISTANT":
                            self.is_assistant_speaking = True
                            self.barge_in_event.clear()
                            self.tts_handler._clear_interrupted()
                        if "additionalModelFields" in cs:
                            try:
                                add = json.loads(cs["additionalModelFields"])
                                self.display_assistant_text = (
                                    add.get("generationStage") == "SPECULATIVE"
                                )
                            except Exception:
                                pass

                    elif "textOutput" in ev:
                        text = ev["textOutput"]["content"]
                        if '{ "interrupted" : true }' in text:
                            self.trigger_barge_in()
                            continue
                        if self.role == "USER":
                            if self.is_assistant_speaking:
                                self.trigger_barge_in()
                            print(f"User: {text}")
                            if self.call_id:
                                ts = datetime.datetime.now().strftime("%H:%M:%S")
                                self.transcript.append({"role": "USER", "text": text, "timestamp": ts})
                                asyncio.create_task(db.insert_transcript_entry(
                                    self.call_id, "USER", ts, text=text))
                        elif self.role == "ASSISTANT" and self.display_assistant_text:
                            if not self.tts_handler._is_interrupted():
                                print(f"Assistant: {text}")
                                if VOICE_CLONE:
                                    self.tts_handler.push_text(text)
                                if self.call_id:
                                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                                    self.transcript.append({"role": "ASSISTANT", "text": text, "timestamp": ts})
                                    asyncio.create_task(db.insert_transcript_entry(
                                        self.call_id, "ASSISTANT", ts, text=text))

                    elif "audioOutput" in ev:
                        if not VOICE_CLONE:
                            try:
                                audio_bytes = base64.b64decode(ev["audioOutput"]["content"])
                                self.audio_output_queue.put_nowait(audio_bytes)
                            except Exception as e:
                                debug_print(f"Error handling audioOutput: {e}")

                    elif "toolUse" in ev:
                        self.toolUseContent = ev["toolUse"]
                        self.toolName       = ev["toolUse"]["toolName"]
                        self.toolUseId      = ev["toolUse"]["toolUseId"]
                        debug_print(f"Tool use: {self.toolName}")

                    elif "contentEnd" in ev:
                        if ev["contentEnd"].get("type") == "TOOL":
                            self._handle_tool(self.toolName, self.toolUseContent, self.toolUseId)
                        elif self.role == "ASSISTANT":
                            self.tts_handler.flush()

                    elif "completionEnd" in ev:
                        debug_print("completionEnd received")

                except StopAsyncIteration:
                    break
                except Exception as e:
                    if self.is_active:
                        print(f"[Bedrock] response error: {e}")
                    break

        except Exception as e:
            print(f"[Bedrock] processor error: {e}")
        finally:
            self.is_active = False

    # ── Tool execution ────────────────────────────────────────────────────────

    def _handle_tool(self, name: str, content, use_id: str):
        cn   = str(uuid.uuid4())
        task = asyncio.create_task(self._exec_tool(name, content, use_id, cn))
        self.pending_tool_tasks[cn] = task
        task.add_done_callback(lambda t: self.pending_tool_tasks.pop(cn, None))

    async def _exec_tool(self, name: str, content, use_id: str, cn: str):
        try:
            result = await self.tool_processor.process_tool_async(name, content)
            if self.call_id:
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                self.transcript.append({"role": "TOOL", "tool": name, "result": result, "timestamp": ts})
                asyncio.create_task(db.insert_transcript_entry(
                    self.call_id, "TOOL", ts, tool_name=name, tool_result=result))
            if name.lower() == "createleadtool" and result.get("saved") and self.call_id:
                lead_data = content.get("content", {})
                if isinstance(lead_data, str):
                    try:
                        lead_data = json.loads(lead_data)
                    except Exception:
                        lead_data = {}
                lead_data["id"] = result.get("leadId")
                asyncio.create_task(db.insert_lead(lead_data, self.call_id))
            await self._send(self._tool_content_start(cn, use_id))
            await self._send(self._tool_result(cn, result))
            await self._send(self._content_end(cn))
        except Exception as e:
            debug_print(f"tool error [{name}]: {e}")
            try:
                await self._send(self._tool_content_start(cn, use_id))
                await self._send(self._tool_result(cn, {"error": str(e)}))
                await self._send(self._content_end(cn))
            except Exception:
                pass

    # ── Teardown ──────────────────────────────────────────────────────────────

    async def close(self):
        if not self.is_active:
            return
        for t in self.pending_tool_tasks.values():
            t.cancel()
        if self.response_task and not self.response_task.done():
            self.response_task.cancel()
        if VOICE_CLONE:
            self.tts_handler.stop()
        await self._send(self._content_end(self.audio_content_name))
        await self._send(self._prompt_end())
        await self._send(self._session_end())
        if self.stream_response:
            await self.stream_response.input_stream.close()
        self.is_active = False