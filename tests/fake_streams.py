"""Minimal in-memory Redis Streams for tests (consumer groups, pending, autoclaim)."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from redis.exceptions import ResponseError


def _seq(entry_id: str) -> int:
    return int(str(entry_id).split("-")[0])


class FakeStreams:
    def __init__(self) -> None:
        self.streams: Dict[str, List[Tuple[str, Dict[str, str]]]] = {}
        self.groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.lists: Dict[str, List[str]] = {}
        self._next = 0
        self.now_ms = time.time() * 1000

    async def xgroup_create(self, name, groupname, id="$", mkstream=False):
        if (name, groupname) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.streams.setdefault(name, [])
        self.groups[(name, groupname)] = {"last": 0, "pending": {}}
        return True

    async def xadd(self, name, fields, id="*", maxlen=None, approximate=True):
        self._next += 1
        entry_id = f"{self._next}-0"
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        return entry_id

    async def xreadgroup(self, groupname, consumername, streams, count=None, block=None, noack=False):
        out = []
        for name in streams:
            group = self.groups[(name, groupname)]
            delivered = []
            for entry_id, fields in self.streams.get(name, []):
                if _seq(entry_id) <= group["last"]:
                    continue
                group["last"] = _seq(entry_id)
                group["pending"][entry_id] = {"consumer": consumername, "at": self.now_ms, "count": 1}
                delivered.append((entry_id, fields))
                if count and len(delivered) >= count:
                    break
            if delivered:
                out.append([name, delivered])
                break
        return out

    async def xautoclaim(self, name, groupname, consumername, min_idle_time, start_id="0-0", count=None, justid=False):
        group = self.groups[(name, groupname)]
        claimed = []
        entries = dict(self.streams.get(name, []))
        for entry_id, info in sorted(group["pending"].items(), key=lambda kv: _seq(kv[0])):
            if self.now_ms - info["at"] < min_idle_time:
                continue
            info.update(consumer=consumername, at=self.now_ms, count=info["count"] + 1)
            claimed.append((entry_id, entries.get(entry_id)))
            if count and len(claimed) >= count:
                break
        return ["0-0", claimed, []]

    async def xpending_range(self, name, groupname, min, max, count, consumername=None, idle=None):
        group = self.groups[(name, groupname)]
        rows = []
        for entry_id, info in group["pending"].items():
            if _seq(min) <= _seq(entry_id) <= _seq(max):
                rows.append({"message_id": entry_id, "consumer": info["consumer"], "times_delivered": info["count"]})
        return rows[:count]

    async def xack(self, name, groupname, *ids):
        group = self.groups[(name, groupname)]
        return sum(1 for entry_id in ids if group["pending"].pop(entry_id, None) is not None)

    async def xdel(self, name, *ids):
        before = len(self.streams.get(name, []))
        self.streams[name] = [e for e in self.streams.get(name, []) if e[0] not in ids]
        return before - len(self.streams[name])

    async def xlen(self, name):
        return len(self.streams.get(name, []))

    async def xrevrange(self, name, max="+", min="-", count=None):
        rows = list(reversed(self.streams.get(name, [])))
        return rows[:count] if count else rows

    async def xrange(self, name, min="-", max="+", count=None):
        rows = [e for e in self.streams.get(name, []) if (min == "-" or _seq(e[0]) >= _seq(min)) and (max == "+" or _seq(e[0]) <= _seq(max))]
        return rows[:count] if count else rows

    async def xinfo_groups(self, name):
        out = []
        for (stream, group_name), group in self.groups.items():
            if stream != name:
                continue
            lag = sum(1 for e in self.streams.get(name, []) if _seq(e[0]) > group["last"])
            consumers = {info["consumer"] for info in group["pending"].values()}
            out.append({"name": group_name, "pending": len(group["pending"]), "consumers": len(consumers), "lag": lag})
        return out

    async def rpop(self, name) -> Optional[str]:
        items = self.lists.get(name) or []
        return items.pop() if items else None

    async def lpush(self, name, value):
        self.lists.setdefault(name, []).insert(0, value)
