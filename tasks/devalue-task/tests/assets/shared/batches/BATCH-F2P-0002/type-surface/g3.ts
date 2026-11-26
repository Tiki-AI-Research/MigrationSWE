import type { JavaScriptTag, UnevalReplacer } from 'devalue';

const replacer: UnevalReplacer = (value, js: JavaScriptTag) => {
	if (typeof value === 'number') return js`${value}`;
	return undefined;
};

void replacer;
