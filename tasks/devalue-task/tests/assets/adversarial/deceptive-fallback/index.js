// Adversarial fixture: pretends to be a migrated package while delegating every
// public entry point to the old published implementation.
export { stringify, stringifyAsync } from 'devalue';
export { parse, unflatten } from 'devalue';
export { uneval } from 'devalue';
export { DevalueError } from 'devalue';
