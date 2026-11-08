"""Second Hands for Hermes: registration.

Agent side: three tools, a /repair slash command and three auxiliary model
slots. Desktop side (desktop/plugin.js + dashboard/plugin_api.py): the live
camera pane. The two halves meet in the plugin's data folder (see store.py).
"""
from __future__ import annotations

import json
import logging
import threading

from . import engine, store

log = logging.getLogger(__name__)

_workers: dict[str, engine.Worker] = {}
_lock = threading.Lock()
_ctx = None


def _emit(event: str, payload: dict) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(engine.PLUGIN, event, payload)
    except Exception:  # no Desktop attached (CLI, cron): the pane polls anyway
        pass


def _demo_default() -> bool:
    try:
        return bool(_ctx.get_config("demo_mode", default=False))
    except Exception:
        return False


def _ensure_worker(sid: str) -> None:
    with _lock:
        w = _workers.get(sid)
        if w and w.alive():
            return
        w = engine.Worker(_ctx.llm, sid, _emit)
        _workers[sid] = w
        w.start()


def _stop_active() -> None:
    sid = store.active_id()
    if not sid:
        return
    st = store.load(sid)
    if st and st["status"] not in ("done", "refused"):
        st["status"] = "finishing"
        store.save(st)
        _ensure_worker(sid)


def start_session(task: str, notes: str = "", demo: bool | None = None) -> dict:
    task = (task or "").strip()
    if not task:
        return {"ok": False, "error": "Say what you're working on, e.g. 'replace a Redmi Note 8 battery'."}
    demo = _demo_default() if demo is None else bool(demo)
    try:
        guide, origin = engine.build_guide(_ctx.llm, task, notes or "")
    except engine.Refused as exc:
        return {"ok": False, "refused": True, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    _stop_active()
    state = engine.new_session(task, guide, origin, demo)
    store.set_active(state["id"])
    _ensure_worker(state["id"])
    _emit("session.started", {"id": state["id"]})
    out = engine.summarize(state)
    out.update({
        "ok": True, "guide_origin": origin, "demo": demo,
        "show_card": f'::second-hands{{session="{state["id"]}"}}',
        "next": ("Tell the user to open the Second Hands pane in Hermes Desktop (right side), pick their camera, "
                 "point it down at the work and press Start. Put the show_card line on its own paragraph in "
                 "your reply so the live card appears in chat. Don't list every step; the pane shows them."),
    })
    return out


def _session_or_active(args: dict) -> dict | None:
    sid = (args or {}).get("session_id") or store.active_id()
    if not sid:
        return None
    try:
        return store.load(sid)
    except ValueError:
        return None


# ---------------------------------------------------------------- tools


def _tool_start(args, **kwargs):
    try:
        return json.dumps(start_session(args.get("task", ""), args.get("notes", ""), args.get("demo")))
    except Exception as exc:
        log.exception("second_hands_start failed")
        return json.dumps({"ok": False, "error": str(exc)})


def _tool_status(args, **kwargs):
    try:
        st = _session_or_active(args)
        if not st:
            return json.dumps({"ok": False, "error": "No Second Hands session yet. Start one with second_hands_start."})
        if st["status"] not in ("done", "refused"):
            _ensure_worker(st["id"])
        return json.dumps({"ok": True, **engine.summarize(st)})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def _tool_finish(args, **kwargs):
    try:
        st = _session_or_active(args)
        if not st:
            return json.dumps({"ok": False, "error": "No active session."})
        if st["status"] != "done":
            with open(store.session_dir(st["id"]) / "commands.jsonl", "a") as f:
                f.write(json.dumps({"type": "finish"}) + "\n")
            _ensure_worker(st["id"])
            w = _workers.get(st["id"])
            if w and w.thread:
                w.thread.join(timeout=45)
            st = store.load(st["id"]) or st
        return json.dumps({"ok": True, **engine.summarize(st)})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


START_SCHEMA = {
    "name": "second_hands_start",
    "description": (
        "Start a live, camera-supervised session for a hands-on physical task (phone or laptop repair, "
        "electronics, flat-pack furniture, cooking prep, bike fixes, crafts). Builds a step-by-step guide "
        "with the visible mistakes to watch for, then the Second Hands pane in Hermes Desktop watches the "
        "user's hands through their camera and says STOP before a costly or dangerous mistake. Use when the "
        "user wants help DOING a physical task, not just reading about it. Refuses mains electricity, gas, "
        "brakes, airbags and firearms. Optionally research the task first (web search) and pass what you "
        "found as notes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The task in the user's words, e.g. 'replace the battery in my Redmi Note 8'."},
            "notes": {"type": "string", "description": "Optional source text (a guide you found) to build the steps from."},
            "demo": {"type": "boolean", "description": "Run the scripted demo (no camera or model calls). Only when the user asks for a demo."},
        },
        "required": ["task"],
    },
}
STATUS_SCHEMA = {
    "name": "second_hands_status",
    "description": "Get the live state of the current Second Hands session: step progress, recent STOP/ask alerts, cost, and the summary once finished.",
    "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}},
}
FINISH_SCHEMA = {
    "name": "second_hands_finish",
    "description": "Finish the current Second Hands session and return its report (steps done, mistakes caught, overrides, summary).",
    "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}},
}


# ---------------------------------------------------------------- /repair


def _cmd_repair(raw_args: str) -> str:
    arg = (raw_args or "").strip()
    if arg in ("", "help"):
        return ("Usage: /repair <task>  start a camera-supervised session\n"
                "       /repair demo <task>  scripted demo, no camera\n"
                "       /repair status | /repair finish")
    if arg == "status":
        return _tool_status({})
    if arg in ("finish", "stop", "done"):
        r = json.loads(_tool_finish({}))
        return r.get("summary") or r.get("error") or "Finished."
    demo = arg.startswith("demo ")
    r = start_session(arg[5:] if demo else arg, demo=True if demo else None)
    if not r.get("ok"):
        return r.get("error", "Couldn't start.")
    return (f"Second Hands: {r['guide_title']} ({len(r['steps'])} steps{', demo' if r['demo'] else ''}).\n"
            f"Camera: {r['camera_setup']}\nOpen the Second Hands pane, pick your camera and press Start.")


def register(ctx):
    global _ctx
    _ctx = ctx
    ctx.register_auxiliary_task(engine.TASK_VISION, display_name="Second Hands: camera checks",
                                description="Looks at each camera frame against the current step. Needs a vision model; a fast one keeps alerts quick.",
                                inherit_from="vision")
    ctx.register_auxiliary_task(engine.TASK_FAST, display_name="Second Hands: alert gate",
                                description="Decides warn / ask / silent and writes the spoken line. A small fast model is ideal.")
    ctx.register_auxiliary_task(engine.TASK_PLAN, display_name="Second Hands: planner and judge",
                                description="Builds step guides, double-checks STOP alerts and writes the report.")
    ctx.register_tool(name="second_hands_start", toolset="second-hands", schema=START_SCHEMA, handler=_tool_start)
    ctx.register_tool(name="second_hands_status", toolset="second-hands", schema=STATUS_SCHEMA, handler=_tool_status)
    ctx.register_tool(name="second_hands_finish", toolset="second-hands", schema=FINISH_SCHEMA, handler=_tool_finish)
    ctx.register_command("repair", _cmd_repair, description="Start a camera-supervised hands-on task (Second Hands)",
                         args_hint="<task> | demo <task> | status | finish")
