const kVolatileJsonKeys = new Set([
  "date",
  "etag",
  "exp",
  "expires",
  "iat",
  "lastModified",
  "nonce",
  "retryAfterSec",
  "timestamp",
  "uptime",
]);

const kIgnoredHeaders = new Set([
  "age",
  "cache-control",
  "connection",
  "content-length",
  "date",
  "etag",
  "expires",
  "keep-alive",
  "last-modified",
  "transfer-encoding",
  "vary",
]);

const kTrackedResponseHeaders = new Set([
  "content-type",
  "location",
  "retry-after",
  "set-cookie",
  "www-authenticate",
  "x-accel-buffering",
  "x-hook",
  "x-request-id",
  "x-test",
]);

const kTrackedGatewayHeaders = new Set([
  "authorization",
  "content-length",
  "content-type",
  "cookie",
]);

const normalizeScalar = (value) => {
  if (typeof value === "string") {
    return value
      .replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z/g, "<timestamp>")
      .replace(/setup_token=[^;\s]+/g, "setup_token=<token>")
      .replace(/\/tmp\/[^"\s]+/g, "<tmp-path>");
  }
  return value;
};

const normalizeJson = (value) => {
  if (Array.isArray(value)) return value.map(normalizeJson);
  if (!value || typeof value !== "object") return normalizeScalar(value);
  const result = {};
  for (const key of Object.keys(value).sort()) {
    if (kVolatileJsonKeys.has(key)) continue;
    result[key] = normalizeJson(value[key]);
  }
  return result;
};

const parseBody = (body, contentType = "") => {
  if (body === undefined || body === null) return null;
  const text = Buffer.isBuffer(body) ? body.toString("utf8") : String(body);
  if (!text) return "";
  if (String(contentType).includes("application/json")) {
    try {
      return { kind: "json", value: normalizeJson(JSON.parse(text)) };
    } catch {
      return { kind: "text", value: text };
    }
  }
  if (String(contentType).includes("text/event-stream")) {
    return {
      kind: "sse",
      value: text
        .split(/\r?\n/)
        .map((line) => line.trimEnd())
        .filter(Boolean)
        .map((line) => normalizeScalar(line)),
    };
  }
  return { kind: "text", value: normalizeScalar(text) };
};

const normalizeMediaType = (value) => {
  const mediaType = String(value || "").split(";")[0].trim().toLowerCase();
  if (!mediaType) return "";
  if (mediaType === "text/plain") return "";
  return mediaType;
};

const normalizeCookie = (cookie) => {
  const parts = String(cookie)
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length === 0) return "";
  const [pair, ...attributes] = parts;
  const separator = pair.indexOf("=");
  const name = (separator >= 0 ? pair.slice(0, separator) : pair).trim();
  const cleared = parts.some((part) =>
    /^(?:max-age=0|expires=thu,?\s+01\s+jan\s+1970)/i.test(part),
  );
  const normalizedAttributes = attributes
    .map((part) => part.toLowerCase())
    .filter((part) => !/^(?:expires|max-age)=/i.test(part))
    .sort();
  return [name, ...(cleared ? ["cleared"] : []), ...normalizedAttributes].join(";");
};

const normalizeHeaders = (headers = {}, options = {}) => {
  const names = options.names;
  const result = {};
  for (const [rawName, rawValue] of Object.entries(headers || {})) {
    const name = String(rawName).toLowerCase();
    if (kIgnoredHeaders.has(name)) continue;
    if (names !== undefined && names !== null) {
      const allow = names instanceof Set ? names : new Set(names);
      if (!allow.has(name)) continue;
    } else if (!kTrackedResponseHeaders.has(name)) {
      continue;
    }
    const value = Array.isArray(rawValue) ? rawValue.join(", ") : String(rawValue);
    if (name === "set-cookie") {
      result[name] = value
        .split(/,(?=\s*[^;,]+=)/)
        .map(normalizeCookie)
        .filter(Boolean)
        .sort();
      continue;
    }
    if (name === "content-type") {
      const mediaType = normalizeMediaType(value);
      if (mediaType) result[name] = mediaType;
      continue;
    }
    result[name] = normalizeScalar(value);
  }
  return result;
};

const normalizeGatewayHeaders = (headers = {}) => {
  const result = {};
  for (const [rawName, rawValue] of Object.entries(headers || {})) {
    const name = String(rawName).toLowerCase();
    if (!kTrackedGatewayHeaders.has(name)) continue;
    const value = Array.isArray(rawValue) ? rawValue.join(", ") : String(rawValue);
    if (name === "content-type") {
      result[name] = String(value).split(";")[0].trim().toLowerCase();
      continue;
    }
    if (name === "cookie") {
      result[name] = normalizeScalar(value);
      continue;
    }
    result[name] = normalizeScalar(value);
  }
  return result;
};

const normalizeGateway = (gateway) => {
  if (!gateway) return null;
  const normalized = normalizeJson(gateway);
  normalized.headers = normalizeGatewayHeaders(gateway.headers || {});
  return normalized;
};

const normalizeExchange = (exchange = {}, options = {}) => {
  if (options.ignore) return {};
  const headers = normalizeHeaders(exchange.headers || {}, {
    names: options.headerNames,
  });
  const result = {};
  if (options.status !== false) {
    const numericStatus = Number(exchange.status);
    result.status = Number.isFinite(numericStatus)
      ? numericStatus
      : exchange.status ?? 0;
  }
  if (options.headers !== false) result.headers = headers;
  if (options.body !== false) {
    if (result.status >= 300 && result.status < 400) {
      result.body = null;
    } else {
      result.body = parseBody(
        exchange.body ?? exchange.text,
        headers["content-type"] || "",
      );
    }
  }
  if (options.gateway !== false) {
    result.gateway = normalizeGateway(exchange.gateway || exchange.gateway_upgrade);
  }
  return result;
};

const stableStringify = (value) => {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (!value || typeof value !== "object") return JSON.stringify(value);
  return `{${Object.keys(value)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`)
    .join(",")}}`;
};

const deepEqual = (left, right) => {
  if (left === right) return true;
  if (typeof left !== typeof right) return false;
  if (!left || !right || typeof left !== "object") return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  if (leftKeys.length !== rightKeys.length) return false;
  for (let index = 0; index < leftKeys.length; index += 1) {
    if (leftKeys[index] !== rightKeys[index]) return false;
    if (!deepEqual(left[leftKeys[index]], right[rightKeys[index]])) return false;
  }
  return true;
};

module.exports = {
  deepEqual,
  normalizeExchange,
  normalizeGateway,
  normalizeHeaders,
  normalizeJson,
  parseBody,
  stableStringify,
};
