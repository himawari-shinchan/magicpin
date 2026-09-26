import os
import json
from datetime import datetime
from typing import Any, Dict, Optional
import time
START_TIME = time.time()
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal
import utils
# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
USE_INMEMORY_STORE = os.getenv('USE_INMEMORY_STORE', 'false').lower() == 'true'

# In‑memory fallback (for local dev when Redis unavailable)
_memory_store: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Pydantic models (request / response schemas)
# ---------------------------------------------------------------------------
from pydantic import RootModel

class ContextPayload(RootModel[Dict[str, Any]]):
    """Root model to accept any JSON payload as a dict."""
    root: Dict[str, Any]

class CtxBody(BaseModel):
    scope: str = Field(..., description="One of 'category', 'merchant', 'trigger', 'customer'")
    context_id: str = Field(..., alias='context_id')
    version: int = Field(..., ge=1)
    payload: ContextPayload
    delivered_at: datetime

    # Scope validation moved to handler

class CtxSuccessResponse(BaseModel):
    accepted: bool = True
    ack_id: str = Field(..., description='ack_<context_id>_v<version>')
    stored_at: datetime

class CtxStaleResponse(BaseModel):
    accepted: bool = False
    reason: Literal["stale_version"] = "stale_version"
    current_version: int

# ---------------------------------------------------------------------------
# Redis / In‑memory store helper
# ---------------------------------------------------------------------------
def _redis_client() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=True)

async def _store_context(key: str, version: int, data: Dict[str, Any]):
    if USE_INMEMORY_STORE:
        _memory_store[key] = {'version': version, 'data': data, 'stored_at': datetime.utcnow().isoformat()}
        return {'stale': False}
    r = _redis_client()
    async with r.pipeline() as pipe:
        while True:
            try:
                await pipe.watch(key)
                existing = await r.hgetall(key)
                if existing:
                    cur_ver = int(existing.get('version', '0'))
                    if version <= cur_ver:
                        await pipe.unwatch()
                        return {'stale': True, 'current_version': cur_ver}
                pipe.multi()
                pipe.hset(key, mapping={
                    'version': version,
                    'data': json.dumps(data),
                    'stored_at': datetime.utcnow().isoformat()
                })
                await pipe.execute()
                return {'stale': False}
            except redis.WatchError:
                continue
            finally:
                await pipe.reset()

async def _get_context(key: str) -> Optional[Dict[str, Any]]:
    if USE_INMEMORY_STORE:
        return _memory_store.get(key)
    r = _redis_client()
    raw = await r.hgetall(key)
    if not raw:
        return None
    raw['version'] = int(raw.get('version', 0))
    raw['data'] = json.loads(raw.get('data', '{}'))
    return raw

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(title='Magicpin Vera Bot', version='0.1.0')

@app.post('/v1/context')
async def post_context(body: CtxBody):
    allowed_scopes = {"category", "merchant", "trigger", "customer"}
    if body.scope not in allowed_scopes:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {allowed_scopes}"}
        )
    key = f'ctx:{body.scope}:{body.context_id}'
    store_result = await _store_context(key, body.version, body.payload.root)
    if isinstance(store_result, dict) and store_result.get('stale'):
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": store_result["current_version"]}
        )
    ack_id = f'ack_{body.context_id}_v{body.version}'
    return JSONResponse(
        status_code=200,
        content={"accepted": True, "ack_id": ack_id, "stored_at": datetime.utcnow().isoformat()}
    )

@app.get('/v1/healthz')
async def healthz():
    uptime_seconds = int(time.time() - START_TIME)
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    if USE_INMEMORY_STORE:
        for key in _memory_store.keys():
            parts = key.split(":")
            if len(parts) >= 3:
                scope = parts[1]
                if scope in counts:
                    counts[scope] += 1
    else:
        r = _redis_client()
        for scope in counts.keys():
            keys = await r.keys(f'ctx:{scope}:*')
            counts[scope] = len(keys)
    return {"status": "ok", "uptime_seconds": uptime_seconds,
            "contexts_loaded": counts}

@app.get('/v1/metadata')
async def metadata():
    return {
        "team_name": "Your Team",
        "team_members": ["Member A", "Member B"],
        "model": "gpt-4o-mini",
        "approach": "Hybrid retrieval + LLM",
        "contact_email": "team@example.com",
        "version": "0.1.0",
        "submitted_at": datetime.utcnow().isoformat()
    }
