import { pathToFileURL } from 'node:url';

import {
  CASES,
  normalize,
  makeProvider,
  createHttp,
  listen,
  close,
  createServerFor,
} from './corpus.mjs';

async function principal(root) {
  const { default: Provider } = await import(`${root}/lib/index.js`);
  const { default: MemoryAdapter } = await import(`${root}/lib/adapters/memory_adapter.js`);
  const { generateKeyPair, exportJWK } = await import('jose');
  const { privateKey } = await generateKeyPair('RS256', { extractable: true });
  const jwk = await exportJWK(privateKey);
  jwk.kid = 'main';
  jwk.alg = 'RS256';
  jwk.use = 'sig';
  return { Provider, MemoryAdapter, jwks: { keys: [jwk] } };
}

async function identity(root) {
  const mod = await import(`${root}/lib/index.js`);
  const Provider = mod.default;
  const provider = new Provider('http://127.0.0.1', {
    adapter: (await import(`${root}/lib/adapters/memory_adapter.js`)).default,
    findAccount: (_ctx, id) => ({ accountId: id, claims: () => ({ sub: id }) }),
    clients: [{
      client_id: 'client',
      client_secret: 'secret',
      redirect_uris: ['https://client.example.com/cb'],
      grant_types: ['authorization_code'],
      response_types: ['code'],
      token_endpoint_auth_method: 'client_secret_basic',
    }],
  });
  return {
    exports: Object.keys(mod).sort(),
    eventEmitter: provider instanceof (await import('node:events')).EventEmitter,
    callback: typeof provider.callback,
    listen: typeof provider.listen,
    createContext: typeof provider.createContext,
    use: typeof provider.use,
    hasMiddleware: 'middleware' in provider,
    hasKeys: 'keys' in provider,
    hasApp: 'app' in provider,
    hasMountPath: 'mountPath' in provider,
    outOfScopeSurfaceKept: typeof provider.use === 'function'
      && typeof provider.createContext === 'function'
      && 'middleware' in provider
      && 'keys' in provider
      && 'app' in provider,
  };
}

export async function run(root) {
  const { Provider, MemoryAdapter, jwks } = await principal(root);
  const send = createHttp();

  const servers = [];
  function start(provider) {
    const server = createServerFor(provider);
    servers.push(server);
    return listen(server);
  }

  const defaultProvider = makeProvider(Provider, MemoryAdapter, jwks);
  const proxyProvider = makeProvider(Provider, MemoryAdapter, jwks, { proxy: true });
  const defaultPort = await start(defaultProvider);
  const proxyPort = await start(proxyProvider);

  const result = {};

  for (const c of CASES) {
    try {
      const port = c.provider === 'proxy' ? proxyPort : defaultPort;
      const res = await send(port, c.req);
      result[c.name] = normalize(res, { port });
    } catch (error) {
      result[c.name] = { error: String(error && error.message) };
    }
  }

  try {
    result.identity = await identity(root);
  } catch (error) {
    result.identity = { error: String(error && error.message) };
  }

  await Promise.all(servers.map(close));
  return result;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const root = process.argv[2] ?? process.env.OIDC_REPO_ROOT ?? '/workspace/repo';
  run(root).then((result) => {
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  });
}
