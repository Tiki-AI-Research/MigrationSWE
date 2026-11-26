import { test } from 'uvu';
import * as assert from 'uvu/assert';
import { parse, stringify, stringifyAsync } from '../../../index.js';

test('P2P-049 Map and Set round-trip with equivalent type, size and entries', () => {
	const map = new Map([
		['alpha', 1],
		['beta', { nested: true }]
	]);
	const set = new Set([1, 'two', { three: 3 }]);

	const revivedMap = parse(stringify(map));
	const revivedSet = parse(stringify(set));

	assert.ok(revivedMap instanceof Map);
	assert.ok(revivedSet instanceof Set);
	assert.is(revivedMap.size, map.size);
	assert.is(revivedSet.size, set.size);
	assert.equal([...revivedMap.entries()], [...map.entries()]);
	assert.equal([...revivedSet.values()], [...set.values()]);
});

test('P2P-050 cyclic and repeated references preserve instance identity', () => {
	const shared = { id: 'shared' };
	const root = { first: shared, second: shared };
	root.self = root;

	const revived = parse(stringify(root));

	assert.is(revived.self, revived);
	assert.is(revived.first, revived.second);
});

test('P2P-051 synchronous and asynchronous payloads are mutually consumable', async () => {
	const value = {
		label: 'payload',
		count: 2,
		entries: new Map([['key', 'value']])
	};

	const syncPayload = stringify(value);
	const asyncPayload = await stringifyAsync(value);

	const fromSync = parse(syncPayload);
	const fromAsync = parse(asyncPayload);

	assert.is(fromSync.label, 'payload');
	assert.is(fromSync.count, 2);
	assert.ok(fromSync.entries instanceof Map);
	assert.is(fromSync.entries.get('key'), 'value');

	assert.is(fromAsync.label, 'payload');
	assert.is(fromAsync.count, 2);
	assert.ok(fromAsync.entries instanceof Map);
	assert.is(fromAsync.entries.get('key'), 'value');
});

test.run();
