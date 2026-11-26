// Minimal provider configuration fixture shared by the BATCH-F2P-0002 probes.
// It intentionally relies only on public configuration keys, so the same
// fixture constructs a provider on both the source (Koa) and target (Fastify)
// trees; the framework choice is observed from the executed module graph, not
// from the configuration.
export default {
  clients: [],
  features: {},
  enableHttpPostMethods: true,
  responseTypes: ['code', 'code id_token', 'id_token', 'none'],
  cookies: { long: { sameSite: 'none' } },
  // Custom error page keeps the provider from emitting its development-only
  // "default renderError" notice during the probes.
  renderError: async (ctx) => {
    ctx.type = 'text/html';
    ctx.body = 'oops! something went wrong';
  },
};
