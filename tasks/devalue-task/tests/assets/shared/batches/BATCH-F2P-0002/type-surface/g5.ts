import type { UnevalReplacer } from 'devalue';

// @ts-expect-error a 5.x string result is no longer an accepted replacer result
const text: UnevalReplacer = () => 'string';
// @ts-expect-error a plain object is not a fragment
const object: UnevalReplacer = () => ({});
// @ts-expect-error a number is not a fragment
const number: UnevalReplacer = () => 1;
// @ts-expect-error true is not one of the accepted falsy fallbacks
const boolean: UnevalReplacer = () => true;
// @ts-expect-error a returned function is not a fragment
const callable: UnevalReplacer = () => () => 1;

void text;
void object;
void number;
void boolean;
void callable;
