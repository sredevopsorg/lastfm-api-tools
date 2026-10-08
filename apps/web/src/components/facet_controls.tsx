import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api, type ItemSummaryPage, type SelectionKind } from '../api/client'
import { ErrorNote } from './ui'
import {
  addValue,
  cleanPatterns,
  entityLabel,
  facetsFor,
  removeValue,
} from './facet_rules'

/**
 * The three narrowings a selection accepts, as controls.
 *
 * Shared by the library browse, the batch form and the genre-removal form, because they
 * send the same three fields to the same selection logic -- and a screen that offered a
 * filter the others did not would be the start of three vocabularies for one idea.
 *
 * State lives with the caller rather than in here: the browse keeps its query in the URL
 * (so a link means the same thing twice) while the batch forms keep it in component state,
 * and a component that owned it would have to pick one.
 */

export interface FacetValues {
  artistIds: string[]
  albumIds: string[]
  patterns: string[]
}

export const EMPTY_FACETS: FacetValues = { artistIds: [], albumIds: [], patterns: [] }

/** How many matches a picker shows before it stops and says so. */
const MATCH_LIMIT = 20

/**
 * What ``POST /api/items/states`` gives back for the chip labels.
 *
 * Not ``ItemSummary``: that is the browse row (``id``, ``genres``, ...) while this is the
 * item's writable state, which names the item as ``item_id``. Two shapes for two jobs, and
 * typing one as the other is how a chip ends up showing `undefined`.
 */
interface NamedItem {
  item_id: string
  name: string
}

/**
 * A searchable picker for one kind of library entity, holding ids.
 *
 * Ids rather than names, deliberately: the live library has three artist names that map to
 * two catalog entities each, so a name-only picker would leave one twin unfilterable and
 * the choice between them a coin flip. The names are resolved separately -- in one batched
 * call -- so a chip reads as a name without the id ever being lost.
 */
function ItemIdPicker({
  kind,
  label,
  placeholder,
  ids,
  onChange,
}: {
  kind: SelectionKind
  label: string
  placeholder: string
  ids: string[]
  onChange: (next: string[]) => void
}) {
  const [term, setTerm] = useState('')

  const matches = useQuery({
    queryKey: ['facet-matches', kind, term],
    queryFn: () =>
      api.items({
        kind,
        page_size: MATCH_LIMIT,
        ...(term.trim() ? { search: term.trim() } : {}),
      }),
    // Nothing to search for yet, so nothing is asked for. An unfiltered browse here would
    // show the first twenty library items as if they were matches for an empty query.
    enabled: term.trim().length > 0,
  })

  // One call for every selected id, so a chip can show a name after a reload. The ids come
  // from the URL, which carries no names.
  const selected = useQuery({
    queryKey: ['facet-names', ids.join(',')],
    queryFn: () => api.post<NamedItem[]>('/api/items/states', ids),
    enabled: ids.length > 0,
  })

  const names = new Map((selected.data ?? []).map((item) => [item.item_id, item.name]))
  const chips = ids.map((id) => ({ id, name: names.get(id) ?? id.slice(-6) }))
  const page: ItemSummaryPage | undefined = matches.data

  return (
    <div className="facet-picker">
      <label className="check" htmlFor={`${kind}-${label}-match`}>
        {label}
        <input
          id={`${kind}-${label}-match`}
          value={term}
          onChange={(event) => setTerm(event.target.value)}
          placeholder={placeholder}
          aria-label={`Find ${label} to filter by`}
        />
      </label>

      {matches.isError && <ErrorNote error={matches.error} />}

      {page && (
        <div className="facet-results" aria-label={`${label} matches`}>
          {page.items.length === 0 ? (
            <p className="muted small">No {label} matches “{term}”.</p>
          ) : (
            <>
              <ul className="plain">
                {page.items.map((item) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      className="link small"
                      // The id is appended only when the name repeats, so the common case
                      // stays readable and the ambiguous one becomes distinguishable.
                      onClick={() => onChange(addValue(ids, item.id))}
                      disabled={ids.includes(item.id)}
                    >
                      {entityLabel({ id: item.id, name: item.name }, page.items)}
                    </button>
                  </li>
                ))}
              </ul>
              {page.total > page.items.length && (
                // Said rather than hidden: "no matches" and "the first twenty of three
                // hundred" look identical in a list, and only one of them means the item
                // is not there.
                <p className="muted small">
                  showing the first {page.items.length} of {page.total.toLocaleString()} matches
                </p>
              )}
            </>
          )}
        </div>
      )}

      {chips.length > 0 && (
        <div className="chips">
          {chips.map((chip) => (
            <span className="chip filter-chip" key={chip.id}>
              {entityLabel(chip, chips)}
              <button
                type="button"
                className="chip-remove"
                aria-label={`Remove ${label} ${chip.name}`}
                onClick={() => onChange(removeValue(ids, chip.id))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * The exclusion patterns, as a list of chips.
 *
 * The hint text is part of the control rather than documentation elsewhere: `live` and
 * `*live*` mean different things, and the difference is the whole reason this is a glob and
 * not a substring match.
 */
function ExclusionPatterns({
  id,
  patterns,
  onChange,
}: {
  id: string
  patterns: string[]
  onChange: (next: string[]) => void
}) {
  const [draft, setDraft] = useState('')

  function add() {
    const next = addValue(patterns, draft, { fold: true })
    onChange(next)
    setDraft('')
  }

  return (
    <div className="facet-picker">
      <label className="check" htmlFor={id}>
        exclude
        <input
          id={id}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              add()
            }
          }}
          placeholder="*Live*"
          aria-label="Exclusion pattern"
        />
      </label>
      <button type="button" className="pill" onClick={add} disabled={!draft.trim()}>
        Add pattern
      </button>

      {patterns.length > 0 && (
        <div className="chips">
          {patterns.map((pattern) => (
            <span className="chip pattern-chip" key={pattern}>
              {pattern}
              <button
                type="button"
                className="chip-remove"
                aria-label={`Remove pattern ${pattern}`}
                onClick={() => onChange(removeValue(patterns, pattern))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <p className="muted small">
        Matched against the item's name, album and album artist. Exact unless you use a
        wildcard: <code>live</code> hides only an item called <code>live</code>, while{' '}
        <code>*live*</code> hides anything containing it. Needs a library scan, and the
        result says how much of the library it read.
      </p>
    </div>
  )
}

export function SelectionFilters({
  kind,
  values,
  onChange,
  idPrefix,
}: {
  kind: SelectionKind
  values: FacetValues
  onChange: (next: FacetValues) => void
  idPrefix: string
}) {
  const available = facetsFor(kind)
  return (
    <div className="facet-row">
      {available.artistIds && (
        <ItemIdPicker
          kind="artist"
          label="artists"
          placeholder="Radiohead"
          ids={values.artistIds}
          onChange={(artistIds) => onChange({ ...values, artistIds })}
        />
      )}
      {available.albumIds && (
        <ItemIdPicker
          kind="album"
          label="albums"
          placeholder="OK Computer"
          ids={values.albumIds}
          onChange={(albumIds) => onChange({ ...values, albumIds })}
        />
      )}
      <ExclusionPatterns
        id={`${idPrefix}-exclude`}
        patterns={values.patterns}
        onChange={(patterns) => onChange({ ...values, patterns: cleanPatterns(patterns) })}
      />
    </div>
  )
}
