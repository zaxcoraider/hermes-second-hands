# Second Hands for Hermes

**A second pair of eyes on your hands.** Point a camera at your workbench, tell Hermes what you're fixing, building or cooking, and Second Hands watches every step live. Before you make a costly or dangerous mistake, it says **STOP**: out loud, on screen, and with the frame that triggered it.

> "Replace my Redmi Note 8 battery" → an 8-step guide with the mistakes that matter → a live camera pane in Hermes Desktop → **STOP. Disconnect the battery before any other connector.**

A prompt or a skill can tell you the steps. Only a plugin can **watch you do them**.

## What it does

| | |
|---|---|
| **Builds a camera-checkable guide** | 5–10 steps, each with what "done" looks like from above and the visible mistakes worth interrupting for. Hand-checked guides for four demo tasks; any other task is planned by your model (pass it web research as notes). |
| **Watches live in Hermes Desktop** | A pane with your camera feed (any webcam, or a phone as a webcam), the current step, a step list, a log and the session cost. |
| **Only interrupts when it matters** | A mistake has to show up in **2 of the last 3 frames**, then a fast model decides *warn / ask / stay quiet*, then on high-risk steps a second model **double-checks before any STOP**. Mistake types are fixed by the guide, so the vision model can't invent new alarms. |
| **Hands-free** | Alerts are spoken aloud. A status-bar chip shows the step, or a red STOP. "Fixed it" / "Not a mistake" (never flag this again on this step), Back, Skip, Finish. |
| **Reports back to Hermes** | A live card in the chat, and a written report when you finish. The agent can read the session with `second_hands_status` and answer "what did I get wrong?" |
| **Refuses what it shouldn't supervise** | Mains electricity, gas, brakes, airbags, firearms: a missed warning there could seriously hurt someone. |
| **Demo mode** | `/repair demo <task>` runs scripted camera observations through the real alert pipeline: no camera, no model calls, no cost. |

## Install

```bash
hermes plugins install second-hands
```

Then:

1. **Enable the Desktop pane:** Hermes Desktop → **Capabilities → Plugins** → turn on **Second Hands** (Hermes keeps every plugin's Desktop half off until you approve it).
2. **Pick models (optional):** Settings → Models → Auxiliary lists three slots. With nothing set they follow your main model; the camera slot follows your **vision** slot.

   | Slot | Job | Good choice |
   |---|---|---|
   | `second_hands_vision` | Looks at each frame | A fast vision model, e.g. `nvidia/Nemotron-3.5-Super-VL` |
   | `second_hands_fast` | warn / ask / silent + the spoken line | A small fast model, e.g. `nvidia/Nemotron-3_5-Lightning` |
   | `second_hands_plan` | Builds guides, double-checks STOPs, writes the report | Your strongest model |

   **Turn thinking off on these slots.** Alerts need speed, not deliberation. For Nemotron on Nebius Token Factory (and other vLLM-served models), `reasoning_effort: "none"` isn't enough; add the model's own switch. It took the gate from 4.8 s to 0.4 s and the camera check from 5.8 s to 1.7 s:

   ```yaml
   auxiliary:
     second_hands_fast:
       provider: nebius-token-factory
       model: nvidia/Nemotron-3_5-Lightning
       extra_body:
         chat_template_kwargs:
           enable_thinking: false
   ```
3. **Camera:** any camera macOS/Windows/Linux sees. To use a phone, run it as a webcam (Continuity Camera / Desk View on iPhone, Iriun or DroidCam on Android) and mount it 15–60 cm above the work, facing down.

## Use

In any Hermes Desktop chat:

```
/repair replace my Redmi Note 8 battery
/repair demo dice an onion          # no camera needed
/repair status
/repair finish
```

Or just ask: *"Help me assemble this flat-pack bookshelf, watch me through the camera."* The agent calls `second_hands_start` (it can research the task first and pass what it found as notes), then shows the live card.

Open the **Second Hands** pane on the right, choose your camera, press **Start**, and work.

## How it works

```
 Camera (Desktop pane) ──frame every 2 s──► plugin_api.py ──► plugin-data/second-hands/
                                                                    │
 Agent tool / /repair ──starts──► worker thread (agent side) ◄──────┘
        │                          observe (vision slot)
        │                          → 2-of-3 confirm (no model)
        │                          → gate: warn / ask / silent (fast slot)
        │                          → verify before STOP (plan slot)
        ▼                                   │
 chat card ::second-hands{…}  ◄── state.json + plugin.second-hands.* events ──► pane, status chip, voice
```

All model calls go through Hermes's `ctx.llm` on the plugin's own auxiliary slots, so Hermes owns provider choice, credentials, fallbacks and the audit log.

## Disclosure

What you should know before installing (catalog rule 13):

- **Network:** the plugin makes **no network calls of its own**. Camera frames (downscaled JPEGs, about every 2 s while a session is live) and step text are sent to **whatever model provider you have configured in Hermes**, through Hermes's `ctx.llm`. The checker always takes the newest frame and skips any that arrived while it was busy, so a 10-minute session is at most about 300 vision calls on your provider's bill (usually far fewer). The pane shows the running cost.
- **Camera:** the Desktop pane opens your camera with the OS permission prompt. It stays on only while the pane is open with an active session, and frames are sent only while a session is live (after **Start**).
- **Files:** reads and writes only `<hermes home>/plugin-data/second-hands/`: session state, the newest frame, and a thumbnail of each frame that triggered an alert. Nothing outside it. Delete the folder to clear history.
- **Background work:** one thread per live session, in the Hermes process that started it. It stops when the session finishes or after 30 idle minutes.
- **No** shell commands, no credentials stored or read, no telemetry. Voice uses the OS speech engine locally.
- **Safety:** this is an assistant, not a guarantee. It can miss mistakes. It refuses tasks where a miss could seriously injure someone.

## Development

```bash
git clone https://github.com/zaxcoraider/hermes-second-hands ~/src/hermes-second-hands
ln -s ~/src/hermes-second-hands ~/.hermes/plugins/second-hands
hermes plugins enable second-hands
hermes plugins validate ~/src/hermes-second-hands
```

The Desktop half hot-reloads from a linked checkout. Python changes need a Hermes restart.

Built from [Second Hands](https://github.com/zaxcoraider/second-hands) (same author, MIT). MIT licensed.
