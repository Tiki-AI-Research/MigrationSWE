import { runObservation } from '../../../shared/batches/BATCH-F2P-0003/checks.mjs'

export const id = 'F2P-066'
export const side = 'target'

export async function run(repo) {
  return runObservation(repo, side, id)
}

export default { id, side, run }
