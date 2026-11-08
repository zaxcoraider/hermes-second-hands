"""Second Hands pipeline: guide -> (observe -> confirm -> gate -> verify) per frame -> report.

All model calls go through Hermes's ctx.llm on this plugin's own auxiliary
tasks, so they run on whatever the user configured (Settings -> Models ->
Auxiliary) and Hermes owns every credential.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import prompts, store

log = logging.getLogger(__name__)

PLUGIN = "second-hands"
TASK_VISION = "second_hands_vision"  # observe one frame (vision model)
TASK_FAST = "second_hands_fast"      # warn / ask / silent + spoken line
TASK_PLAN = "second_hands_plan"      # guide, STOP verification, report

CONFIRM_WINDOW = 3      # look at the last N frames ...
CONFIRM_NEEDED = 2      # ... and need K of them to agree
MIN_CONFIDENCE = 0.6
REPEAT_COOLDOWN_MS = 25_000
IDLE_STOP_MS = 30 * 60_000
MAX_EVENTS = 60

BLOCKED_TASKS = re.compile(
    r"\b(mains|breakers?|fuse ?box|consumer unit|live wires?|rewir\w*|(?:110|120|220|230|240) ?v(?:olts?)?|"
    r"high[- ]voltage|gas (?:line|pipe|valve|leak)s?|boilers?|chainsaws?|firearms?|handguns?|rifles?|"
    r"shotguns?|ammo|explosives?|airbags?|brake lines?)\b",
    re.I,
)
REFUSAL = "This task needs a qualified professional, so Second Hands won't supervise it."

_CURATED = json.loads((Path(__file__).parent / "guides.json").read_text())
_MATCH = [
    (re.compile(r"redmi\s*note\s*8", re.I), "REDMI_NOTE_8_BATTERY"),
    (re.compile(r"\bled\b|breadboard", re.I), "LED_BREADBOARD"),
    (re.compile(r"onion|\bdice\b|\bchop", re.I), "DICE_ONION"),
    (re.compile(r"bookshelf|bookcase|flat.?pack|ikea", re.I), "FLATPACK_BOOKSHELF"),
]

# ---------------------------------------------------------------- JSON schemas

_MISTAKE = {
    "type": "object",
    "properties": {"type": {"type": "string"}, "description": {"type": "string"}, "looksLike": {"type": "string"}},
    "required": ["type", "description", "looksLike"],
}
GUIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "refuse": {"type": "string"},
        "title": {"type": "string"},
        "device": {"type": "string"},
        "category": {"type": "string"},
        "setup": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "instruction": {"type": "string"},
                    "visualCheck": {"type": "string"},
                    "commonMistakes": {"type": "array", "items": _MISTAKE},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "safety": {"type": "boolean"},
                },
                "required": ["id", "title", "instruction", "visualCheck", "severity"],
            },
        },
    },
}
OBSERVE_SCHEMA = {
    "type": "object",
    "properties": {
        "stepState": {"type": "string", "enum": ["not_started", "in_progress", "done", "unclear"]},
        "observation": {"type": "string"},
        "mistake": {
            "type": ["object", "null"],
            "properties": {"type": {"type": "string"}, "confidence": {"type": "number"}, "evidence": {"type": "string"}},
        },
        "needCloserLook": {"type": "boolean"},
        "visibility": {"type": "string"},
        "focus": {
            "type": ["object", "null"],
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "w": {"type": "number"}, "h": {"type": "number"}},
        },
    },
    "required": ["stepState", "observation"],
}
GATE_SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["warn", "ask", "silent"]}, "line": {"type": "string"}},
    "required": ["action", "line"],
}
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"confirmed": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["confirmed", "reason"],
}

# ---------------------------------------------------------------- model calls


# $ per 1M tokens (in, out), used when the provider returns token counts but no price.
_PRICES = {
    "nemotron-3.5-super-vl": (0.3, 0.9),
    "nemotron-3_5-lightning": (0.06, 0.24),
    "nemotron-3-ultra": (1.0, 3.0),
}


def _cost(model: str, usage) -> float:
    if usage is None:
        return 0.0
    if getattr(usage, "cost_usd", None):
        return float(usage.cost_usd)
    m = model.lower()
    price = next((p for k, p in _PRICES.items() if k in m), None)
    if not price:
        return 0.0
    return (getattr(usage, "input_tokens", 0) * price[0] + getattr(usage, "output_tokens", 0) * price[1]) / 1e6


_LAST_ERROR: list[str] = [""]


def _friendly(exc: Exception, task: str) -> str:
    msg = str(exc).strip().splitlines()[0][:240] if str(exc).strip() else type(exc).__name__
    if "No LLM provider" in msg or "unavailable" in msg:
        kind = "a vision model" if task == TASK_VISION else "a model"
        return (f"No model is set up for {task}. Run `hermes model` (or Settings → Models → Auxiliary) "
                f"and pick {kind} for it. Or try `/repair demo <task>`, which needs no model.")
    return msg


def _loose_json(text: str) -> Any:
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _call(llm, state: dict | None, *, stage: str, task: str, system: str, text: str,
          image: bytes | None = None, schema: dict | None = None, max_tokens: int = 400,
          timeout: float = 20.0) -> Any:
    """One structured call. Returns parsed JSON (or text when schema is None), None on failure."""
    parts: list[dict] = [{"type": "text", "text": text}]
    if image:
        parts.append({"type": "image", "data": image, "mime_type": "image/jpeg"})
    t0 = time.monotonic()
    ok, model, cost = False, "", 0.0
    try:
        if schema is None:
            r = llm.complete(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
                max_tokens=max_tokens, timeout=timeout, task=task, purpose=f"second-hands.{stage}",
            )
            out: Any = (r.text or "").strip() or None
        else:
            r = llm.complete_structured(
                instructions="Follow the system prompt. Reply with the JSON object only.",
                system_prompt=system, input=parts, json_schema=schema, schema_name=f"second_hands_{stage}",
                max_tokens=max_tokens, timeout=timeout, temperature=0.2, task=task,
                purpose=f"second-hands.{stage}",
            )
            out = r.parsed if r.content_type == "json" and isinstance(r.parsed, dict) else _loose_json(r.text)
        model = getattr(r, "model", "") or ""
        cost = _cost(model, getattr(r, "usage", None))
        ok = out is not None
        if ok and state is not None and (state.get("last_error") or {}).get("stage") == stage:
            state["last_error"] = None
        return out
    except Exception as exc:  # never let a model failure kill the worker
        log.warning("second-hands %s call failed: %s", stage, exc)
        _LAST_ERROR[0] = _friendly(exc, task)
        if state is not None:
            state["last_error"] = {"stage": stage, "message": _LAST_ERROR[0], "at": store.now_ms()}
        return None
    finally:
        if state is not None:
            state.setdefault("trace", []).insert(0, {
                "stage": stage, "model": model, "ms": int((time.monotonic() - t0) * 1000),
                "cost_usd": cost, "ok": ok, "at": store.now_ms(),
            })
            del state["trace"][40:]
            state["cost_usd"] = round(state.get("cost_usd", 0) + cost, 6)


# ---------------------------------------------------------------- guide


class Refused(Exception):
    pass


def curated_guide(task: str) -> dict | None:
    for rx, key in _MATCH:
        if rx.search(task):
            return json.loads(json.dumps(_CURATED[key]))
    return None


def _clean_guide(g: dict) -> dict | None:
    steps = []
    for i, s in enumerate(g.get("steps") or []):
        if not isinstance(s, dict) or not s.get("title"):
            continue
        sev = s.get("severity") if s.get("severity") in ("low", "medium", "high") else "medium"
        steps.append({
            "id": f"s{i + 1}",
            "title": str(s["title"])[:80],
            "instruction": str(s.get("instruction", ""))[:400],
            "visualCheck": str(s.get("visualCheck", ""))[:300],
            "commonMistakes": [
                {k: str(m.get(k, ""))[:300] for k in ("type", "description", "looksLike")}
                for m in (s.get("commonMistakes") or [])[:2] if isinstance(m, dict) and m.get("type")
            ],
            "severity": sev,
            "safety": bool(s.get("safety", False)),
        })
    if not steps:
        return None
    return {
        "title": str(g.get("title") or "Hands-on task")[:120],
        "device": str(g.get("device") or ""),
        "category": str(g.get("category") or ""),
        "setup": str(g.get("setup") or "Put the camera above your work, facing down, in good light."),
        "sources": g.get("sources") or [],
        "steps": steps[:12],
    }


def build_guide(llm, task: str, notes: str = "", prefer_curated: bool = True) -> tuple[dict, str]:
    """Returns (guide, origin) where origin is "curated" or "planned". Raises Refused."""
    if BLOCKED_TASKS.search(task):
        raise Refused(REFUSAL)
    if prefer_curated and not notes.strip():
        g = curated_guide(task)
        if g:
            return g, "curated"
    planned = _call(llm, None, stage="guide", task=TASK_PLAN, system=prompts.GUIDE_SYSTEM,
                    text=prompts.guide_user(task, notes), schema=GUIDE_SCHEMA, max_tokens=3000, timeout=90)
    if isinstance(planned, dict) and planned.get("refuse"):
        raise Refused(str(planned["refuse"]) or REFUSAL)
    g = _clean_guide(planned) if isinstance(planned, dict) else None
    if g:
        return g, "planned"
    g = curated_guide(task)
    if g:
        return g, "curated"
    why = _LAST_ERROR[0] or "The planner model returned no usable steps. Try describing the task in more detail."
    raise RuntimeError(f"Couldn't build a guide: {why}")


# ---------------------------------------------------------------- session


def new_session(task: str, guide: dict, origin: str, demo: bool) -> dict:
    sid = store.new_id()
    steps = guide["steps"]
    state = {
        "id": sid, "task": task, "guide": guide, "origin": origin, "demo": demo,
        "status": "ready", "created_at": store.now_ms(), "started_at": None, "finished_at": None,
        "current_step": 0,
        "step_status": {s["id"]: ("active" if i == 0 else "todo") for i, s in enumerate(steps)},
        "events": [], "observations": [], "overrides": [], "trace": [],
        "frames": 0, "last_frame_seq": 0, "last_observation": None, "cost_usd": 0.0,
        "alert": None, "summary": None, "stats": {"stops": 0, "asks": 0, "warns": 0, "overrides": 0},
    }
    store.save(state)
    return state


def _event(state: dict, kind: str, message: str, **extra) -> dict:
    step = _step(state)
    e = {"id": store.new_id(), "at": store.now_ms(), "kind": kind, "message": message,
         "step_id": step["id"] if step else None, **{k: v for k, v in extra.items() if v is not None}}
    state["events"].insert(0, e)
    del state["events"][MAX_EVENTS:]
    if kind in ("stop", "ask", "warn"):
        state["stats"][kind + "s"] += 1
        state["alert"] = e
    return e


def _step(state: dict) -> dict | None:
    steps = state["guide"]["steps"]
    i = state["current_step"]
    return steps[i] if 0 <= i < len(steps) else None


def _advance(state: dict, by: int = 1, manual: bool = False) -> None:
    steps = state["guide"]["steps"]
    cur = _step(state)
    if cur:
        state["step_status"][cur["id"]] = "skipped" if (manual and by > 0) else ("todo" if by < 0 else "done")
    state["current_step"] = max(0, min(len(steps), state["current_step"] + by))
    nxt = _step(state)
    if nxt:
        state["step_status"][nxt["id"]] = "active"
    state["alert"] = None


def _recent(state: dict, step_id: str) -> list[dict]:
    return [o for o in state["observations"] if o["step_id"] == step_id][:CONFIRM_WINDOW]


def should_advance(recent: list[dict]) -> bool:
    return sum(1 for o in recent if o["stepState"] == "done") >= CONFIRM_NEEDED


def find_candidate(state: dict, step: dict, recent: list[dict]) -> dict | None:
    latest = recent[0].get("mistake") if recent else None
    if not latest or latest.get("confidence", 0) < MIN_CONFIDENCE:
        return None
    agreeing = [o for o in recent if (o.get("mistake") or {}).get("type") == latest["type"]
                and o["mistake"].get("confidence", 0) >= MIN_CONFIDENCE]
    if len(agreeing) < CONFIRM_NEEDED:
        return None
    if any(ov["step_id"] == step["id"] and ov["mistake_type"] == latest["type"] for ov in state["overrides"]):
        return None
    now = store.now_ms()
    if any(e.get("step_id") == step["id"] and e.get("mistake_type") == latest["type"]
           and e["kind"] in ("ask", "warn", "stop") and now - e["at"] < REPEAT_COOLDOWN_MS for e in state["events"]):
        return None
    return {
        "type": latest["type"],
        "confidence": sum(o["mistake"]["confidence"] for o in agreeing) / len(agreeing),
        "evidence": latest.get("evidence", ""),
        "need_closer": any(o.get("needCloserLook") for o in agreeing),
    }


def _scripted_gate(step: dict, cand: dict, mistake: dict | None) -> dict:
    """Deterministic fallback when the fast model fails: still protects the user."""
    if cand["need_closer"] or cand["confidence"] < 0.8:
        return {"action": "ask", "line": "Hold on. Is that right? Hold it closer."}
    what = (mistake or {}).get("description", "That looks wrong.").split(".")[0]
    return {"action": "warn", "line": f"Stop. {what}."}


def _focus(f) -> dict | None:
    """Box (fractions of the frame, top-left origin) around the hands and the part, or None."""
    try:
        x, y, w, h = (max(0.0, min(1.0, float(f[k]))) for k in ("x", "y", "w", "h"))
    except (TypeError, KeyError, ValueError):
        return None
    if w < 0.03 or h < 0.03:
        return None
    return {"x": x, "y": y, "w": min(w, 1 - x), "h": min(h, 1 - y)}


def _clamp(line: str, n: int = 15) -> str:
    w = line.strip().split()
    return line.strip() if len(w) <= n else " ".join(w[:n]).rstrip(",;:") + "."


# ---------------------------------------------------------------- worker


class Worker:
    """Owns one live session: drains pane commands, runs frames through the pipeline."""

    def __init__(self, llm, sid: str, emit: Callable[[str, dict], None]):
        self.llm, self.sid, self.emit = llm, sid, emit
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.demo_tick = 0
        self.demo_counts: dict[str, int] = {}

    def start(self) -> None:
        run = contextvars.copy_context().run  # keep Hermes's profile scope in the thread
        self.thread = threading.Thread(target=run, args=(self._loop,), name=f"second-hands-{self.sid}", daemon=True)
        self.thread.start()

    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _loop(self) -> None:
        last_activity = store.now_ms()
        state: dict | None = None
        while not self.stop_flag.is_set():
            try:
                store.heartbeat(self.sid)
                state = store.load(self.sid)
                if not state or state["status"] in ("done", "refused"):
                    return
                changed = self._commands(state)
                if state["status"] == "finishing":
                    self._finish(state)
                    return
                seq = store.frame_seq(self.sid)
                if state["demo"] and state["status"] == "live":
                    changed |= self._demo_step(state)
                    last_activity = store.now_ms()
                elif seq > state["last_frame_seq"]:
                    state["last_frame_seq"] = seq
                    frame = store.read_frame(self.sid)
                    if frame:
                        self._frame(state, frame)
                        changed = True
                        last_activity = store.now_ms()
                if changed:
                    store.save(state)
                    self.emit("state", {"id": self.sid, "status": state["status"], "step": state["current_step"]})
                if store.now_ms() - last_activity > IDLE_STOP_MS:
                    return
            except Exception:
                log.exception("second-hands worker error")
                time.sleep(2)
            time.sleep(1.6 if state and state.get("demo") else 0.25)

    # --- pane buttons ---------------------------------------------------------

    def _commands(self, state: dict) -> bool:
        cmds = store.drain_commands(self.sid)
        for c in cmds:
            t = c.get("type")
            if t == "begin" and state["status"] == "ready":
                state["status"], state["started_at"] = "live", store.now_ms()
                _event(state, "info", f"Started. Step 1: {state['guide']['steps'][0]['title']}.")
            elif t == "override" and state["alert"]:
                a = state["alert"]
                state["overrides"].append({"step_id": a.get("step_id"), "mistake_type": a.get("mistake_type"),
                                           "at": store.now_ms()})
                state["stats"]["overrides"] += 1
                _event(state, "override", "Got it. I won't flag that again on this step.", speak=False,
                       mistake_type=a.get("mistake_type"))
                state["alert"] = None
            elif t == "dismiss":
                state["alert"] = None
            elif t in ("skip", "back"):
                left = _step(state)
                _advance(state, 1 if t == "skip" else -1, manual=True)
                nxt = _step(state)
                verb = "Skipped" if t == "skip" else "Went back from"
                what = f"{verb}: {left['title'].lower()}. " if left else ""
                _event(state, "skip" if t == "skip" else "info",
                       what + (f"Now step {state['current_step'] + 1}: {nxt['title'].lower()}." if nxt
                               else "No steps left. Finish when you're ready."))
            elif t == "finish":
                state["status"] = "finishing"
        return bool(cmds)

    # --- one frame --------------------------------------------------------------

    def _frame(self, state: dict, image: bytes) -> None:
        step = _step(state)
        if state["status"] == "ready":
            state["status"], state["started_at"] = "live", store.now_ms()
        state["frames"] += 1
        if not step:
            return
        steps = state["guide"]["steps"]
        i = state["current_step"]
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        obs = _call(self.llm, state, stage="observe", task=TASK_VISION, system=prompts.OBSERVE_SYSTEM,
                    text=prompts.observe_user(step, nxt, i, len(steps)), image=image,
                    schema=OBSERVE_SCHEMA, max_tokens=300, timeout=25)
        if not isinstance(obs, dict) or obs.get("stepState") not in ("not_started", "in_progress", "done", "unclear"):
            return
        known = {m["type"] for m in step.get("commonMistakes", [])}
        m = obs.get("mistake") if isinstance(obs.get("mistake"), dict) else None
        if m and (m.get("type") not in known):
            m = None  # the model can't invent new alarms
        o = {"step_id": step["id"], "at": store.now_ms(), "stepState": obs["stepState"],
             "observation": str(obs.get("observation", ""))[:200], "mistake": m,
             "needCloserLook": bool(obs.get("needCloserLook")), "visibility": obs.get("visibility", "good"),
             "focus": _focus(obs.get("focus"))}
        self._observe(state, o, image)

    def _observe(self, state: dict, o: dict, image: bytes | None) -> None:
        step = _step(state)
        state["observations"].insert(0, o)
        del state["observations"][30:]
        state["last_observation"] = o
        recent = _recent(state, step["id"])
        if should_advance(recent):
            _advance(state)
            nxt = _step(state)
            _event(state, "step_done", f"Step {state['current_step']} done. Next: {nxt['title'].lower()}." if nxt
                   else "All steps done. Press Finish when you're ready.")
            return
        cand = find_candidate(state, step, recent)
        if not cand:
            return
        mistake = next((mm for mm in step.get("commonMistakes", []) if mm["type"] == cand["type"]), None)
        said = [e["message"] for e in state["events"] if e["kind"] in ("ask", "warn", "stop")][:3]
        decision = None if state["demo"] else _call(
            self.llm, state, stage="gate", task=TASK_FAST, system=prompts.GATE_SYSTEM,
            text=prompts.gate_user(step, cand["type"], (mistake or {}).get("description", ""), cand["confidence"],
                                   cand["evidence"], cand["need_closer"], said),
            schema=GATE_SCHEMA, max_tokens=120, timeout=8)
        if isinstance(decision, dict) and decision.get("action") in ("warn", "ask", "silent"):
            decision["line"] = _clamp(str(decision.get("line", "")))
        else:
            decision = _scripted_gate(step, cand, mistake)
        if decision["action"] == "silent":
            return
        kind, line, reason, verified = decision["action"], str(decision.get("line", "")), cand["evidence"], None
        if kind == "warn" and step["severity"] == "high":
            obs_lines = [
                f"{x['observation']}" + (f" [suspected {x['mistake']['type']} @ {x['mistake']['confidence']:.2f}: "
                                         f"{x['mistake'].get('evidence', '')}]" if x.get("mistake") else "")
                for x in recent
            ]
            verdict = {"confirmed": True, "reason": cand["evidence"]} if state["demo"] else _call(
                self.llm, state, stage="verify", task=TASK_PLAN, system=prompts.VERIFY_SYSTEM,
                text=prompts.verify_user(step, cand["type"], (mistake or {}).get("description", ""), obs_lines),
                schema=VERIFY_SCHEMA, max_tokens=600, timeout=15)
            if isinstance(verdict, dict) and verdict.get("confirmed"):
                kind, verified, reason = "stop", True, str(verdict.get("reason") or reason)
            else:
                kind, line = "ask", "Hold on. Is that right? Hold it closer."
                reason = str((verdict or {}).get("reason") or reason) if isinstance(verdict, dict) else reason
        e = _event(state, kind, line, mistake_type=cand["type"], reason=reason, verified=verified,
                   focus=recent[0].get("focus") if recent else None,
                   manual=f"{step['title']}: {step['instruction']}" +
                          (f"\nCommon mistake: {mistake['description']}" if mistake else ""))
        if image:
            store.put_thumb(state["id"], e["id"], image)
            e["thumb"] = True
        self.emit("alert", {"id": state["id"], "kind": kind, "message": line})

    # --- demo mode (no camera, no model calls) ------------------------------------

    def _demo_step(self, state: dict) -> bool:
        """Scripted observations: each step progresses and completes; the first high-severity
        step shows its first listed mistake twice, which runs the real confirm/gate/STOP path."""
        step = _step(state)
        if not step:
            return False
        if state["alert"]:
            return False  # wait for the user to answer the alert
        self.demo_tick += 1
        risky = step["severity"] == "high" and step.get("commonMistakes") and not any(
            ov["step_id"] == step["id"] for ov in state["overrides"]) and not any(
            e.get("step_id") == step["id"] and e["kind"] in ("stop", "ask", "warn") for e in state["events"])
        if risky:
            m = step["commonMistakes"][0]
            o = {"step_id": step["id"], "at": store.now_ms(), "stepState": "in_progress",
                 "observation": f"Demo: {m['looksLike']}",
                 "mistake": {"type": m["type"], "confidence": 0.9, "evidence": m["looksLike"]},
                 "needCloserLook": False, "visibility": "good"}
        else:
            n = self.demo_counts.get(step["id"], 0)
            self.demo_counts[step["id"]] = n + 1
            o = {"step_id": step["id"], "at": store.now_ms(), "stepState": "in_progress" if n == 0 else "done",
                 "observation": f"Demo: working on '{step['title']}'.", "mistake": None,
                 "needCloserLook": False, "visibility": "good"}
        state["frames"] += 1
        self._observe(state, o, None)
        return True

    # --- end of session ---------------------------------------------------------------

    def _finish(self, state: dict) -> None:
        timeline = []
        t0 = state.get("started_at") or state["created_at"]
        for e in reversed(state["events"]):
            timeline.append(f"+{(e['at'] - t0) // 1000}s {e['kind']}: {e['message']}"
                            + (f" (reason: {e['reason']})" if e.get("reason") else ""))
        done = sum(1 for v in state["step_status"].values() if v == "done")
        steps_status = [f"{s['title']}: {state['step_status'].get(s['id'], 'todo')}" for s in state["guide"]["steps"]]
        summary = None
        if not state["demo"]:
            summary = _call(self.llm, state, stage="report", task=TASK_PLAN, system=prompts.REPORT_SYSTEM,
                            text=prompts.report_user(state["task"], timeline, steps_status), schema=None,
                            max_tokens=400, timeout=40)
        if not summary:
            s = state["stats"]
            how = "in the scripted demo" if state["demo"] else "by camera"
            summary = (f"{done} of {len(state['guide']['steps'])} steps confirmed {how}. "
                       f"{s['stops']} STOP, {s['warns']} warnings and {s['asks']} questions raised, "
                       f"{s['overrides']} overridden.")
        state["summary"] = summary
        state["status"], state["finished_at"], state["alert"] = "done", store.now_ms(), None
        _event(state, "info", "Session finished.", speak=False)
        store.save(state)
        if store.active_id() == state["id"]:
            store.set_active(None)
        self.emit("finished", {"id": state["id"]})


def summarize(state: dict) -> dict:
    """Compact view for the agent (tool results)."""
    steps = state["guide"]["steps"]
    cur = _step(state)
    return {
        "session_id": state["id"], "task": state["task"], "status": state["status"],
        "guide_title": state["guide"]["title"], "camera_setup": state["guide"].get("setup"),
        "sources": state["guide"].get("sources") or [],
        "step": f"{min(state['current_step'] + 1, len(steps))}/{len(steps)}",
        "current_step": cur["title"] if cur else None,
        "steps": [{"title": s["title"], "status": state["step_status"].get(s["id"])} for s in steps],
        "frames_checked": state["frames"], "stats": state["stats"], "cost_usd": state.get("cost_usd", 0),
        "recent_events": [{"kind": e["kind"], "message": e["message"], "reason": e.get("reason")}
                          for e in state["events"][:8]],
        "summary": state.get("summary"),
    }
