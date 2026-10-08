import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type BlacklistConflictView,
  type BlacklistPreviewResponse,
  type BlacklistUpdateResponse,
  type GenreBlacklistResponse,
} from '../api/client'
import { ErrorNote } from '../components/ui'

/**
 * The genre blacklist: genres this tool must never propose for writing.
 *
 * Two things about this screen are deliberate and both come from the same discovery --
 * that a comma is a legal character in a genre value as well as the separator operators
 * naturally type.
 *
 * 1. The unambiguous format is one entry per line, and the hint says so.
 * 2. A line containing a comma is *not* split. It is shown back with both readings, so
 *    the operator decides. Splitting `"Rock, Reggae"` into two entries would blacklist two
 *    genres they did not name, and nothing on screen would say so.
 *
 * The preview is what makes the second point usable: before saving, the operator sees the
 * exact library genres each entry matches.
 */
export function Settings() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState('')
  const [saved, setSaved] = useState<BlacklistUpdateResponse | null>(null)
  const [loadedOnce, setLoadedOnce] = useState(false)

  const state = useQuery({
    queryKey: ['genre-blacklist'],
    queryFn: () => api.get<GenreBlacklistResponse>('/api/settings/genre-blacklist'),
  })

  // Seed the textarea from the server once, and only once. Re-seeding on every refetch
  // would discard what the operator is typing whenever the query invalidated.
  useEffect(() => {
    if (state.data && !loadedOnce) {
      setDraft(state.data.entries.join('\n'))
      setLoadedOnce(true)
    }
  }, [state.data, loadedOnce])

  const preview = useMutation({
    mutationFn: (raw: string) =>
      api.post<BlacklistPreviewResponse>('/api/settings/genre-blacklist/preview', { raw }),
  })

  const save = useMutation({
    mutationFn: (payload: { raw: string; allow_commas: boolean }) =>
      api.put<BlacklistUpdateResponse>('/api/settings/genre-blacklist', payload),
    onSuccess: (result) => {
      setSaved(result)
      setDraft(result.state.entries.join('\n'))
      void queryClient.invalidateQueries({ queryKey: ['genre-blacklist'] })
    },
  })

  const conflictRaws = useMemo(
    () => new Set((preview.data?.conflicts ?? []).map((conflict) => conflict.raw)),
    [preview.data],
  )

  const lines = draft.split('\n').map((line) => line.trim()).filter(Boolean)

  return (
    <section>
      <h2>Settings</h2>
      <p className="muted small">
        Genres blacklisted here are never proposed for writing, on any screen. Matching is
        exact and case-insensitive.
      </p>

      {state.isLoading && <p className="muted">Loading…</p>}
      <ErrorNote error={state.error} />

      {state.data && (
        <>
          <div className="panel">
            <h3>Blacklisted genres</h3>
            <p className="muted small">
              One genre per line. A line containing a comma is reported rather than split —
              a comma is also legal inside a genre value.
            </p>

            <textarea
              className="blacklist-input"
              rows={8}
              value={draft}
              spellCheck={false}
              onChange={(event) => {
                setDraft(event.target.value)
                setSaved(null)
              }}
              aria-label="Blacklisted genres, one per line"
            />

            <div className="row">
              <button
                className="pill"
                onClick={() => preview.mutate(draft)}
                disabled={preview.isPending}
              >
                {preview.isPending ? 'Checking…' : 'Preview matches'}
              </button>
              <button
                className="primary"
                onClick={() => save.mutate({ raw: draft, allow_commas: false })}
                disabled={save.isPending}
              >
                {save.isPending ? 'Saving…' : 'Save'}
              </button>
              {/* Offered only alongside the explanation, never as the default: on a
                  library whose genres contain commas this changes *which* values are
                  blacklisted, so it is a decision rather than a convenience. */}
              <button
                className="pill"
                onClick={() => save.mutate({ raw: draft, allow_commas: true })}
                disabled={save.isPending}
                title="Split each line on commas. Use this only if your genres never contain commas."
              >
                Save, splitting on commas
              </button>
              <span className="muted small">{lines.length} line(s)</span>
            </div>

            <ErrorNote error={save.error} />

            {saved?.needs_review && (
              <ConflictList conflicts={saved.conflicts} savedCount={saved.count} />
            )}
            {saved && !saved.needs_review && (
              <p className="badge ok">Saved {saved.count} entr{saved.count === 1 ? 'y' : 'ies'}.</p>
            )}
          </div>

          {preview.data && (
            <div className="panel">
              <h3>What this would match</h3>
              <p className="muted small">{preview.data.note}</p>
              <table className="grid">
                <thead>
                  <tr>
                    <th>Entry</th>
                    <th>Matches in this library</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.data.preview.map((entry) => (
                    <tr key={entry.norm}>
                      <td className="mono">{entry.entry}</td>
                      <td>
                        {entry.matches.length === 0 ? (
                          <span className="muted">nothing right now</span>
                        ) : (
                          <span className="chips">
                            {entry.matches.map((match) => (
                              <span className="chip" key={match}>
                                {match}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {conflictRaws.size > 0 && (
                <ConflictList
                  conflicts={preview.data.conflicts}
                  savedCount={preview.data.would_blacklist.length}
                  wouldSave
                />
              )}
            </div>
          )}

          <div className="panel">
            <h3>
              In effect{' '}
              <span className="muted small">
                {state.data.counts.effective} values · {state.data.counts.stored} yours ·{' '}
                {state.data.counts.env} from configuration · {state.data.counts.defaults} built in
              </span>
            </h3>
            <p className="muted small">{state.data.note}</p>
            <details>
              <summary>Everything currently enforced</summary>
              <span className="chips">
                {state.data.effective.map((value) => (
                  <span className="chip" key={value}>
                    {value}
                  </span>
                ))}
              </span>
            </details>
          </div>
        </>
      )}
    </section>
  )
}

/**
 * A line that could be read two ways, with both readings named.
 *
 * Presented as a choice rather than an error: the unambiguous entries on the same save
 * *were* saved, so this is "one line needs an answer", not "nothing happened".
 */
function ConflictList({
  conflicts,
  savedCount,
  wouldSave = false,
}: {
  conflicts: BlacklistConflictView[]
  savedCount: number
  wouldSave?: boolean
}) {
  return (
    <div className="conflict-box">
      <p className="badge warn">
        {savedCount} entr{savedCount === 1 ? 'y' : 'ies'}{' '}
        {wouldSave ? 'would be blacklisted' : 'saved'}, but {conflicts.length} line
        {conflicts.length === 1 ? '' : 's'} need{savedCount === 1 ? 's' : ''} an answer:
      </p>
      <ul className="plain">
        {conflicts.map((conflict) => (
          <li key={conflict.raw}>
            <span className="mono">{conflict.raw}</span>
            <br />
            <span className="muted small">
              As one genre it matches <strong>{conflict.whole}</strong>; split it would match{' '}
              <strong>{conflict.fragments.join(', ')}</strong>. Put each genre on its own line
              to choose.
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
