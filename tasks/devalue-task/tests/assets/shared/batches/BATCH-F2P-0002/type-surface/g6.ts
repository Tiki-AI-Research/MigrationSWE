import type { JavaScriptFragment, JavaScriptTag } from 'devalue';

declare const js: JavaScriptTag;
declare const fragment: JavaScriptFragment;

// @ts-expect-error the tag is callable only as a tagged template
js('not a template');
// @ts-expect-error a fragment is opaque and cannot be forged from an object literal
const forged: JavaScriptFragment = { __fragment: true };

void fragment;
void forged;
