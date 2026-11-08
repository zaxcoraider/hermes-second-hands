"""Second Hands Desktop backend: /api/plugins/second-hands/*.

The pane posts camera frames and button presses here; the agent-side worker
(engine.py) picks them up from the same data folder and writes state.json back.
Reads and writes stay inside <hermes home>/plugin-data/second-hands/.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

PLUGIN = "second-hands"
MAX_FRAME_BYTES = 1_500_000
COMMANDS = {"begin", "override", "dismiss", "skip", "back", "finish"}


def _root() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN)


def _dir(sid: str) -> Path:
    if not sid or not sid.isalnum() or len(sid) > 32:
        raise HTTPException(status_code=400, detail="bad session id")
    d = _root() / "sessions" / sid
    if not (d / "state.json").exists():
        raise HTTPException(status_code=404, detail="no such session")
    return d


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _view(d: Path) -> dict:
    state = _read(d / "state.json", {}) or {}
    hb = (_read(d / "heartbeat.json", {}) or {}).get("at", 0)
    state.pop("observations", None)
    state["worker_alive"] = int(time.time() * 1000) - hb < 8000
    return state


@router.get("/active")
async def active():
    sid = (_read(_root() / "active.json", {}) or {}).get("id")
    if not sid:
        return {"session": None}
    try:
        return {"session": _view(_dir(sid))}
    except HTTPException:
        return {"session": None}


@router.get("/state/{sid}")
async def state(sid: str):
    return {"session": _view(_dir(sid))}


@router.post("/frame/{sid}")
async def frame(sid: str, body: dict):
    d = _dir(sid)
    raw = str(body.get("image", ""))
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        jpeg = base64.b64decode(raw, validate=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="image must be base64 JPEG")
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_FRAME_BYTES:
        raise HTTPException(status_code=400, detail="image must be a JPEG under 1.5 MB")
    tmp = d / f"frame.{os.getpid()}.tmp"
    tmp.write_bytes(jpeg)
    os.replace(tmp, d / "frame.jpg")
    seq = int((_read(d / "frame.json", {}) or {}).get("seq", 0)) + 1
    _write(d / "frame.json", {"seq": seq, "at": int(time.time() * 1000)})
    return {"ok": True, "seq": seq}


@router.post("/command/{sid}")
async def command(sid: str, body: dict):
    d = _dir(sid)
    kind = body.get("type")
    if kind not in COMMANDS:
        raise HTTPException(status_code=400, detail="unknown command")
    with open(d / "commands.jsonl", "a") as f:
        f.write(json.dumps({"type": kind, "at": int(time.time() * 1000)}) + "\n")
    return {"ok": True}


@router.get("/thumb/{sid}/{eid}")
async def thumb(sid: str, eid: str):
    d = _dir(sid)
    if not eid.isalnum():
        raise HTTPException(status_code=400, detail="bad id")
    try:
        data = (d / "thumbs" / f"{eid}.jpg").read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="no thumbnail")
    return {"image": "data:image/jpeg;base64," + base64.b64encode(data).decode()}
