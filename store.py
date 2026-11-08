"""Session files under <hermes home>/plugin-data/second-hands/.

The Desktop pane (through dashboard/plugin_api.py) and the agent-side worker
share state only through these files, so they work whether or not they run in
the same process. Layout per session:

    sessions/<id>/state.json      whole session (worker writes, pane reads)
    sessions/<id>/frame.jpg       newest camera frame (pane writes)
    sessions/<id>/frame.json      {"seq": n, "at": ms} for frame.jpg
    sessions/<id>/commands.jsonl  button presses from the pane (worker drains)
    sessions/<id>/thumbs/<eid>.jpg frame that triggered an alert
    active.json                   {"id": <session id>} for the pane to find
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

PLUGIN = "second-hands"


def root() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN)


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def session_dir(sid: str) -> Path:
    if not sid.isalnum():
        raise ValueError("bad session id")
    d = root() / "sessions" / sid
    (d / "thumbs").mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def load(sid: str) -> dict | None:
    return read_json(session_dir(sid) / "state.json")


def save(state: dict) -> None:
    state["updated_at"] = now_ms()
    write_json(session_dir(state["id"]) / "state.json", state)


def set_active(sid: str | None) -> None:
    write_json(root() / "active.json", {"id": sid})


def active_id() -> str | None:
    return (read_json(root() / "active.json", {}) or {}).get("id")


def frame_seq(sid: str) -> int:
    return int((read_json(session_dir(sid) / "frame.json", {}) or {}).get("seq", 0))


def read_frame(sid: str) -> bytes | None:
    try:
        return (session_dir(sid) / "frame.jpg").read_bytes()
    except OSError:
        return None


def drain_commands(sid: str) -> list[dict]:
    d = session_dir(sid)
    src, work = d / "commands.jsonl", d / f"commands.{os.getpid()}.work"
    try:
        os.replace(src, work)
    except FileNotFoundError:
        return []
    out = []
    for line in work.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    work.unlink(missing_ok=True)
    return out


def heartbeat(sid: str) -> None:
    write_json(session_dir(sid) / "heartbeat.json", {"at": now_ms()})


def put_thumb(sid: str, eid: str, jpeg: bytes) -> None:
    (session_dir(sid) / "thumbs" / f"{eid}.jpg").write_bytes(jpeg)
