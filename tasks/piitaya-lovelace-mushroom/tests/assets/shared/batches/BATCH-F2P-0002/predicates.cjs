"use strict";

// Semantic predicates for BATCH-F2P-0002.
//
// Each predicate is evaluated with the SAME function body in both environments.
// The predicates use only the target framework entry points that the frozen
// contract requires (Zod `safeParse` / `z.ZodError`), so the Superstruct
// baseline fails them because the target capability is absent, not because of
// an unrelated setup error. `runProbes` returns one entry per predicate id.
//
// Predicate ids are stable and are mapped to observation ids in
// shared/batches/BATCH-F2P-0002/manifest.json.

function isZodError(e) {
  return !!e && (e.name === "ZodError" || Array.isArray(e.issues));
}

function makeHass() {
  return {
    localize(key) {
      let s = String(key);
      for (let i = 1; i + 1 < arguments.length; i += 2) {
        s += " " + String(arguments[i]) + "=" + String(arguments[i + 1]);
      }
      return s;
    },
  };
}

function evaluate(fn, symbols, hass) {
  try {
    const result = fn(symbols, hass);
    return { pass: !!(result && result.ok === true), result };
  } catch (e) {
    return { pass: false, error: String((e && e.message) || e) };
  }
}

function runProbes(symbols) {
  const hass = makeHass();
  const out = {};
  for (const id of Object.keys(PREDICATES)) {
    out[id] = evaluate(PREDICATES[id], symbols, hass);
  }
  return out;
}

function summarise(results) {
  const ids = Object.keys(results);
  const failed = ids.filter((id) => !(results[id] && results[id].pass === true));
  return { pass: failed.length === 0, total: ids.length, failed };
}

function loadedFrom(pkg) {
  const cache = (typeof require !== "undefined" && require.cache) || {};
  const a = "/node_modules/" + pkg + "/";
  const b = "\\node_modules\\" + pkg + "\\";
  return Object.keys(cache).some((p) => p.includes(a) || p.includes(b));
}

function predicateR001(s, hass) {
  const good = s.validateConfig(s.entityCardConfigStruct, {
    type: "custom:mushroom-entity-card",
    entity: "light.kitchen",
  });
  if (!good || good.success !== true) {
    return { ok: false, why: "valid bound card config did not validate" };
  }
  const res = s.validateConfig(s.entityCardConfigStruct, {
    type: "custom:mushroom-entity-card",
    entity: 42,
  });
  if (!res || res.success !== false) {
    return { ok: false, why: "wrong-typed entity was not rejected" };
  }
  if (!isZodError(res.error)) {
    return { ok: false, why: "rejection is not a z.ZodError" };
  }
  const h = s.handleStructError(
    hass,
    res.error,
    { type: "custom:mushroom-entity-card", entity: 42 }
  );
  const msgs = [].concat(h.warnings || [], h.errors || []);
  const localized =
    msgs.length > 0 &&
    msgs.every((m) => typeof m === "string" && m.startsWith("ui.errors.config."));
  return { ok: localized, why: localized ? "zod issues localized" : "not localized" };
}

function predicateS001(s, hass) {
  const res = s.validateConfig(s.entityCardConfigStruct, {
    type: "custom:mushroom-entity-card",
    entity: 42,
  });
  if (!res || res.success !== false || !isZodError(res.error)) {
    return { ok: false, why: "no z.ZodError produced" };
  }
  const h = s.handleStructError(
    hass,
    res.error,
    { type: "custom:mushroom-entity-card", entity: 42 }
  );
  const detected =
    Array.isArray(h.warnings) && Array.isArray(h.errors) && h.warnings.length > 0;
  return { ok: detected, why: detected ? "zod error detected" : "zod error not detected" };
}

function predicateS004(s, hass) {
  // The preserved contract only requires the wrong-type warning to include the
  // offending value. How that value reaches the message -- carried on the
  // framework error, or recovered from the original input handed to
  // handleStructError -- is an implementation choice and MUST NOT be asserted.
  // Two distinct offending values additionally reject a hardcoded value.
  const recover = (value) => {
    const input = { entity: value };
    const res = s.validateConfig(s.entitySharedConfigStruct, input);
    if (!res || res.success !== false || !isZodError(res.error)) {
      return { ok: false, why: "no z.ZodError produced" };
    }
    const handled = s.handleStructError(hass, res.error, input);
    const msg = (handled.warnings || []).find((m) => m.includes("entity"));
    if (!msg || !msg.includes(String(value))) {
      return { ok: false, why: "offending value not recovered" };
    }
    return { ok: true };
  };
  const first = recover(42);
  if (!first.ok) {
    return { ok: false, why: first.why };
  }
  const second = recover(99);
  if (!second.ok) {
    return { ok: false, why: second.why };
  }
  return { ok: true, why: "offending value recovered for distinct inputs" };
}

function predicateS002(s, hass) {
  const input = { entity: "light.kitchen", bogus: 1 };
  const res = s.validateConfig(s.entitySharedConfigStruct, input);
  if (!res || res.success !== false || !isZodError(res.error)) {
    return { ok: false, why: "no z.ZodError produced" };
  }
  const unrec = res.error.issues.some(
    (i) => i.code === "unrecognized_keys" && (i.keys || []).includes("bogus")
  );
  if (!unrec) {
    return { ok: false, why: "extra key was accepted" };
  }
  const h = s.handleStructError(hass, res.error, input);
  const warned = (h.warnings || []).some(
    (m) => m.includes("key_not_expected") && m.includes("bogus")
  );
  return { ok: warned, why: warned ? "key_not_expected reported" : "not reported" };
}

function predicateS008(s) {
  const known = s.validateConfig(s.entityCardConfigStruct, {
    type: "custom:mushroom-entity-card",
  });
  if (!known || known.success !== true) {
    return { ok: false, why: "known config rejected" };
  }
  const res = s.validateConfig(s.entityCardConfigStruct, {
    type: "custom:mushroom-entity-card",
    bogus: 1,
  });
  if (!res || res.success !== false || !isZodError(res.error)) {
    return { ok: false, why: "no z.ZodError produced" };
  }
  const unrec = res.error.issues.some(
    (i) => i.code === "unrecognized_keys" && (i.keys || []).includes("bogus")
  );
  return { ok: unrec, why: unrec ? "composed schema is strict" : "extra key accepted" };
}

function predicateS007(s) {
  const none = s.validateConfig(s.actionConfigStruct, { action: "none" });
  const url = s.validateConfig(s.actionConfigStruct, { action: "url" });
  if (!none || none.success !== true) {
    return { ok: false, why: "action 'none' rejected" };
  }
  if (!url || url.success !== false || !isZodError(url.error)) {
    return { ok: false, why: "action 'url' not rejected" };
  }
  const pathIssue = url.error.issues.some(
    (i) => (i.path || []).join(".") === "url_path"
  );
  return { ok: pathIssue, why: pathIssue ? "dispatched on action" : "no url_path issue" };
}

function predicateS010(s, hass) {
  const h1 = s.handleStructError(hass, new TypeError("boom"));
  const passthrough =
    h1 &&
    JSON.stringify(h1.warnings) === JSON.stringify(["boom"]) &&
    h1.errors === undefined;
  const res = s.validateConfig(s.entityCardConfigStruct, {
    type: "custom:mushroom-entity-card",
    entity: 42,
  });
  if (!res || res.success !== false || !isZodError(res.error)) {
    return { ok: false, why: "no z.ZodError produced" };
  }
  const h2 = s.handleStructError(
    hass,
    res.error,
    { type: "custom:mushroom-entity-card", entity: 42 }
  );
  const detects = Array.isArray(h2.errors) && h2.warnings.length > 0;
  const ok = Boolean(passthrough) && detects;
  return { ok, why: ok ? "guard is z.ZodError-based" : "guard mismatch" };
}

function predicateR017(s) {
  const superstructLoaded = loadedFrom("superstruct");
  const zodLoaded = loadedFrom("zod");
  let ranZod = false;
  try {
    const res = s.validateConfig(s.entityCardConfigStruct, {
      type: "custom:mushroom-entity-card",
      entity: "light.kitchen",
    });
    ranZod = !!(res && res.success === true);
  } catch {
    ranZod = false;
  }
  const ok = !superstructLoaded && zodLoaded && ranZod;
  return {
    ok,
    why: "superstructLoaded=" + superstructLoaded + " zodLoaded=" + zodLoaded + " ran=" + ranZod,
  };
}

const PREDICATES = {
  "P-R001": predicateR001,
  "P-S001": predicateS001,
  "P-S004": predicateS004,
  "P-S002": predicateS002,
  "P-S008": predicateS008,
  "P-S007": predicateS007,
  "P-S010": predicateS010,
  "P-R017": predicateR017,
};

module.exports = { runProbes, summarise, PREDICATES };
