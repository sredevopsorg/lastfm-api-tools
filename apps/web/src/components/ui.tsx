import type { ConfidenceView, FieldChangeView } from '../api/client'

/** Render any JSON-ish value the way an operator needs to read it. */
export function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === '') {
    return <span className="muted">— empty —</span>
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">— none —</span>
    return (
      <span className="chips">
        {value.map((entry, index) => (
          <span className="chip" key={`${String(entry)}-${index}`}>
            {typeof entry === 'object' && entry !== null
              ? String((entry as Record<string, unknown>).Name ?? JSON.stringify(entry))
              : String(entry)}
          </span>
        ))}
      </span>
    )
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    if (entries.length === 0) return <span className="muted">— none —</span>
    return (
      <span className="chips">
        {entries.map(([key, val]) => (
          <span className="chip" key={key}>
            {key}: {String(val)}
          </span>
        ))}
      </span>
    )
  }
  if (typeof value === 'boolean') return <span>{value ? 'yes' : 'no'}</span>
  // Long prose is worth showing in full rather than truncated to one line.
  return <span className="mono-wrap">{String(value)}</span>
}

const verdictClass = { auto: 'ok', review: 'warn', reject: 'danger' } as const

export function ConfidenceBadge({ confidence }: { confidence: ConfidenceView }) {
  return (
    <span className={`badge ${verdictClass[confidence.verdict]}`} title={confidence.notes.join('; ')}>
      {confidence.verdict} · {confidence.total.toFixed(2)}
    </span>
  )
}

export function ConfidenceDetail({ confidence }: { confidence: ConfidenceView }) {
  return (
    <div className="confidence">
      {confidence.mbid_conflict && (
        <p className="badge danger">
          MusicBrainz ids conflict — these are different entities regardless of the names
        </p>
      )}
      <ul className="plain">
        {confidence.components.map((component) => (
          <li key={component.name}>
            <span className="mono">{component.name}</span>{' '}
            {component.value === null ? (
              <span className="muted">not compared</span>
            ) : (
              <>
                <strong>{component.value.toFixed(2)}</strong>{' '}
                <span className="muted">×{component.applied_weight.toFixed(2)}</span>
              </>
            )}{' '}
            <span className="muted">{component.detail}</span>
          </li>
        ))}
      </ul>
      {confidence.notes.length > 0 && (
        <p className="muted small">{confidence.notes.join(' · ')}</p>
      )}
    </div>
  )
}

/**
 * One proposed change, with an independent toggle.
 *
 * Fields are individually selectable because the payload sent to Jellyfin is a *full
 * overwrite*: everything unticked is carried through at its current value, so ticking
 * a field is the only way it can change.
 */
export function ChangeRow({
  change,
  checked,
  onToggle,
  disabled,
}: {
  change: FieldChangeView
  checked: boolean
  onToggle: (field: string) => void
  disabled?: boolean
}) {
  const differs = change.changes_anything
  return (
    <div className={`change-row${differs ? '' : ' noop'}`}>
      <label className="change-toggle">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled || !differs}
          onChange={() => onToggle(change.field)}
        />
        <span className="mono">{change.field}</span>
      </label>
      <div className="change-values">
        <div>
          <span className="muted small">current</span>
          <Value value={change.current} />
        </div>
        <div>
          <span className="muted small">proposed</span>
          <Value value={change.proposed} />
        </div>
      </div>
      <div className="change-meta">
        <span className="badge">{change.mode}</span>{' '}
        <span className="muted small">{change.reason}</span>
        {!differs && <span className="badge">already correct</span>}
      </div>
    </div>
  )
}

export function WithheldRow({ change }: { change: FieldChangeView }) {
  return (
    <div className="change-row withheld">
      <span className="mono">{change.field}</span>
      <span className="muted small">{change.withheld_reason ?? 'not proposed'}</span>
    </div>
  )
}

export function bytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof Error ? error.message : String(error)
  return <p className="badge danger">{message}</p>
}
