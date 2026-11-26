// Positive control: the migrated implementation reads its own local modules and
// uses no subprocess, module-loader or worker bridge.
import { DevalueError } from './utils.js';

export function stringify(value) {
	if (typeof value === 'function') {
		throw new DevalueError('Cannot stringify a function', [], value, value);
	}

	return JSON.stringify([value]);
}
