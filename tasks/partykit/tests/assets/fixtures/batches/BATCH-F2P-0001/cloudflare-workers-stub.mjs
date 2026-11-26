const active = () => globalThis.__cloudflareEnv ?? {};

export const env = new Proxy(
  {},
  {
    get(_target, property) {
      return Reflect.get(active(), property);
    },
    has(_target, property) {
      return Reflect.has(active(), property);
    },
    ownKeys() {
      return Reflect.ownKeys(active());
    },
    getOwnPropertyDescriptor(_target, property) {
      return {
        configurable: true,
        enumerable: true,
        value: Reflect.get(active(), property)
      };
    }
  }
);
