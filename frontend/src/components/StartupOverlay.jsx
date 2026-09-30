import { useEffect, useRef, useState } from 'react'

const STATUS_ICON = {
  queued: '⋯',
  checking: '⏳',
  cached: '✅',
  done: '✅',
  downloading: '⬇️',
  error: '❌',
}

const STATUS_TEXT = {
  queued: 'in queue',
  checking: 'checking…',
  cached: 'ok (cached)',
  done: 'ok',
  downloading: 'downloading…',
  error: 'error',
}

function ItemRow({ item }) {
  const icon = STATUS_ICON[item.status] || '⏳'
  const isBlockingError = item.status === 'error' && item.required
  const isWarning = item.status === 'error' && !item.required
  // A failed optional check isn't actually broken, just reduced -- show its
  // short warning_label ("slow processing", "align/snap disabled") instead
  // of the bare word "error", which reads as a real problem.
  const statusText = isWarning && item.warning_label ? item.warning_label : (STATUS_TEXT[item.status] || item.status)
  // Real byte-level progress (see mashup_engine.py's tqdm bridge) -- only
  // available for the 5 audio-separator models, not the 2 Demucs ones or
  // the environment checks, which just show the plain "downloading…" text.
  const showBar = item.status === 'downloading' && typeof item.progress === 'number'

  return (
    <li className={`startup-item${isBlockingError ? ' startup-item--error' : ''}${isWarning ? ' startup-item--warning' : ''}${item.status === 'queued' ? ' startup-item--queued' : ''}`}>
      <div className="startup-item-row">
        <span className={`startup-item-icon${item.status === 'checking' ? ' startup-item-icon--pulse' : ''}`}>
          {isWarning ? '⚠️' : icon}
        </span>
        <span className="startup-item-label">{item.label}</span>
        <span className="startup-item-status">
          {showBar ? `${Math.round(item.progress * 100)}%` : statusText}
        </span>
      </div>
      {showBar && (
        <div className="startup-progress-track">
          <div className="startup-progress-fill" style={{ width: `${item.progress * 100}%` }} />
        </div>
      )}
    </li>
  )
}

/**
 * Full-screen blocking overlay shown while the backend is still checking
 * dependencies and fetching models (see server.py's /api/startup-status).
 * Stays up (with a slight per-item "checking..." pause on the backend, so
 * the list doesn't just jump straight to fully resolved) until the user
 * dismisses it with the OK button -- which only appears once every
 * REQUIRED item is ready. Optional items (no GPU, rubberband missing,
 * restoration models still downloading, etc.) are shown live too, but
 * don't hold up that button.
 */
export default function StartupOverlay() {
  const [state, setState] = useState(null)
  const [dismissed, setDismissed] = useState(false)
  const pollRef = useRef(null)

  useEffect(() => {
    const poll = () => {
      fetch('/api/startup-status')
        .then((res) => res.json())
        .then((data) => {
          setState(data)
          if (data.ready) {
            clearInterval(pollRef.current)
          }
        })
        .catch(() => {
          // Backend not reachable yet (e.g. still binding the port right
          // after launch) -- just try again on the next tick.
        })
    }
    poll()
    pollRef.current = setInterval(poll, 700)
    return () => clearInterval(pollRef.current)
  }, [])

  if (dismissed || !state) return null

  const items = state.items || []
  const envItems = items.filter((i) => i.key.startsWith('env_'))
  const modelItems = items.filter((i) => !i.key.startsWith('env_'))
  const blockingErrors = items.filter((i) => i.status === 'error' && i.required)

  return (
    <div className="startup-overlay">
      <div className="startup-card">
        <h2>🎵 DualSync Pro is getting ready…</h2>
        <p className="startup-subtitle">
          {blockingErrors.length > 0
            ? 'A required dependency is missing -- see below.'
            : 'Checking dependencies and models (first run may take a few minutes).'}
        </p>

        {envItems.length > 0 && (
          <>
            <h3>Environment</h3>
            <ul className="startup-list">
              {envItems.map((item) => <ItemRow key={item.key} item={item} />)}
            </ul>
          </>
        )}

        {modelItems.length > 0 && (
          <>
            <h3>Models</h3>
            <ul className="startup-list">
              {modelItems.map((item) => <ItemRow key={item.key} item={item} />)}
            </ul>
          </>
        )}

        {blockingErrors.length > 0 && (
          <p className="startup-error-note">
            Fix the issue{blockingErrors.length > 1 ? 's' : ''} above and restart the server.
          </p>
        )}

        {state.ready && (
          <button className="startup-ok-button" onClick={() => setDismissed(true)}>
            OK
          </button>
        )}
      </div>
    </div>
  )
}
