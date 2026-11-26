// Install the same live-response probe for ESM default and named imports.
// The wrapper's own import must resolve to the real dependency, without a loop.
let target;
const wrapper = new URL('./fastify-probe.mjs', import.meta.url);

export function initialize(data) {
  target = data.target;
}

export async function resolve(specifier, context, nextResolve) {
  const result = await nextResolve(specifier, context);
  if (result.url === target && !context.parentURL?.startsWith(wrapper.href)) {
    const url = new URL(wrapper);
    url.searchParams.set('target', target);
    return { url: url.href, shortCircuit: true };
  }
  return result;
}
