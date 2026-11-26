#!/usr/bin/env node
// Real-JS module-name evaluator.
//
// Given a single source file, it finds every dynamic module load
// (`import(...)`, `require(...)`, and `require`-like aliases) and evaluates the
// argument expression with the real JavaScript engine, replaying the file's
// constant assignments first.  This makes the *value* of the specifier come
// from JS semantics, so any constant construction -- `+=` reassignment,
// `Array.from`, `String.fromCharCode`, template/concat/join, control flow,
// `Buffer`, `atob`, ... -- is observed exactly, instead of being enumerated
// form by form.
//
// Usage: node module-name-eval.cjs <file>
// Output: {"args": [...], "values": [<string|null>, ...]}
'use strict';

const fs = require('node:fs');
const vm = require('node:vm');

function stripComments(text) {
  const characters = Array.from(text);
  const length = text.length;
  let state = 'normal';
  let quote = '';
  let index = 0;
  while (index < length) {
    const character = text[index];
    if (state === 'normal') {
      if (character === '/' && index + 1 < length && text[index + 1] === '/') {
        while (index < length && text[index] !== '\n') { characters[index] = ' '; index += 1; }
        continue;
      }
      if (character === '/' && index + 1 < length && text[index + 1] === '*') {
        characters[index] = ' '; characters[index + 1] = ' '; index += 2;
        while (index < length && !(text[index] === '*' && index + 1 < length && text[index + 1] === '/')) {
          if (text[index] !== '\n') characters[index] = ' ';
          index += 1;
        }
        if (index < length) { characters[index] = ' '; index += 1; }
        if (index < length) { characters[index] = ' '; index += 1; }
        continue;
      }
      if (character === "'" || character === '"' || character === '`') { state = 'string'; quote = character; index += 1; continue; }
      index += 1;
      continue;
    }
    if (character === '\\') { index += 2; continue; }
    if (character === quote) { state = 'normal'; quote = ''; index += 1; continue; }
    index += 1;
  }
  return characters.join('');
}

function requireLikeNames(source) {
  const names = new Set(['require']);
  const importRe = /import\s*\{([^}]*)\}\s*from\s*['"]node:module['"]/g;
  for (const match of source.matchAll(importRe)) {
    for (const part of match[1].split(',')) {
      const alias = part.match(/\s*createRequire(?:\s+as\s+([A-Za-z_$][\w$]*))?\s*$/);
      if (alias) names.add(alias[1] || 'createRequire');
    }
  }
  for (let pass = 0; pass < 5; pass += 1) {
    const before = names.size;
    for (const match of source.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:[\w$.]+\.)?createRequire\s*\(/g)) names.add(match[1]);
    for (const match of source.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*\(/g)) if (names.has(match[2])) names.add(match[1]);
    if (names.size === before) break;
  }
  return names;
}

function dynamicArgs(source) {
  const args = [];
  for (const match of source.matchAll(/(?:^|[^\w$.])import\s*\(\s*([^)]*?)\s*\)/g)) args.push(match[1]);
  for (const match of source.matchAll(/\brequire\s*\(\s*([^)]*?)\s*\)/g)) args.push(match[1]);
  for (const name of requireLikeNames(source)) {
    if (name === 'require') continue;
    const re = new RegExp('\\b' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\(\\s*([^)]*?)\\s*\\)', 'g');
    for (const match of source.matchAll(re)) args.push(match[1]);
  }
  return args;
}

function balanced(text) {
  let depth = 0;
  let quote = '';
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (quote) {
      if (character === '\\') { index += 1; continue; }
      if (character === quote) quote = '';
      continue;
    }
    if (character === "'" || character === '"' || character === '`') { quote = character; continue; }
    if ('([{'.includes(character)) depth += 1;
    else if (')]}'.includes(character)) { depth -= 1; if (depth < 0) return false; }
  }
  return depth === 0 && quote === '';
}

function extractAssignments(source) {
  const assignments = [];
  const names = new Set();
  const declaration = /(?:^|[;\n{}(,])\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)/g;
  const reassign = /(?:^|[^\w$.'"])([A-Za-z_$][\w$]*)\s*(\+=|-=|=)(?!=)\s*([^;\n]+)/g;
  for (const match of source.matchAll(declaration)) { names.add(match[1]); assignments.push({ name: match[1], op: '=', rhs: match[2].trim() }); }
  for (const match of source.matchAll(reassign)) {
    if (match[2] === '=' && names.has(match[1])) assignments.push({ name: match[1], op: '=', rhs: match[3].trim() });
    else if (match[2] !== '=') { names.add(match[1]); assignments.push({ name: match[1], op: match[2], rhs: match[3].trim() }); }
  }
  return { assignments, names };
}

function main() {
  const file = process.argv[2];
  const source = stripComments(fs.readFileSync(file, 'utf8'));
  const args = dynamicArgs(source);
  if (args.length === 0) {
    process.stdout.write(JSON.stringify({ args: [], values: [] }));
    return;
  }
  const { assignments, names } = extractAssignments(source);
  let preamble = '';
  for (const name of names) preamble += `let ${name};\n`;
  for (const assignment of assignments) {
    const rhs = assignment.rhs;
    if (!balanced(rhs) || /import\s*\.\s*meta|await\b|\bimport\s*\(/.test(rhs)) continue;
    preamble += `try { ${assignment.name} ${assignment.op} (${rhs}); } catch (e) {}\n`;
  }
  let values;
  try {
    const sandbox = {
      Buffer,
      atob: typeof atob === 'function' ? atob : undefined,
      btoa: typeof btoa === 'function' ? btoa : undefined,
      TextDecoder,
      TextEncoder,
    };
    vm.createContext(sandbox);
    try {
      vm.runInContext(preamble, sandbox, { timeout: 3000 });
    } catch (error) {
      /* assignments that fail to parse simply leave their names undefined */
    }
    values = args.map((argument) => {
      if (!balanced(argument) || /import\s*\.\s*meta/.test(argument)) return null;
      try {
        const value = vm.runInContext(`(${argument})`, sandbox, { timeout: 2000 });
        return typeof value === 'string' && value ? value : null;
      } catch (error) {
        return null;
      }
    });
  } catch (error) {
    values = args.map(() => null);
  }
  process.stdout.write(JSON.stringify({ args, values }));
}

try {
  main();
} catch (error) {
  process.stdout.write(JSON.stringify({ args: [], values: [], error: String(error && error.message ? error.message : error) }));
}
