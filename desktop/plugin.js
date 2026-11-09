// Second Hands for Hermes Desktop: live camera pane, status chip and chat card.
// Disk plugin: plain ESM, no JSX. Talks only to its own backend (dashboard/plugin_api.py).
import { atom, COMPOSER_AREAS, host, STATUSBAR_AREAS, useValue } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'second-hands'
const FRAME_MS = 2000
const POLL_MS = 1500

const $session = atom(null) // latest state.json view, or null
const $error = atom(null)
const $voice = atom(true)

let api = null // ctx.rest, set in register()
let lastSpoken = null
let lastStopNotified = null

// ------------------------------------------------------------------ data

async function refresh() {
  if (!api) return
  try {
    const cur = $session.get()
    const res = cur && cur.status !== 'done' ? await api(`/state/${cur.id}`) : await api('/active')
    let s = res && res.session
    if (!s && cur && cur.status === 'done') s = cur // keep the finished report on screen
    if (!s && cur && cur.status !== 'done') {
      const again = await api('/active')
      s = again && again.session
    }
    $session.set(s || null)
    $error.set(null)
    if (s) announce(s)
  } catch (e) {
    $error.set(String((e && e.message) || e))
  }
}

async function command(type) {
  const s = $session.get()
  if (!s || !api) return
  try {
    await api(`/command/${s.id}`, { method: 'POST', body: { type } })
  } catch (e) {
    host.notify({ kind: 'error', message: `Second Hands: ${String((e && e.message) || e)}` })
  }
  if (ctxRef) ctxRef.setTimeout(refresh, 300)
}

function speak(text) {
  if (!$voice.get() || !text || typeof window === 'undefined' || !window.speechSynthesis) return
  try {
    window.speechSynthesis.cancel()
    const u = new SpeechSynthesisUtterance(text)
    u.rate = 1.05
    window.speechSynthesis.speak(u)
  } catch {
    // speech is a nicety; never break the pane over it
  }
}

function chime() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext
    if (!AC || !$voice.get()) return
    const ac = new AC()
    ;[880, 660].forEach((f, i) => {
      const o = ac.createOscillator()
      const g = ac.createGain()
      o.type = 'square'
      o.frequency.value = f
      g.gain.setValueAtTime(0.08, ac.currentTime + i * 0.16)
      g.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + i * 0.16 + 0.15)
      o.connect(g).connect(ac.destination)
      o.start(ac.currentTime + i * 0.16)
      o.stop(ac.currentTime + i * 0.16 + 0.16)
    })
    setTimeout(() => ac.close(), 600)
  } catch {
    // audio is a nicety
  }
}

function announce(s) {
  const e = (s.events || [])[0]
  if (!e) return
  if (lastSpoken === null) {
    lastSpoken = e.id // don't read out history when the pane first opens
    return
  }
  if (e.id === lastSpoken) return
  lastSpoken = e.id
  if (e.kind === 'stop') chime()
  if (e.speak !== false) speak(e.message)
  if (e.kind === 'stop' && lastStopNotified !== e.id) {
    lastStopNotified = e.id
    host.notify({ kind: 'warning', message: `STOP: ${e.message}` })
    try {
      ctxRef && ctxRef.os.notify({ title: 'Second Hands: STOP', body: e.message })
    } catch {
      // OS notifications can be off; the in-app toast already fired
    }
  }
}

let ctxRef = null

// ------------------------------------------------------------------ small UI bits

// Desktop shows a plugin slash command's reply as a plain system line, so the
// live chat card (an agent-reply directive) can't appear there. In Desktop,
// `/repair <task>` and `/repair demo <task>` become a normal message: the agent
// calls second_hands_start and replies with the card. status / finish / help
// stay instant slash commands. CLI and other surfaces keep the plain /repair.
// The demo sentence must match DEMO_REQUEST_PREFIX in __init__.py (it forces demo mode).
const REPAIR_PASSTHROUGH = new Set(['', 'help', 'demo', 'status', 'finish', 'stop', 'done'])

function routeRepair(draft) {
  const m = /^\/repair(?:\s+([\s\S]*))?$/.exec((draft && draft.text ? draft.text : '').trim())
  if (!m) return draft
  const arg = (m[1] || '').trim()
  if (REPAIR_PASSTHROUGH.has(arg.toLowerCase())) return draft
  const demo = /^demo\s+/i.test(arg)
  const task = demo ? arg.replace(/^demo\s+/i, '').trim() : arg
  if (!task) return draft
  const text = demo
    ? `Start a Second Hands demo session (scripted, no camera) for: ${task}`
    : `Start a Second Hands session and watch me through the camera while I: ${task}`
  return { ...draft, text }
}

const cls = (...xs) => xs.filter(Boolean).join(' ')

function Btn({ onClick, children, tone, disabled, title }) {
  const tones = {
    danger: 'bg-(--ui-red) text-white hover:opacity-90',
    primary: 'bg-primary text-primary-foreground hover:opacity-90',
    default: 'bg-(--ui-bg-tertiary) text-(--ui-text-primary) hover:bg-(--ui-control-hover-background)'
  }
  return jsx('button', {
    type: 'button',
    title,
    disabled,
    onClick,
    className: cls(
      'rounded-md px-2.5 py-1 text-[0.75rem] font-medium transition-opacity disabled:opacity-40',
      tones[tone || 'default']
    ),
    children
  })
}

// The Second Hands mark: red status ring + clock second hand. Inline SVG, no network.
// The hand uses currentColor so it follows the light/dark theme.
function Logo({ size = 16 }) {
  return jsxs('svg', {
    viewBox: '82 82 348 348', width: size, height: size, 'aria-hidden': true, className: 'shrink-0',
    children: [
      jsx('path', { d: 'M271.7 106.8 A150 150 0 1 1 240.3 106.8', fill: 'none', stroke: '#FF5A52', strokeWidth: 44 }),
      jsx('line', { x1: 256, y1: 256, x2: 256, y2: 132, stroke: 'currentColor', strokeWidth: 30, strokeLinecap: 'round', transform: 'rotate(48 256 256)' }),
      jsx('circle', { cx: 256, cy: 256, r: 28, fill: 'currentColor' })
    ]
  })
}

function Label({ children }) {
  return jsx('div', {
    className: 'text-[0.625rem] font-semibold tracking-[0.12em] uppercase text-(--ui-text-tertiary)',
    children
  })
}

function money(n) {
  return `$${(n || 0).toFixed(n >= 0.1 ? 2 : 4)}`
}

// ------------------------------------------------------------------ camera

function useCameras() {
  const [list, setList] = useState([])
  useEffect(() => {
    let live = true
    const load = () =>
      navigator.mediaDevices
        .enumerateDevices()
        .then(ds => live && setList(ds.filter(d => d.kind === 'videoinput')))
        .catch(() => {})
    load()
    navigator.mediaDevices.addEventListener('devicechange', load)
    return () => {
      live = false
      navigator.mediaDevices.removeEventListener('devicechange', load)
    }
  }, [])
  return [list, setList]
}

function Camera({ session }) {
  const video = useRef(null)
  const canvas = useRef(null)
  const busy = useRef(false)
  const [cams, setCams] = useCameras()
  const [deviceId, setDeviceId] = useState(() => {
    try {
      return localStorage.getItem('hermes.plugin.second-hands.camera') || ''
    } catch {
      return ''
    }
  })
  const [camError, setCamError] = useState(null)
  const [sent, setSent] = useState(0)
  const [aspect, setAspect] = useState(4 / 3)
  const watching = session && session.status === 'live' && !session.demo
  const stopNow = !!(session && session.alert && session.alert.kind === 'stop')
  const lastObs = session && session.last_observation
  const focus = watching && lastObs && lastObs.focus && Date.now() - lastObs.at < 8000 ? lastObs.focus : null

  // Open the chosen camera.
  useEffect(() => {
    let stream = null
    let cancelled = false
    setCamError(null)
    navigator.mediaDevices
      .getUserMedia({ video: deviceId ? { deviceId: { exact: deviceId }, width: 1280 } : { width: 1280 }, audio: false })
      .then(s => {
        if (cancelled) return s.getTracks().forEach(t => t.stop())
        stream = s
        if (video.current) video.current.srcObject = s
        navigator.mediaDevices.enumerateDevices().then(ds => setCams(ds.filter(d => d.kind === 'videoinput')))
      })
      .catch(e => setCamError(e && e.name === 'NotAllowedError' ? 'Camera permission denied. Allow Hermes in System Settings → Privacy → Camera.' : `Camera unavailable: ${(e && e.message) || e}`))
    return () => {
      cancelled = true
      if (stream) stream.getTracks().forEach(t => t.stop())
    }
  }, [deviceId])

  // While live, send a frame every FRAME_MS (never two at once).
  useEffect(() => {
    if (!watching) return
    const tick = async () => {
      const v = video.current
      const c = canvas.current
      if (busy.current || !v || !c || !v.videoWidth || !api) return
      busy.current = true
      try {
        const w = 768
        const h = Math.round((v.videoHeight / v.videoWidth) * w)
        c.width = w
        c.height = h
        c.getContext('2d').drawImage(v, 0, 0, w, h)
        const image = c.toDataURL('image/jpeg', 0.72)
        await api(`/frame/${session.id}`, { method: 'POST', body: { image }, timeoutMs: 8000 })
        setSent(n => n + 1)
      } catch {
        // a dropped frame is fine; the next tick retries
      } finally {
        busy.current = false
      }
    }
    tick()
    const t = setInterval(tick, FRAME_MS)
    return () => clearInterval(t)
  }, [watching, session && session.id])

  const pick = e => {
    const id = e.target.value
    setDeviceId(id)
    try {
      localStorage.setItem('hermes.plugin.second-hands.camera', id)
    } catch {
      // storage can be unavailable
    }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsxs('div', {
        className: cls(
          'relative overflow-hidden rounded-lg border-2 bg-(--ui-bg-quaternary)',
          stopNow ? 'animate-pulse border-(--ui-red)' : 'border-(--ui-stroke-secondary)'
        ),
        style: { aspectRatio: String(aspect) },
        children: [
          jsx('video', {
            ref: video,
            autoPlay: true,
            muted: true,
            playsInline: true,
            onLoadedMetadata: e => e.target.videoWidth && setAspect(e.target.videoWidth / e.target.videoHeight),
            className: 'h-full w-full object-cover'
          }),
          focus &&
            jsx('div', {
              className: cls('pointer-events-none absolute rounded-sm border-2 transition-all duration-500', stopNow ? 'border-(--ui-red)' : 'border-(--ui-accent)'),
              style: { left: `${focus.x * 100}%`, top: `${focus.y * 100}%`, width: `${focus.w * 100}%`, height: `${focus.h * 100}%` }
            }),
          camError &&
            jsx('div', {
              className: 'absolute inset-0 flex items-center justify-center p-4 text-center text-[0.75rem] text-(--ui-text-secondary)',
              children: camError
            }),
          watching &&
            jsxs('div', {
              className: 'absolute left-2 top-2 flex items-center gap-1.5 rounded bg-(--ui-bg-elevated)/80 px-1.5 py-0.5 text-[0.625rem] font-semibold tracking-wider text-(--ui-red)',
              children: [jsx('span', { className: 'inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-(--ui-red)' }), `WATCHING · ${sent}`]
            })
        ]
      }),
      jsx('canvas', { ref: canvas, className: 'hidden' }),
      jsxs('select', {
        value: deviceId,
        onChange: pick,
        className: 'rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-input) px-2 py-1 text-[0.75rem] text-(--ui-text-primary)',
        children: [
          jsx('option', { value: '', children: 'Default camera' }),
          ...cams.map((c, i) => jsx('option', { value: c.deviceId, children: c.label || `Camera ${i + 1}` }, c.deviceId || i))
        ]
      })
    ]
  })
}

// ------------------------------------------------------------------ alert plate

function Alert({ session }) {
  const a = session.alert
  const [thumb, setThumb] = useState(null)
  useEffect(() => {
    setThumb(null)
    if (!a || !a.thumb || !api) return
    api(`/thumb/${session.id}/${a.id}`).then(r => setThumb(r && r.image)).catch(() => {})
  }, [a && a.id])
  if (!a) return null
  const stop = a.kind === 'stop'
  return jsxs('div', {
    className: cls(
      'flex flex-col gap-2 rounded-lg border-2 p-3',
      stop ? 'border-(--ui-red) bg-(--ui-diff-remove-background)' : 'border-(--ui-yellow) bg-(--ui-bg-elevated)'
    ),
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('div', {
            className: cls('text-[1.5rem] font-black leading-none tracking-[0.15em]', stop ? 'text-(--ui-red)' : 'text-(--ui-yellow)'),
            children: stop ? 'STOP' : a.kind === 'warn' ? 'CAREFUL' : 'CHECK'
          }),
          a.verified && jsx('div', { className: 'text-[0.625rem] uppercase tracking-wider text-(--ui-text-tertiary)', children: 'double-checked' })
        ]
      }),
      jsx('div', { className: 'text-[0.9375rem] font-semibold text-(--ui-text-primary)', children: a.message }),
      a.reason && jsx('div', { className: 'text-[0.75rem] text-(--ui-text-secondary)', children: `Why: ${a.reason}` }),
      thumb && jsx('img', { src: thumb, alt: 'Frame that triggered the alert', className: 'max-h-32 w-full rounded object-cover' }),
      jsxs('div', {
        className: 'flex gap-2',
        children: [
          jsx(Btn, { tone: stop ? 'danger' : 'primary', onClick: () => command('dismiss'), children: 'Fixed it' }),
          jsx(Btn, { onClick: () => command('override'), title: "Don't flag this again on this step", children: 'Not a mistake' })
        ]
      })
    ]
  })
}

// ------------------------------------------------------------------ pane

function Steps({ session }) {
  const steps = session.guide.steps
  return jsx('ol', {
    className: 'flex flex-col gap-0.5',
    children: steps.map((s, i) => {
      const st = session.step_status[s.id]
      const active = i === session.current_step && session.status !== 'done'
      return jsxs(
        'li',
        {
          className: cls(
            'flex items-start gap-2 rounded px-1.5 py-1 text-[0.75rem]',
            active && 'bg-(--ui-row-active-background)'
          ),
          children: [
            jsx('span', {
              className: cls(
                'mt-px w-4 shrink-0 text-center font-mono',
                st === 'done' ? 'text-(--ui-green)' : st === 'skipped' ? 'text-(--ui-text-quaternary)' : active ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)'
              ),
              children: st === 'done' ? '✓' : st === 'skipped' ? '–' : String(i + 1)
            }),
            jsxs('span', {
              className: cls(st === 'done' || st === 'skipped' ? 'text-(--ui-text-tertiary)' : 'text-(--ui-text-primary)'),
              children: [s.title, s.severity === 'high' && jsx('span', { className: 'ml-1.5 text-[0.625rem] font-semibold text-(--ui-red)', children: 'HIGH RISK' })]
            })
          ]
        },
        s.id
      )
    })
  })
}

function Events({ session }) {
  const evs = (session.events || []).slice(0, 6)
  if (!evs.length) return null
  const color = k => (k === 'stop' ? 'text-(--ui-red)' : k === 'ask' || k === 'warn' ? 'text-(--ui-yellow)' : k === 'step_done' ? 'text-(--ui-green)' : k === 'skip' ? 'text-(--ui-text-quaternary)' : 'text-(--ui-text-tertiary)')
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsx(Label, { children: 'Log' }),
      ...evs.map(e =>
        jsxs(
          'div',
          {
            className: 'flex gap-2 text-[0.6875rem]',
            children: [
              jsx('span', { className: cls('w-12 shrink-0 font-mono uppercase', color(e.kind)), children: e.kind.replace('_', ' ') }),
              jsx('span', { className: 'text-(--ui-text-secondary)', children: e.message })
            ]
          },
          e.id
        )
      )
    ]
  })
}

function Report({ session }) {
  const s = session.stats || {}
  const done = Object.values(session.step_status).filter(v => v === 'done').length
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) p-3',
    children: [
      jsx(Label, { children: 'Report' }),
      jsx('div', { className: 'text-[0.875rem] font-semibold', children: session.guide.title }),
      jsxs('div', {
        className: 'grid grid-cols-4 gap-1 text-center',
        children: [
          ['steps', `${done}/${session.guide.steps.length}`],
          ['stops', s.stops || 0],
          ['checks', (s.asks || 0) + (s.warns || 0)],
          session.demo ? ['mode', 'demo'] : ['cost', money(session.cost_usd)]
        ].map(([k, v]) =>
          jsxs('div', {
            className: 'rounded bg-(--ui-bg-tertiary) py-1',
            children: [jsx('div', { className: 'text-[0.875rem] font-semibold', children: v }), jsx('div', { className: 'text-[0.625rem] uppercase text-(--ui-text-tertiary)', children: k })]
          }, k)
        )
      }),
      session.summary && jsx('div', { className: 'text-[0.75rem] leading-relaxed text-(--ui-text-secondary)', children: session.summary })
    ]
  })
}

function Empty() {
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-lg border border-dashed border-(--ui-stroke-secondary) p-3 text-[0.75rem] text-(--ui-text-secondary)',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-[0.875rem] font-semibold text-(--ui-text-primary)',
        children: [jsx(Logo, { size: 28 }), 'An extra pair of eyes on your hands']
      }),
      jsx('div', { children: 'Ask Hermes for help with a hands-on task, or type:' }),
      jsx('code', { className: 'rounded bg-(--ui-inline-code-background) px-1.5 py-1 font-mono text-[0.6875rem]', children: '/repair replace my Redmi Note 8 battery' }),
      jsx('code', { className: 'rounded bg-(--ui-inline-code-background) px-1.5 py-1 font-mono text-[0.6875rem]', children: '/repair demo dice an onion' }),
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'Point a camera down at your work. A phone works too, as a webcam (Continuity Camera, Iriun, DroidCam).' })
    ]
  })
}

function Pane() {
  const session = useValue($session)
  const error = useValue($error)
  const voice = useValue($voice)
  const step = session && session.guide.steps[session.current_step]
  const live = session && session.status === 'live'

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-y-auto p-3 text-(--ui-text-primary)',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-1.5 text-[0.75rem] font-black tracking-[0.2em]',
            children: [jsx(Logo, { size: 16 }), 'SECOND HANDS']
          }),
          jsxs('div', {
            className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
            children: [
              session && !session.demo && jsx('span', { children: money(session.cost_usd) }),
              jsx('button', {
                type: 'button',
                onClick: () => $voice.set(!voice),
                title: voice ? 'Voice on' : 'Voice off',
                className: 'rounded px-1 hover:bg-(--ui-control-hover-background)',
                children: voice ? '🔊' : '🔇'
              })
            ]
          })
        ]
      }),
      error && jsx('div', { className: 'text-[0.6875rem] text-(--ui-red)', children: `Backend: ${error}. Is the plugin enabled for this profile?` }),
      !session && jsx(Empty, {}),
      session && jsxs('div', {
        className: 'flex flex-col gap-0.5',
        children: [
          jsx(Label, { children: session.demo ? 'Demo · ' + session.task : session.task }),
          jsx('div', { className: 'text-[0.9375rem] font-semibold', children: session.guide.title }),
          (session.guide.sources || []).length > 0 &&
            jsx('div', {
              className: 'truncate text-[0.6875rem] text-(--ui-text-tertiary)',
              title: session.guide.sources.map(x => x.url).join('\n'),
              children: 'Based on: ' + session.guide.sources.map(x => x.title.split(/ [-|–] /)[0]).join(' · ')
            })
        ]
      }),
      session && session.status !== 'done' && !session.demo && jsx(Camera, { session }),
      session && session.status !== 'done' && session.demo && jsx('div', {
        className: 'rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) p-3 text-center text-[0.75rem] text-(--ui-text-secondary)',
        children: 'Demo mode: scripted camera observations, no model calls.'
      }),
      session && session.last_error && session.status !== 'done' && jsx('div', {
        className: 'rounded-md border border-(--ui-yellow) p-2 text-[0.6875rem] text-(--ui-text-secondary)',
        children: `Model problem (${session.last_error.stage}): ${session.last_error.message}`
      }),
      session && session.status !== 'done' && !session.worker_alive && jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-yellow)',
        children: 'The checker is paused. Ask Hermes "check my Second Hands session" to resume it.'
      }),
      session && session.status === 'ready' && jsxs('div', {
        className: 'flex flex-col gap-2',
        children: [
          jsx('div', { className: 'text-[0.75rem] text-(--ui-text-secondary)', children: session.guide.setup }),
          jsx(Btn, { tone: 'primary', onClick: () => command('begin'), children: 'Start' })
        ]
      }),
      session && live && jsx(Alert, { session }),
      session && live && step && jsxs('div', {
        className: 'flex flex-col gap-1 rounded-lg bg-(--ui-bg-elevated) p-3',
        children: [
          jsx(Label, { children: `Step ${session.current_step + 1} of ${session.guide.steps.length}` }),
          jsx('div', { className: 'text-[0.9375rem] font-semibold', children: step.title }),
          jsx('div', { className: 'text-[0.75rem] text-(--ui-text-secondary)', children: step.instruction }),
          session.last_observation && jsx('div', { className: 'mt-1 text-[0.6875rem] italic text-(--ui-text-tertiary)', children: `Seeing: ${session.last_observation.observation}` })
        ]
      }),
      session && live && jsxs('div', {
        className: 'flex gap-2',
        children: [
          jsx(Btn, { onClick: () => command('back'), disabled: session.current_step === 0, children: 'Back' }),
          jsx(Btn, { onClick: () => command('skip'), children: 'Skip step' }),
          jsx('div', { className: 'flex-1' }),
          jsx(Btn, { onClick: () => command('finish'), children: 'Finish' })
        ]
      }),
      session && session.status === 'finishing' && jsx('div', { className: 'text-[0.75rem] text-(--ui-text-secondary)', children: 'Writing the report…' }),
      session && session.status === 'done' && jsx(Report, { session }),
      session && jsx(Steps, { session }),
      session && jsx(Events, { session })
    ]
  })
}

// ------------------------------------------------------------------ status chip + chat card

function Chip() {
  const s = useValue($session)
  if (!s || s.status === 'done') return null
  const stop = s.alert && s.alert.kind === 'stop'
  return jsx('span', {
    className: cls('px-1.5 text-[0.6875rem] font-semibold', stop ? 'text-(--ui-red)' : 'text-(--ui-text-tertiary)'),
    children: stop ? '● STOP' : `✋ ${Math.min(s.current_step + 1, s.guide.steps.length)}/${s.guide.steps.length}`
  })
}

function Card({ attrs }) {
  const s = useValue($session)
  if (!attrs || !attrs.session || !/^[a-z0-9]+$/i.test(attrs.session)) return null
  const mine = s && s.id === attrs.session
  if (!mine) return jsx('div', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children: 'Second Hands session' })
  const done = Object.values(s.step_status).filter(v => v === 'done').length
  const pct = Math.round((done / s.guide.steps.length) * 100)
  return jsxs('div', {
    className: 'my-1 flex max-w-md flex-col gap-1.5 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) p-3',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [jsxs('div', { className: 'flex items-center gap-1.5', children: [jsx(Logo, { size: 14 }), jsx(Label, { children: 'Second Hands' })] }), jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: s.status })]
      }),
      jsx('div', { className: 'text-[0.875rem] font-semibold', children: s.guide.title }),
      jsx('div', {
        className: 'h-1 overflow-hidden rounded bg-(--ui-bg-tertiary)',
        children: jsx('div', { className: 'h-full bg-(--ui-accent)', style: { width: `${pct}%` } })
      }),
      jsx('div', {
        className: 'text-[0.75rem] text-(--ui-text-secondary)',
        children: s.status === 'done' ? s.summary : s.alert ? `${s.alert.kind.toUpperCase()}: ${s.alert.message}` : `${done}/${s.guide.steps.length} steps · open the Second Hands pane to watch`
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Second Hands',
  register(ctx) {
    api = ctx.rest
    ctxRef = ctx
    lastSpoken = null
    ctx.registerMany([
      {
        id: 'pane',
        area: 'panes',
        title: 'second hands',
        data: { placement: 'right', width: '380px' },
        render: () => jsx(Pane, {})
      },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 125, render: () => jsx(Chip, {}) },
      {
        id: 'card',
        area: 'transcript.directives',
        data: { name: 'second-hands', render: props => jsx(Card, props) }
      },
      { id: 'repair-route', area: COMPOSER_AREAS.middleware, data: { handler: routeRepair } }
    ])
    for (const ev of ['state', 'alert', 'finished', 'session.started']) ctx.onEvent(`plugin.${ID}.${ev}`, () => refresh())
    ctx.setInterval(refresh, POLL_MS)
    refresh()
  }
}
