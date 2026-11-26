let currentEnv = {};

// cloudflare:workers exports a stable binding proxy. Cached imports must
// still observe the active request, rather than the first scenario's object.
export const env = new Proxy({}, {
  get: (_target, key) => Reflect.get(currentEnv, key),
  set: (_target, key, value) => Reflect.set(currentEnv, key, value),
  has: (_target, key) => Reflect.has(currentEnv, key),
  ownKeys: () => Reflect.ownKeys(currentEnv),
  getOwnPropertyDescriptor(_target, key) {
    const descriptor = Reflect.getOwnPropertyDescriptor(currentEnv, key);
    return descriptor && { ...descriptor, configurable: true };
  }
});

export function setEnv(next) {
  currentEnv = next ?? {};
}
