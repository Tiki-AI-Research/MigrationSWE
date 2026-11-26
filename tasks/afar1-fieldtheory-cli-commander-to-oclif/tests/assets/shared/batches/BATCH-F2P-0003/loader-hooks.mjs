import { appendFileSync } from 'node:fs';

export async function resolve(specifier, context, nextResolve) {
  const result = await nextResolve(specifier, context);
  const log = process.env.FT_MODULE_LOG;
  if (log) {
    appendFileSync(log, `${JSON.stringify({ specifier, url: result.url })}\n`);
  }
  return result;
}
