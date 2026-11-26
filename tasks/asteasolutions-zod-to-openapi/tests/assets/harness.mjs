import { runMain } from './shared/integration/run-observations.mjs';

runMain({ side: 'target' }).catch(error => {
  process.stderr.write(String((error && error.stack) || error) + '\n');
  process.exitCode = 1;
});
