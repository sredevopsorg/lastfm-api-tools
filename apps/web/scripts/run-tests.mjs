/**
 * Run the frontend unit tests, and fail if there are none.
 *
 * `node --test <glob>` exits 0 when the glob matches nothing -- it reports "fail 0" and
 * passes. So a renamed or moved test file would leave `pnpm build` green while checking
 * nothing at all, which is the failure mode this whole session keeps finding in other
 * forms: a check that looks like coverage and is not.
 *
 * Finding the files here makes "no tests" a failure rather than a success.
 */

import { execFileSync } from 'node:child_process'
import { globSync } from 'node:fs'

const patterns = process.argv.slice(2)
if (patterns.length === 0) patterns.push('src/**/*.test.ts')

const files = patterns.flatMap((pattern) => globSync(pattern)).sort()

if (files.length === 0) {
  console.error(
    `run-tests: no test files matched ${patterns.join(', ')}.\n` +
      'That is treated as a failure: a green build with zero tests is worse than a red one.',
  )
  process.exit(1)
}

console.log(`run-tests: ${files.length} file(s): ${files.join(', ')}`)
execFileSync(
  process.execPath,
  ['--test', '--experimental-strip-types', ...files],
  { stdio: 'inherit' },
)
