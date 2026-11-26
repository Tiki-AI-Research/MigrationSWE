import type { UnevalReplacer } from 'devalue';

const undef: UnevalReplacer = () => undefined;
const nul: UnevalReplacer = () => null;
const falsy: UnevalReplacer = () => false;
const nothing: UnevalReplacer = () => {};

void undef;
void nul;
void falsy;
void nothing;
