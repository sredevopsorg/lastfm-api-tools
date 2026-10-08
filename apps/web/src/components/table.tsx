import type { ReactNode } from 'react'
import { ariaSortFor, nextSort, pageCount, rangeLabel, windowFor, type SortState } from './paging'

/**
 * Table furniture the browse screens share.
 *
 * These live in one place because three screens need the same behaviour and the details
 * that make it usable are not the kind anyone re-derives correctly per screen: `aria-sort`
 * has to name the current direction, a disabled "next" has to be genuinely disabled rather
 * than styled to look it, and the row range has to come from what the server *returned*
 * rather than what was asked for, or the last page claims a row that does not exist.
 */

export function Pagination({
  startIndex,
  pageSize,
  returned,
  total,
  onStartIndex,
  unit = 'items',
}: {
  startIndex: number
  pageSize: number
  returned: number
  total: number
  onStartIndex: (startIndex: number) => void
  unit?: string
}) {
  const window = windowFor({ startIndex, returned })
  const pages = pageCount(total, pageSize)
  const currentPage = Math.floor(window.start / pageSize) + 1
  const canGoBack = window.start > 0
  const canGoForward = window.end < total

  return (
    <nav className="pager" aria-label={`${unit} pagination`}>
      <button
        type="button"
        className="pill"
        onClick={() => onStartIndex(Math.max(0, window.start - pageSize))}
        disabled={!canGoBack}
      >
        ← Previous
      </button>
      <button
        type="button"
        className="pill"
        onClick={() => onStartIndex(window.start + returned)}
        disabled={!canGoForward}
      >
        Next →
      </button>
      {/* aria-live so the range is announced when paging, which is the only feedback
          that the table changed when the visible rows look similar. */}
      <span className="muted small" aria-live="polite">
        {rangeLabel(window, total)} · page {currentPage} of {pages}
      </span>
    </nav>
  )
}

export function SortHeader({
  column,
  label,
  current,
  onSort,
  width,
}: {
  column: string
  label: ReactNode
  current: SortState
  onSort: (next: SortState) => void
  width?: string
}) {
  const ariaSort = ariaSortFor(current, column)
  const active = ariaSort !== 'none'
  return (
    <th scope="col" aria-sort={ariaSort} style={width ? { width } : undefined}>
      <button
        type="button"
        className={active ? 'sort-header active' : 'sort-header'}
        onClick={() => onSort(nextSort(current, column))}
        // The arrow is decorative; `aria-sort` is what a screen reader announces, and a
        // second announcement of the same fact is noise.
        title={`Sort by ${typeof label === 'string' ? label : column}`}
      >
        {label}
        <span aria-hidden="true" className="sort-arrow">
          {ariaSort === 'ascending' ? '▲' : ariaSort === 'descending' ? '▼' : ''}
        </span>
      </button>
    </th>
  )
}

/**
 * The filters currently applied, as removable chips.
 *
 * Chips rather than only the controls: a filter set three screens ago is otherwise
 * invisible, and the symptom -- a list that is quietly too short -- looks like missing
 * data rather than an active filter.
 */
export function FilterChips({
  filters,
  onRemove,
  onClear,
}: {
  filters: { key: string; label: string }[]
  onRemove: (key: string) => void
  onClear: () => void
}) {
  if (filters.length === 0) return null
  return (
    <div className="chips filter-chips" aria-label="Active filters">
      {filters.map((filter) => (
        <span className="chip filter-chip" key={filter.key}>
          {filter.label}
          <button
            type="button"
            className="chip-remove"
            onClick={() => onRemove(filter.key)}
            aria-label={`Remove filter ${filter.label}`}
          >
            ×
          </button>
        </span>
      ))}
      {filters.length > 1 && (
        <button type="button" className="link small" onClick={onClear}>
          clear all
        </button>
      )}
    </div>
  )
}

/**
 * What a scan-backed filter actually covered.
 *
 * A filtered count drawn from a partial read is a lower bound, and printing it as a total
 * is the defect the scan reporting exists to remove. This makes the scope visible instead
 * of leaving an operator to assume a small number means a small problem.
 */
export function ScanNote({
  scanned,
  matched,
  truncated,
  limit,
}: {
  scanned: number
  matched: number
  truncated: boolean
  limit: number
}) {
  return (
    <p className="muted small scan-note">
      {matched.toLocaleString()} matched, from {scanned.toLocaleString()} checked
      {truncated ? (
        <>
          {' '}
          — <strong>the scan stopped at {limit.toLocaleString()}</strong>, so the count is a
          lower bound and the filter is still narrowing it.
        </>
      ) : (
        ' — every item was checked.'
      )}
    </p>
  )
}
