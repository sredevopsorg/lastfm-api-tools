/**
 * Paging and sorting arithmetic, kept out of the components.
 *
 * Every mistake this module prevents is one that is invisible in a rendered page: an
 * off-by-one window shows a row twice or not at all, and a "page 3 of 2" is only ever
 * noticed by a person who happens to look. So it lives in plain functions that can be
 * checked directly, and the components stay thin enough to have nothing to get wrong.
 */

/** Which rows a page covers, in the API's `start_index`/`page_size` terms. */
export interface Window {
  start: number
  end: number
  // 1-based, for display. The API is 0-based for items and 1-based for archive pages,
  // which is a difference worth containing here rather than at every call site.
  firstRow: number
  lastRow: number
}

export function windowFor({
  startIndex,
  returned,
}: {
  startIndex: number
  returned: number
}): Window {
  // Nothing came back, so there is no window to describe -- and the *clamped* start must
  // not be reported as if a row were there. Reporting `start: 9, firstRow: 0` for an empty
  // page was the first version of this function, and the two fields disagreed about
  // whether the window existed at all.
  if (returned === 0) return { start: 0, end: 0, firstRow: 0, lastRow: 0 }

  // `returned`, not `pageSize`, for the end: the last page is partial, and computing the
  // range from the requested size shows a last row that does not exist.
  const start = Math.max(0, startIndex)
  return {
    start,
    end: start + returned,
    firstRow: start + 1,
    lastRow: start + returned,
  }
}

export function pageCount(total: number, pageSize: number): number {
  if (pageSize <= 0) return 1
  return Math.max(1, Math.ceil(total / pageSize))
}

/**
 * A short label for a window, e.g. "1–50 of 5,442" or "0 of 0".
 *
 * `toLocaleString` on the total only: a thousands separator makes 5,442 readable at a
 * glance, and row numbers never reach four digits.
 */
export function rangeLabel(window: Window, total: number): string {
  // Keyed on the window being empty, not on the total: a stale page can ask for rows past
  // the end of a non-empty result, and "0–0 of 10" is not a range anyone should read.
  if (window.firstRow === 0) return `0 of ${total.toLocaleString()}`
  return `${window.firstRow}–${window.lastRow} of ${total.toLocaleString()}`
}

/** The next sort state after clicking a header, given the current one. */
export interface SortState {
  sort: string
  order: 'asc' | 'desc'
}

/**
 * Clicking a sorted column reverses it; clicking a different column starts ascending.
 *
 * Starting a *new* column at ascending rather than inheriting the previous direction is
 * deliberate: carrying "desc" across a column change means the first click on any header
 * gives the least useful end of the list, which reads as the sort being wrong.
 */
export function nextSort(current: SortState, clicked: string): SortState {
  if (current.sort !== clicked) return { sort: clicked, order: 'asc' }
  return { sort: clicked, order: current.order === 'asc' ? 'desc' : 'asc' }
}

/** The value `aria-sort` must carry for a header. */
export function ariaSortFor(current: SortState, column: string): 'ascending' | 'descending' | 'none' {
  if (current.sort !== column) return 'none'
  return current.order === 'asc' ? 'ascending' : 'descending'
}
