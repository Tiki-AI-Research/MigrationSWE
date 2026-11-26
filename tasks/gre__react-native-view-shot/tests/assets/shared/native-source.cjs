"use strict";

// Preserve quoted data (notably file:// and data URIs), blank comments only.
function codeOnly(text) {
  if (typeof text !== "string") return text;
  let out = "", i = 0;
  while (i < text.length) {
    const c = text[i], n = text[i + 1];
    if (c === '"' || c === "'" || c === "`") {
      const quote = c, verbatim = c === '"' && text[i - 1] === "@";
      out += text[i++];
      while (i < text.length) {
        const q = text[i++]; out += q;
        if (q === "\\" && !verbatim && i < text.length) out += text[i++];
        else if (q === quote) {
          if (verbatim && text[i] === quote) out += text[i++];
          else break;
        }
      }
    } else if (c === "/" && (n === "/" || n === "*")) {
      const block = n === "*"; out += "  "; i += 2;
      while (i < text.length) {
        if (block && text[i] === "*" && text[i + 1] === "/") { out += "  "; i += 2; break; }
        if (!block && text[i] === "\n") break;
        out += text[i] === "\n" ? "\n" : " "; i++;
      }
    } else out += text[i++];
  }
  return out;
}

function codeWithoutStrings(text) {
  if (typeof text !== "string") return text;
  return codeOnly(text).replace(/("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)/gs,
    (literal) => literal.replace(/[^\n]/g, " "));
}

// Match code against code and literal values against whole literal values.
// An inert string containing a method declaration/call cannot supply that code.
function containsSource(text, needle) {
  if (typeof text !== "string") return false;
  const clean = codeOnly(text), code = codeWithoutStrings(clean);
  let offset = clean.indexOf(needle);
  while (offset !== -1) {
    if (code.slice(offset, offset + needle.length) === codeWithoutStrings(needle) &&
        /\S/.test(codeWithoutStrings(needle))) return true;
    offset = clean.indexOf(needle, offset + 1);
  }
  const literals = clean.match(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/gs) || [];
  if (literals.some((literal) => literal === needle || literal.slice(1, -1) === needle)) return true;
  // Pure data checks (format names, module names, MIME prefixes) are not code.
  if (["data:image/", "base64", "deprecated"].includes(needle)) {
    return literals.some((literal) => {
      const value = literal.slice(1, -1);
      return value.includes(needle);
    });
  }
  return false;
}

// React's Objective-C classes do not necessarily have a same-named header.
// Resolve direct React imports against the trusted target package, rather than
// accepting a source file just because its method names look migrated.
const reactHeaderCache = new Map();
function missingReactHeaders(text, reactNativeRoot) {
  const fs = require("fs"), path = require("path");
  if (!reactHeaderCache.has(reactNativeRoot)) {
    const pkg = JSON.parse(fs.readFileSync(path.join(reactNativeRoot, "package.json"), "utf8"));
    if (pkg.version !== "0.85.0") throw new Error("React header check requires the trusted 0.85.0 package");
    const headers = new Set();
    function visit(directory) {
      for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
        const file = path.join(directory, entry.name);
        if (entry.isDirectory()) visit(file);
        else if (entry.isFile() && /\.h(?:pp)?$/.test(entry.name)) {
          const parts = path.relative(reactNativeRoot, file).split(path.sep);
          for (let i = 0; i < parts.length; i++) headers.add(parts.slice(i).join("/"));
        }
      }
    }
    for (const directory of ["React", "ReactCommon", "Libraries"]) {
      const location = path.join(reactNativeRoot, directory);
      if (fs.existsSync(location)) visit(location);
    }
    if (!headers.has("RCTBridgeModule.h")) throw new Error("Trusted React header inventory is incomplete");
    reactHeaderCache.set(reactNativeRoot, headers);
  }
  const headers = reactHeaderCache.get(reactNativeRoot);
  const missing = [], conditions = [];
  const active = () => conditions.every(frame => frame.current === true);
  const negate = value => value === null ? null : !value;
  const both = (a, b) => a === false || b === false ? false : a === true && b === true ? true : null;
  const either = (a, b) => a === true || b === true ? true : a === false && b === false ? false : null;
  const clean = codeOnly(text), masked = codeWithoutStrings(clean).split(/\r?\n/);
  // Only diagnose definitely active imports. Unknown build conditions remain
  // the native compiler's responsibility; optional/disabled imports are valid.
  for (const [index, line] of clean.split(/\r?\n/).entries()) {
    if (!/^\s*#/.test(masked[index])) continue;
    const directive = /^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$/.exec(line);
    if (directive) {
      const [, kind, expression] = directive;
      const condition = expression.trim() === "1" ? true : expression.trim() === "0" ? false : null;
      if (["if", "ifdef", "ifndef"].includes(kind)) {
        const value = kind === "if" ? condition : null;
        conditions.push({current: value, taken: value});
      }
      else if (kind === "endif") conditions.pop();
      else if (conditions.length) {
        const frame = conditions[conditions.length - 1];
        frame.current = kind === "else" ? negate(frame.taken) : both(negate(frame.taken), condition);
        frame.taken = kind === "else" ? true : either(frame.taken, condition);
      }
      continue;
    }
    const include = /^\s*#\s*(?:import|include)\s*[<"]React\/([^>"]+)[>"]/.exec(line);
    if (include && active() && !headers.has(include[1])) {
      missing.push({header: "React/" + include[1], line: index + 1});
    }
  }
  return missing;
}

module.exports = { codeOnly, codeWithoutStrings, containsSource, missingReactHeaders };
