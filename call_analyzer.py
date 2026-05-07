"""
call_analyzer.py — Post-call intelligence using Bedrock Nova Lite
Extracts leads, generates summaries, flags short calls.
"""

import os
import json
import asyncio
from typing import List, Dict, Optional

import boto3
from dotenv import load_dotenv

load_dotenv()

PRIMARY_MODEL  = "us.amazon.nova-lite-v1:0"
FALLBACK_MODEL = "us.amazon.nova-lite-v1:0"


class BedrockAnalyzer:
    def __init__(self):
        self.bedrock = boto3.client(
            "bedrock-runtime",
            region_name="us-west-2",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"))

    def _invoke(self, prompt: str, system: str = (
            "You are an expert call analytics AI. Respond with ONLY valid JSON.")) -> str:
        body = json.dumps({
            "messages": [
                {"role": "user", "content": [{"text": prompt}]}
            ],
            "system": [{"text": system}],
            "inferenceConfig": {
                "maxTokens": 4096,
                "temperature": 0.1
            }
        })
        try:
            r = self.bedrock.invoke_model(modelId=PRIMARY_MODEL, body=body)
        except Exception:
            try:
                r = self.bedrock.invoke_model(modelId=FALLBACK_MODEL, body=body)
            except Exception as e:
                print(f"[Analyzer] Both models failed: {e}")
                return ""
        response_body = json.loads(r["body"].read())
        return response_body["output"]["message"]["content"][0]["text"].strip()

    # ✅ This method was missing — that's what caused the crash
    async def _invoke_async(self, prompt: str) -> str:
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._invoke(prompt)
            )
        except asyncio.CancelledError:
            print("[Analyzer] Async task was cancelled (call ended early)")
            return ""

    @staticmethod
    def _to_text(transcript: List[Dict]) -> str:
        lines = []
        for e in transcript:
            if e.get("role") == "TOOL":
                lines.append(f"[TOOL: {e.get('tool','')} → {json.dumps(e.get('result',{}), ensure_ascii=False)[:120]}]")
            elif e.get("text"):
                label = "Priya" if e["role"] == "ASSISTANT" else "Customer"
                lines.append(f"{label} [{e.get('timestamp','')}]: {e['text']}")
        return "\n".join(lines)

    @staticmethod
    def _clean(raw: str) -> str:
        raw = raw.strip()
        for f in ("```json", "```"):
            if raw.startswith(f): raw = raw[len(f):]
        if raw.endswith("```"): raw = raw[:-3]
        return raw.strip()

    async def extract_lead_from_transcript(self, transcript, call_duration_seconds, call_uuid) -> Optional[Dict]:
        if call_duration_seconds < 60:
            return None
        text = self._to_text(transcript)
        if not text:
            return None
        prompt = f"""Extract lead info from this B2B sales call transcript.
TRANSCRIPT:
{text}

Return ONLY JSON:
{{"should_save":true,"customer_name":"","company_name":"","phone":"","email":"",
"interested_in":"","requirement_summary":"","budget":"","location":"",
"callback_time":"","lead_quality":"hot/warm/cold","notes":"","extraction_confidence":"high/medium/low"}}"""
        try:
            raw = await self._invoke_async(prompt)
            if not raw:
                return None
            data = json.loads(self._clean(raw))
            if data.get("should_save"):
                data["source"] = "llm_extraction"
                return data
        except Exception as e:
            print(f"[LeadExtractor] {e}")
        return None

    async def analyze_call(self, transcript, call_start, call_end, call_uuid,
                           call_duration_seconds, lead_was_saved_by_tool) -> Dict:
        text = self._to_text(transcript)
        tool_names = [e.get("tool", "") for e in transcript if e.get("role") == "TOOL"]
        prompt = f"""Analyze this AI sales call.
Duration: {call_duration_seconds:.0f}s | Tools: {tool_names or 'none'} | Lead saved by tool: {lead_was_saved_by_tool}

TRANSCRIPT:
{text or '[empty]'}

Return ONLY JSON:
{{"summary":"","call_outcome":"completed_lead_captured/completed_no_lead/transferred/dropped/busy_callback/too_short/no_conversation",
"end_reason":"user_completed/user_busy_callback/user_wanted_human/connection_jitter/call_dropped/very_short_call/unknown",
"end_reason_detail":"","jitter_detected":false,"jitter_evidence":"","communication_quality":"good/fair/poor",
"agent_performance_score":7,"agent_performance_label":"good/fair/poor","agent_issues":"",
"customer_sentiment":"positive/neutral/negative/unknown","customer_intent":"",
"follow_up_required":true,"follow_up_notes":"","topics_discussed":[],"products_mentioned":[],
"customer_name_detected":"","customer_location":"","callback_time_promised":"",
"transfer_was_requested":false,"transfer_was_completed":false,"transfer_department":"",
"is_short_call":{str(call_duration_seconds < 60).lower()},"is_busy_callback":false,
"quality_score":7,"recommendations":""}}"""
        try:
            raw = await self._invoke_async(prompt)
            if not raw:
                raise ValueError("Empty response from model")
            return json.loads(self._clean(raw))
        except Exception as e:
            print(f"[Analyzer] {e}")
            return {
                "summary": "Analysis unavailable", "call_outcome": "unknown",
                "end_reason": "unknown", "is_short_call": call_duration_seconds < 60,
                "is_busy_callback": False, "quality_score": 0, "jitter_detected": False,
                "agent_performance_score": 0, "follow_up_required": False,
                "transfer_was_requested": False, "transfer_was_completed": False
            }