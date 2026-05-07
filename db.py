"""
db.py — NeonDB (PostgreSQL) integration for EPACK Priya Voice Agent (Orbit)
Tables: calls, transcript_entries, leads, call_summaries, short_calls
"""

import os
import asyncio
import json
import datetime
from typing import List, Dict, Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "")
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set in .env")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=30)
        print("✅ NeonDB connection pool created")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        print("🔌 NeonDB pool closed")


CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS calls (
    id            SERIAL PRIMARY KEY,
    call_uuid     TEXT        NOT NULL DEFAULT '',
    agent         TEXT        NOT NULL DEFAULT 'Priya',
    company       TEXT        NOT NULL DEFAULT 'EPACK Prefab',
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at      TIMESTAMPTZ,
    total_turns   INTEGER     NOT NULL DEFAULT 0,
    forward_dept  TEXT,
    raw_json      JSONB
);

CREATE TABLE IF NOT EXISTS transcript_entries (
    id          SERIAL PRIMARY KEY,
    call_id     INTEGER     NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    role        TEXT        NOT NULL,
    text        TEXT,
    tool_name   TEXT,
    tool_result JSONB,
    ts          TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS leads (
    id              SERIAL PRIMARY KEY,
    lead_id         TEXT        NOT NULL,
    call_id         INTEGER     REFERENCES calls(id),
    customer_name   TEXT        NOT NULL DEFAULT '',
    company_name    TEXT        NOT NULL DEFAULT '',
    phone           TEXT,
    email           TEXT,
    interested_in   TEXT,
    budget          TEXT,
    location        TEXT,
    callback_time   TEXT,
    notes           TEXT,
    status          TEXT        NOT NULL DEFAULT 'New Lead',
    source          TEXT        NOT NULL DEFAULT 'agent_tool',
    lead_quality    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS call_summaries (
    id                      SERIAL PRIMARY KEY,
    call_id                 INTEGER     REFERENCES calls(id) ON DELETE CASCADE,
    call_uuid               TEXT        NOT NULL DEFAULT '',
    call_duration_secs      FLOAT       NOT NULL DEFAULT 0,
    summary                 TEXT,
    call_outcome            TEXT,
    end_reason              TEXT,
    end_reason_detail       TEXT,
    customer_intent         TEXT,
    jitter_detected         BOOLEAN     NOT NULL DEFAULT FALSE,
    jitter_evidence         TEXT,
    communication_quality   TEXT,
    agent_performance_score INTEGER,
    agent_performance_label TEXT,
    agent_issues            TEXT,
    customer_sentiment      TEXT,
    customer_name_detected  TEXT,
    customer_location       TEXT,
    callback_time_promised  TEXT,
    transfer_was_requested  BOOLEAN     NOT NULL DEFAULT FALSE,
    transfer_was_completed  BOOLEAN     NOT NULL DEFAULT FALSE,
    transfer_department     TEXT,
    topics_discussed        JSONB,
    products_mentioned      JSONB,
    follow_up_required      BOOLEAN     NOT NULL DEFAULT FALSE,
    follow_up_notes         TEXT,
    recommendations         TEXT,
    is_short_call           BOOLEAN     NOT NULL DEFAULT FALSE,
    is_busy_callback        BOOLEAN     NOT NULL DEFAULT FALSE,
    quality_score           INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS short_calls (
    id                  SERIAL PRIMARY KEY,
    call_id             INTEGER     REFERENCES calls(id) ON DELETE CASCADE,
    call_uuid           TEXT        NOT NULL DEFAULT '',
    call_duration_secs  FLOAT       NOT NULL DEFAULT 0,
    reason              TEXT,
    callback_time       TEXT,
    customer_name       TEXT,
    customer_location   TEXT,
    phone               TEXT,
    notes               TEXT,
    follow_up_required  BOOLEAN     NOT NULL DEFAULT TRUE,
    follow_up_done      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_calls_call_uuid          ON calls(call_uuid);
CREATE INDEX IF NOT EXISTS idx_calls_started_at         ON calls(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_created_at         ON leads(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_te_call_id               ON transcript_entries(call_id);
CREATE INDEX IF NOT EXISTS idx_call_summaries_call_id   ON call_summaries(call_id);
CREATE INDEX IF NOT EXISTS idx_short_calls_call_id      ON short_calls(call_id);
"""

MIGRATE_SQL = """
ALTER TABLE leads ADD COLUMN IF NOT EXISTS location        TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS callback_time   TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS lead_quality    TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS source          TEXT;
UPDATE leads SET source = 'agent_tool' WHERE source IS NULL;
"""


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)
        for stmt in MIGRATE_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    await conn.execute(stmt)
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        print(f"⚠️  Migration: {e}")
    print("✅ NeonDB tables ready")


async def create_call_record(call_uuid: str = "") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO calls (call_uuid, started_at) VALUES ($1, NOW()) RETURNING id",
            call_uuid)
    call_id = row["id"]
    print(f"🗄️  Call record created: id={call_id}, uuid={call_uuid}")
    return call_id


async def finalize_call_record(call_id: int, total_turns: int,
                                forward_dept: Optional[str], transcript_json: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE calls SET ended_at=NOW(), total_turns=$2, forward_dept=$3, raw_json=$4::jsonb WHERE id=$1",
            call_id, total_turns, forward_dept,
            json.dumps(transcript_json, ensure_ascii=False))


async def insert_transcript_entry(call_id: int, role: str, ts: str,
                                   text: Optional[str] = None,
                                   tool_name: Optional[str] = None,
                                   tool_result: Optional[dict] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO transcript_entries (call_id, role, text, tool_name, tool_result, ts) VALUES ($1,$2,$3,$4,$5::jsonb,$6)",
            call_id, role, text, tool_name,
            json.dumps(tool_result, ensure_ascii=False) if tool_result else None, ts)


async def insert_lead(lead: dict, call_id: Optional[int] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO leads (lead_id, call_id, customer_name, company_name,
                phone, email, interested_in, budget, location, callback_time,
                notes, status, source, lead_quality, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
            lead.get("id", ""), call_id,
            lead.get("name", lead.get("customer_name", "")),
            lead.get("company", lead.get("company_name", "")),
            lead.get("phone", ""), lead.get("email", ""),
            lead.get("interestedIn", lead.get("interested_in", "")),
            lead.get("budget", ""), lead.get("location", ""),
            lead.get("callback_time", ""),
            lead.get("notes", lead.get("requirement_summary", "")),
            lead.get("status", "New Lead"),
            lead.get("source", "agent_tool"),
            lead.get("lead_quality", ""),
            datetime.datetime.now())
    print(f"🗄️  Lead saved: {lead.get('name', '?')} / {lead.get('company', '?')}")


async def insert_call_summary(call_id: Optional[int], call_uuid: str,
                               call_duration_secs: float, analysis: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO call_summaries (
                call_id, call_uuid, call_duration_secs,
                summary, call_outcome, end_reason, end_reason_detail, customer_intent,
                jitter_detected, jitter_evidence, communication_quality,
                agent_performance_score, agent_performance_label, agent_issues,
                customer_sentiment, customer_name_detected, customer_location, callback_time_promised,
                transfer_was_requested, transfer_was_completed, transfer_department,
                topics_discussed, products_mentioned,
                follow_up_required, follow_up_notes, recommendations,
                is_short_call, is_busy_callback, quality_score, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22::jsonb,$23::jsonb,$24,$25,$26,$27,$28,$29,NOW())""",
            call_id, call_uuid, call_duration_secs,
            analysis.get("summary", ""), analysis.get("call_outcome", ""),
            analysis.get("end_reason", ""), analysis.get("end_reason_detail", ""),
            analysis.get("customer_intent", ""),
            bool(analysis.get("jitter_detected", False)), analysis.get("jitter_evidence", ""),
            analysis.get("communication_quality", ""),
            analysis.get("agent_performance_score"), analysis.get("agent_performance_label", ""),
            analysis.get("agent_issues", ""), analysis.get("customer_sentiment", ""),
            analysis.get("customer_name_detected", ""), analysis.get("customer_location", ""),
            analysis.get("callback_time_promised", ""),
            bool(analysis.get("transfer_was_requested", False)),
            bool(analysis.get("transfer_was_completed", False)),
            analysis.get("transfer_department", ""),
            json.dumps(analysis.get("topics_discussed", []), ensure_ascii=False),
            json.dumps(analysis.get("products_mentioned", []), ensure_ascii=False),
            bool(analysis.get("follow_up_required", False)),
            analysis.get("follow_up_notes", ""), analysis.get("recommendations", ""),
            bool(analysis.get("is_short_call", False)),
            bool(analysis.get("is_busy_callback", False)),
            analysis.get("quality_score"))
    print(f"🗄️  Call summary saved: call_id={call_id} | outcome={analysis.get('call_outcome')}")


async def insert_short_call(call_id: Optional[int], call_uuid: str,
                             call_duration_secs: float, analysis: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO short_calls (
                call_id, call_uuid, call_duration_secs,
                reason, callback_time, customer_name, customer_location, phone,
                notes, follow_up_required, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())""",
            call_id, call_uuid, call_duration_secs,
            analysis.get("end_reason", "unknown"), analysis.get("callback_time_promised", ""),
            analysis.get("customer_name_detected", ""), analysis.get("customer_location", ""),
            "", analysis.get("summary", ""), True)
    print(f"🗄️  Short-call record saved: call_id={call_id}")


async def get_recent_calls(limit: int = 20) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, call_uuid, started_at, ended_at, total_turns, forward_dept FROM calls ORDER BY started_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def get_recent_leads(limit: int = 50) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM leads ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def get_call_summaries(limit: int = 50) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM call_summaries ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def get_short_calls_pending(limit: int = 50) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM short_calls WHERE follow_up_required=TRUE AND follow_up_done=FALSE ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]
