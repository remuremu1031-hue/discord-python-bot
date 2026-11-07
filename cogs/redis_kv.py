"""cog 共通の小さなRedisヘルパー（main.py の bot.kv を使う）。
Redis が未設定・一時的に失敗したときは、メモリに保存して動き続ける（再起動で消える）。"""
from __future__ import annotations

import json
import time

_mem: dict[str, object] = {}
_exp: dict[str, float] = {}


def _alive(key: str) -> bool:
    if key in _exp and _exp[key] < time.time():
        _mem.pop(key, None)
        _exp.pop(key, None)
    return key in _mem


async def _kv(bot, op, *args):
    if getattr(bot, "kv_ready", lambda: False)():
        try:
            return True, await bot.kv(op, *args)
        except Exception as e:
            print(f"Redisの操作に失敗（{op}）: {type(e).__name__}")
    return False, None


async def get_json(bot, key: str, default=None):
    ok, v = await _kv(bot, "get", key)
    if ok:
        try:
            return json.loads(v) if v else default
        except Exception:
            return default
    return _mem.get(key, default) if _alive(key) else default


async def set_json(bot, key: str, value, ttl: int | None = None) -> None:
    raw = json.dumps(value, ensure_ascii=False)
    ok, _ = await _kv(bot, "set", key, raw, *(["ex", ttl] if ttl else []))
    if not ok:
        _mem[key] = value
        if ttl:
            _exp[key] = time.time() + ttl


async def delete(bot, key: str) -> None:
    ok, _ = await _kv(bot, "delete", key)
    if not ok:
        _mem.pop(key, None)


async def incr(bot, key: str) -> int:
    ok, v = await _kv(bot, "incr", key)
    if ok:
        return int(v)
    _mem[key] = int(_mem.get(key, 0) or 0) + 1
    return _mem[key]


async def get_int(bot, key: str) -> int:
    ok, v = await _kv(bot, "get", key)
    if ok:
        return int(v or 0)
    return int(_mem.get(key, 0) or 0)


async def hgetall(bot, key: str) -> dict[str, dict]:
    ok, v = await _kv(bot, "hgetall", key)
    src = v if ok else _mem.get(key, {})
    out = {}
    for k, val in (src or {}).items():
        try:
            out[k] = json.loads(val) if isinstance(val, str) else val
        except Exception:
            pass
    return out


async def hset(bot, key: str, field: str, value) -> None:
    raw = json.dumps(value, ensure_ascii=False)
    ok, _ = await _kv(bot, "hset", key, field, raw)
    if not ok:
        _mem.setdefault(key, {})[field] = raw


async def hdel(bot, key: str, field: str) -> None:
    ok, _ = await _kv(bot, "hdel", key, field)
    if not ok:
        _mem.get(key, {}).pop(field, None)
