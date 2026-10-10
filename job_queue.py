from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger("uz_navo.queue")


class MediaJobQueue:
    def __init__(self, redis_url: str | None = None):
        self.redis_url = (redis_url or os.getenv("REDIS_URL", "")).strip()
        self.queue_name = os.getenv("MEDIA_QUEUE_NAME", "uznavo:media:jobs")
        self.cache_ttl = int(os.getenv("MEDIA_CACHE_TTL_SECONDS", "604800"))
        self._redis = None

    @property
    def enabled(self) -> bool:
        return bool(self.redis_url)

    async def _client(self):
        if not self.enabled:
            raise RuntimeError("REDIS_URL sozlanmagan")
        if self._redis is None:
            from redis.asyncio import from_url
            self._redis = from_url(self.redis_url, decode_responses=True, health_check_interval=30)
        return self._redis

    async def ping(self) -> bool:
        if not self.enabled:
            return False
        try:
            redis = await self._client()
            return bool(await redis.ping())
        except Exception:
            logger.exception("Redis ping failed")
            return False

    async def enqueue(self, job: dict[str, Any]) -> str:
        redis = await self._client()
        payload = dict(job)
        payload.setdefault("job_id", uuid.uuid4().hex)
        payload.setdefault("attempt", 0)
        await redis.rpush(self.queue_name, json.dumps(payload, ensure_ascii=False))
        return payload["job_id"]

    async def dequeue(self, timeout: int = 5) -> dict[str, Any] | None:
        redis = await self._client()
        result = await redis.blpop(self.queue_name, timeout=timeout)
        if not result:
            return None
        _, payload = result
        return json.loads(payload)

    async def requeue(self, job: dict[str, Any]) -> None:
        redis = await self._client()
        await redis.rpush(self.queue_name, json.dumps(job, ensure_ascii=False))

    @staticmethod
    def media_cache_key(action: str, url: str, effect: str | None = None) -> str:
        raw = f"{action}|{effect or ''}|{url}".encode("utf-8")
        return "uznavo:media:cache:" + hashlib.sha256(raw).hexdigest()

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            redis = await self._client()
            value = await redis.get(key)
            return json.loads(value) if value else None
        except Exception:
            logger.exception("Redis cache_get failed")
            return None

    async def cache_set(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            redis = await self._client()
            await redis.set(key, json.dumps(value, ensure_ascii=False), ex=self.cache_ttl)
        except Exception:
            logger.exception("Redis cache_set failed")

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
