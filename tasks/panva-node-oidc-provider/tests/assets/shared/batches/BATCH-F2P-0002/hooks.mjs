import { appendFileSync } from 'node:fs';

const out = process.env.BATCH_F2P_0002_TRACE_OUT;

export async function resolve(specifier, context, next) {
  const result = await next(specifier, context);
  try {
    if (out && result && result.url) appendFileSync(out, `${result.url}\n`);
  } catch {
    // tracing must never break module resolution
  }
  return result;
}
