import { runAll as runChecks, resolveRepo } from '../../../shared/batches/BATCH-F2P-0003/checks.mjs'
import * as F2P064 from './F2P-064.mjs'
import * as F2P068 from './F2P-068.mjs'
import * as F2P070 from './F2P-070.mjs'
import * as F2P072 from './F2P-072.mjs'
import * as F2P049 from './F2P-049.mjs'
import * as F2P060 from './F2P-060.mjs'
import * as F2P062 from './F2P-062.mjs'
import * as F2P063 from './F2P-063.mjs'
import * as F2P065 from './F2P-065.mjs'
import * as F2P066 from './F2P-066.mjs'
import * as F2P067 from './F2P-067.mjs'
import * as F2P069 from './F2P-069.mjs'
import * as F2P071 from './F2P-071.mjs'
import * as F2P050 from './F2P-050.mjs'
import * as F2P051 from './F2P-051.mjs'
import * as F2P052 from './F2P-052.mjs'
import * as F2P054 from './F2P-054.mjs'
import * as F2P055 from './F2P-055.mjs'
import * as F2P056 from './F2P-056.mjs'
import * as F2P057 from './F2P-057.mjs'
import * as F2P058 from './F2P-058.mjs'
import * as F2P059 from './F2P-059.mjs'
import * as F2P061 from './F2P-061.mjs'

export const side = 'target'
export const observations = [
  F2P064.default,
  F2P068.default,
  F2P070.default,
  F2P072.default,
  F2P049.default,
  F2P060.default,
  F2P062.default,
  F2P063.default,
  F2P065.default,
  F2P066.default,
  F2P067.default,
  F2P069.default,
  F2P071.default,
  F2P050.default,
  F2P051.default,
  F2P052.default,
  F2P054.default,
  F2P055.default,
  F2P056.default,
  F2P057.default,
  F2P058.default,
  F2P059.default,
  F2P061.default
]

export async function runAll(repo) {
  return runChecks(
    resolveRepo(repo),
    side,
    observations.map((observation) => observation.id)
  )
}

export default { side, observations, runAll }
