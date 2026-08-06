import asyncio
import datetime
import json
import os
import random
from dataclasses import asdict, dataclass, field
from typing import Optional

from .config import settings


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def xp_for_level(level: int) -> int:
    """XP required to go from level-1 to level. Standard MEE6-style curve —
    starts gentle, ramps up so higher levels take meaningfully longer.
    """
    return 5 * (level**2) + 50 * level + 100


def level_for_total_xp(total_xp: int) -> int:
    level = 0
    remaining = total_xp
    while remaining >= xp_for_level(level + 1):
        remaining -= xp_for_level(level + 1)
        level += 1
    return level


@dataclass
class Member:
    user_id: int
    xp: int = 0
    last_message_at: Optional[str] = None


class XpStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self.members: dict[str, Member] = {}

    async def load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.members = {k: Member(**v) for k, v in raw.get("members", {}).items()}

    async def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        payload = {"members": {k: asdict(v) for k, v in self.members.items()}}
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, self._path)

    async def try_add_xp(self, user_id: int) -> Optional[tuple[int, int, int]]:
        """Returns (old_level, new_level, total_xp) if XP was awarded (i.e.
        not on cooldown), or None if this message was on cooldown and
        ignored. Caller decides whether to announce based on old != new.
        """
        async with self._lock:
            key = str(user_id)
            member = self.members.get(key)
            now = datetime.datetime.now(datetime.timezone.utc)
            if member and member.last_message_at:
                last = datetime.datetime.fromisoformat(member.last_message_at)
                if (now - last).total_seconds() < settings.xp_cooldown_seconds:
                    return None

            if member is None:
                member = Member(user_id=user_id)
                self.members[key] = member

            old_level = level_for_total_xp(member.xp)
            member.xp += random.randint(settings.xp_min_per_message, settings.xp_max_per_message)
            member.last_message_at = now.isoformat()
            new_level = level_for_total_xp(member.xp)
            await self._save()
            return old_level, new_level, member.xp

    async def get_rank(self, user_id: int) -> Optional[dict]:
        member = self.members.get(str(user_id))
        if member is None:
            return None
        ordered = sorted(self.members.values(), key=lambda m: m.xp, reverse=True)
        position = next((i + 1 for i, m in enumerate(ordered) if m.user_id == user_id), None)
        return {"xp": member.xp, "level": level_for_total_xp(member.xp), "position": position, "total_members": len(ordered)}

    async def get_leaderboard(self, limit: int = 10) -> list[dict]:
        ordered = sorted(self.members.values(), key=lambda m: m.xp, reverse=True)[:limit]
        return [{"user_id": m.user_id, "xp": m.xp, "level": level_for_total_xp(m.xp)} for m in ordered]


store = XpStore(settings.xp_file)
