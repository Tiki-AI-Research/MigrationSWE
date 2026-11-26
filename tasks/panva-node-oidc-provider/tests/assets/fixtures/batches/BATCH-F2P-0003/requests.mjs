/*
 * Fixtures for BATCH-F2P-0003 probes.
 *
 * These values are the concrete inputs named by the observations' required
 * postconditions. They are shared by the source-side and target-side probes so
 * that both sides exercise the exact same predicate.
 */

export const origin = 'https://rp.example';

export const features = {
  devInteractions: { enabled: false },
};

export const clients = [{
  client_id: 'client',
  client_secret: 'secret',
  redirect_uris: ['https://rp.example/cb'],
  grant_types: ['authorization_code', 'refresh_token'],
}];

export const defaultConfig = {
  clients,
  features,
};

export const notFoundPath = '/definitely-not-a-route';

export const discoveryPath = '/.well-known/openid-configuration';

export const jwksPath = '/jwks';

export const preflight = {
  method: 'OPTIONS',
  headers: {
    Origin: origin,
    'Access-Control-Request-Method': 'POST',
    'Access-Control-Request-Headers': 'content-type',
  },
};

export const unsupportedContentType = {
  method: 'POST',
  headers: { 'content-type': 'application/xml' },
  body: '<request/>',
};

export const oversizedUrlEncoded = {
  method: 'POST',
  headers: { 'content-type': 'application/x-www-form-urlencoded' },
  body: `grant_type=authorization_code&code=${'a'.repeat(60 * 1024)}`,
};

export const tokenForm = (clientId) => ({
  method: 'POST',
  headers: { 'content-type': 'application/x-www-form-urlencoded' },
  body: `grant_type=authorization_code&client_id=${clientId}`,
});
