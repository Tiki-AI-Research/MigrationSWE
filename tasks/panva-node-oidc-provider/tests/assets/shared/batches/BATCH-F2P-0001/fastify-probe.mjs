import { channel } from 'node:diagnostics_channel';

const target = new URL(import.meta.url).searchParams.get('target');
const { default: original } = await import(target);
const observed = channel('migration.fastify.onSend');

const wrapped = new Proxy(original, {
  get(factory, key, receiver) {
    if (key === 'fastify' || key === 'default') return wrapped;
    return Reflect.get(factory, key, receiver);
  },
  apply(factory, receiver, args) {
    const app = Reflect.apply(factory, receiver, args);
    app.addHook('onSend', (request, reply, payload, done) => {
      // A raw/hijacked response still cannot establish Fastify ownership.
      if (reply.request === request && request.raw && reply.raw) observed.publish();
      done(null, payload);
    });
    return app;
  },
});

export { wrapped as fastify };
export default wrapped;
