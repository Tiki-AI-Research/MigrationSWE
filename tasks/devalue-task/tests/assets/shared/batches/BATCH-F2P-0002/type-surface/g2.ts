import type { JavaScriptFragment, JavaScriptTag } from 'devalue';

declare const js: JavaScriptTag;

const template: JavaScriptFragment = js`a${1}b${'c'}d${true}e${null}f${undefined}g${10n}h${Symbol('s')}`;
const nonEmpty: [JavaScriptFragment] extends [never] ? never : true = true;

void template;
void nonEmpty;
