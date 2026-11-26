//! F2P target probe for BATCH-F2P-0002.
//!
//! Every observation owned by this batch is exercised through the public
//! `pev2_core` API. The example prints one JSON object mapping each
//! observation id to `{"ok": bool, "detail": string}` on stdout. A non-zero
//! exit code is used only for harness/compile level failures; per-observation
//! outcomes are reported in the JSON payload.
//!
//! The probe is intended to be copied (by the batch harness) into
//! `crates/pev2-core/examples/f2p_probe.rs` and executed against the migrated
//! target repository. It therefore relies on `CARGO_MANIFEST_DIR` to inspect
//! the crate manifest and sources for the dependency-policy observations.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use pev2_core::{
    cleanup_source, create_plan, find_node_by_id, find_node_by_subplan_name, from_json,
    from_source, from_text, get_plan_content, parse_node_type, should_show_prop, split_balanced,
    split_into_lines,
};
use serde_json::{json, Value};

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

fn get_f64(v: &Value, k: &str) -> Option<f64> {
    v.get(k).and_then(|x| x.as_f64())
}

fn get_i64(v: &Value, k: &str) -> Option<i64> {
    v.get(k).and_then(|x| x.as_i64())
}

fn get_str<'a>(v: &'a Value, k: &str) -> Option<&'a str> {
    v.get(k).and_then(|x| x.as_str())
}

fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-6
}

fn plan_of(source: &str) -> Result<Value, String> {
    let content = from_source(source).map_err(|e| format!("from_source: {e}"))?;
    let plan = create_plan("", content, "").map_err(|e| format!("create_plan: {e}"))?;
    serde_json::to_value(&plan).map_err(|e| e.to_string())
}

fn content_of(source: &str) -> Result<Value, String> {
    let plan = plan_of(source)?;
    plan.get("content")
        .cloned()
        .ok_or_else(|| "no content".to_string())
}

fn root_of(source: &str) -> Result<Value, String> {
    let content = content_of(source)?;
    content
        .get("Plan")
        .cloned()
        .ok_or_else(|| "no Plan".to_string())
}

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn read_all_rs(dir: &Path, out: &mut Vec<(PathBuf, String)>) {
    if let Ok(entries) = fs::read_dir(dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                read_all_rs(&path, out);
            } else if path.extension().map(|e| e == "rs").unwrap_or(false) {
                if let Ok(text) = fs::read_to_string(&path) {
                    out.push((path, text));
                }
            }
        }
    }
}

/// Blank Rust comments: a removed bridge mentioned in a comment must not be
/// reported as live delegation (the verifier FFI detector strips comments too).
fn strip_comments(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] == b'/' && i + 1 < bytes.len() && bytes[i + 1] == b'*' {
            if let Some(offset) = text[i + 2..].find("*/") {
                i = i + 2 + offset + 2;
                continue;
            }
            break;
        }
        if bytes[i] == b'/' && i + 1 < bytes.len() && bytes[i + 1] == b'/' {
            match text[i..].find('\n') {
                Some(offset) => {
                    out.push('\n');
                    i += offset + 1;
                    continue;
                }
                None => break,
            }
        }
        let character = text[i..].chars().next().unwrap();
        out.push(character);
        i += character.len_utf8();
    }
    out
}

/// Decode ``\xHH`` / ``\uHHHH`` / ``\u{...}`` / octal / simple escapes.
fn unescape_body(body: &str) -> String {
    let chars: Vec<char> = body.chars().collect();
    let mut out = String::new();
    let mut i = 0usize;
    while i < chars.len() {
        let c = chars[i];
        if c != '\\' || i + 1 >= chars.len() {
            out.push(c);
            i += 1;
            continue;
        }
        let next = chars[i + 1];
        if next == 'x' && i + 3 < chars.len() {
            let hex: String = chars[i + 2..i + 4].iter().collect();
            if let Ok(code) = u32::from_str_radix(&hex, 16) {
                if let Some(ch) = char::from_u32(code) {
                    out.push(ch);
                    i += 4;
                    continue;
                }
            }
        }
        if next == 'u' {
            if i + 2 < chars.len() && chars[i + 2] == '{' {
                if let Some(close) = chars[i + 3..].iter().position(|&x| x == '}') {
                    let hex: String = chars[i + 3..i + 3 + close].iter().collect();
                    if let Ok(code) = u32::from_str_radix(&hex, 16) {
                        if let Some(ch) = char::from_u32(code) {
                            out.push(ch);
                            i = i + 3 + close + 1;
                            continue;
                        }
                    }
                }
            } else if i + 5 < chars.len() {
                let hex: String = chars[i + 2..i + 6].iter().collect();
                if let Ok(code) = u32::from_str_radix(&hex, 16) {
                    if let Some(ch) = char::from_u32(code) {
                        out.push(ch);
                        i += 6;
                        continue;
                    }
                }
            }
        }
        if next.is_digit(8) {
            let mut j = i + 1;
            while j < chars.len() && j < i + 4 && chars[j].is_digit(8) {
                j += 1;
            }
            let oct: String = chars[i + 1..j].iter().collect();
            if let Ok(code) = u32::from_str_radix(&oct, 8) {
                if let Some(ch) = char::from_u32(code) {
                    out.push(ch);
                    i = j;
                    continue;
                }
            }
        }
        let mapped = match next {
            'n' => '\n',
            'r' => '\r',
            't' => '\t',
            '0' => '\0',
            'a' => '\u{7}',
            'b' => '\u{8}',
            'f' => '\u{c}',
            'v' => '\u{b}',
            '\\' => '\\',
            '\'' => '\'',
            '"' => '"',
            '?' => '?',
            other => other,
        };
        out.push(mapped);
        i += 2;
    }
    out
}

/// Decode a string / byte / raw / char literal, or return ``None``.
fn decode_string_literal(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut idx = 0usize;
    let mut raw = false;
    while idx < bytes.len() && idx < 2 {
        match bytes[idx] {
            b'r' | b'R' => raw = true,
            b'b' | b'B' => {}
            _ => break,
        }
        idx += 1;
    }
    let rest = &text[idx..];
    let mut chars = rest.chars();
    let quote = chars.next()?;
    if quote != '"' && quote != '\'' {
        return None;
    }
    if rest.len() < 2 || !rest.ends_with(quote) {
        return None;
    }
    let inner = &rest[1..rest.len() - 1];
    if raw {
        Some(inner.to_string())
    } else {
        Some(unescape_body(inner))
    }
}

fn parse_int(text: &str) -> Option<u32> {
    let t = text.trim().trim_start_matches('+');
    if let Some(hex) = t.strip_prefix("0x").or_else(|| t.strip_prefix("0X")) {
        return u32::from_str_radix(hex, 16).ok();
    }
    if let Some(oct) = t.strip_prefix("0o").or_else(|| t.strip_prefix("0O")) {
        return u32::from_str_radix(oct, 8).ok();
    }
    if let Some(bin) = t.strip_prefix("0b").or_else(|| t.strip_prefix("0B")) {
        return u32::from_str_radix(bin, 2).ok();
    }
    t.parse::<u32>().ok()
}

fn strip_casts(text: &str) -> String {
    match text.find(" as ") {
        Some(pos) => text[..pos].trim_end().to_string(),
        None => text.to_string(),
    }
}

fn strip_numeric_suffix(text: &str) -> String {
    for suffix in [
        "_u8", "_u16", "_u32", "_u64", "_u128", "_usize", "_i8", "_i16", "_i32", "_i64",
        "_i128", "_isize", "_f32", "_f64", "u8", "u16", "u32", "u64", "u128", "usize", "i8",
        "i16", "i32", "i64", "i128", "isize", "f32", "f64",
    ] {
        if let Some(stripped) = text.strip_suffix(suffix) {
            return stripped.to_string();
        }
    }
    text.to_string()
}

fn unwrap_chr(text: &str) -> Option<&str> {
    for name in [
        "chr",
        "char::from_u32",
        "from_u32",
        "String::from_char",
        "char_from_u32",
    ] {
        if let Some(rest) = text.strip_prefix(name) {
            let rest = rest.trim_start();
            if let Some(inner) = rest.strip_prefix('(') {
                let inner = inner.trim_end();
                if let Some(inner) = inner.strip_suffix(')') {
                    return Some(inner.trim());
                }
            }
        }
    }
    None
}

/// Decode ``[0x6c, 0x69, ...]`` / ``['l', ...]`` array literals into strings.
fn decode_arrays(text: &str) -> Vec<String> {
    let mut decoded = Vec::new();
    for segment in text.split('[') {
        let body = match segment.find(']') {
            Some(index) => &segment[..index],
            None => continue,
        };
        let mut out = String::new();
        let mut valid = !body.trim().is_empty();
        for item in body.split(',') {
            let item = item.trim();
            // Blank element == trailing comma / blank line (``rustfmt``): skip
            // it rather than discarding the whole array.
            if item.is_empty() {
                continue;
            }
            // Decode the element with the same vocabulary as an incremental
            // write (literals, ``as u8`` casts, numeric suffixes).
            match literal_fragment(item) {
                Some(fragment) => out.push_str(&fragment),
                None => {
                    valid = false;
                    break;
                }
            }
        }
        if valid && !out.is_empty() {
            decoded.push(out);
        }
    }
    decoded
}

/// Decode a single element expression into the characters it writes.
fn literal_fragment(expr: &str) -> Option<String> {
    let mut text = expr.trim();
    while let Some(first) = text.chars().next() {
        if first == '&' || first == '*' {
            text = text[first.len_utf8()..].trim_start();
        } else {
            break;
        }
    }
    if let Some(rest) = text.strip_prefix("mut ") {
        text = rest.trim_start();
    }
    loop {
        if text.len() >= 2 && text.starts_with('(') && text.ends_with(')') {
            let inner = &text[1..text.len() - 1];
            if inner.matches('(').count() == inner.matches(')').count() {
                text = inner.trim();
                continue;
            }
        }
        break;
    }
    if text.is_empty() {
        return None;
    }
    // String / byte / raw / char literal.
    if let Some(fragment) = decode_string_literal(text) {
        return Some(fragment);
    }
    // A bracketed array argument (``extend(&[...])`` / ``vec![...]``).
    if text.contains('[') && text.contains(']') {
        let arrays = decode_arrays(text);
        if arrays.is_empty() {
            return None;
        }
        return Some(arrays.concat());
    }
    // ``chr(0x6e)`` / ``char::from_u32(110)`` wrapper.
    if let Some(inner) = unwrap_chr(text) {
        return literal_fragment(inner);
    }
    // Integer code point, tolerating ``as u8`` casts and ``_u8`` suffixes.
    let stripped = strip_numeric_suffix(&strip_casts(text));
    let stripped = stripped.trim();
    if let Some(code) = parse_int(stripped) {
        return char::from_u32(code).map(|c| c.to_string());
    }
    None
}

fn is_ident_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'$'
}

/// Walk back from ``end`` over an identifier / ``a.b`` / ``a::b`` receiver.
fn receiver_before(bytes: &[u8], mut end: usize) -> String {
    while end > 0 && (bytes[end - 1] == b' ' || bytes[end - 1] == b'\t') {
        end -= 1;
    }
    let mut start = end;
    loop {
        if start == 0 {
            break;
        }
        let c = bytes[start - 1];
        if is_ident_byte(c) || c == b'.' {
            start -= 1;
            continue;
        }
        if c == b':' && start >= 2 && bytes[start - 2] == b':' {
            start -= 2;
            continue;
        }
        break;
    }
    String::from_utf8_lossy(&bytes[start..end]).to_string()
}

/// Return the byte index of the ``)`` matching the ``(`` at ``open``.
fn match_paren(bytes: &[u8], open: usize) -> Option<usize> {
    let mut depth = 0i32;
    let mut i = open;
    while i < bytes.len() {
        match bytes[i] {
            b'(' => depth += 1,
            b')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(i);
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

const ELEMENT_METHODS: [&str; 15] = [
    "push_back",
    "push_str",
    "extend_from_slice",
    "WriteString",
    "WriteByte",
    "WriteRune",
    "write_all",
    "write_str",
    "push",
    "append",
    "extend",
    "write",
    "insert",
    "concat",
    "add",
];

/// Rebuild strings written element-by-element into a container.
///
/// Mirrors ``detectors/subprocess.py`` in the verifier: every literal element
/// written to the same receiver (``name.push(0x6c); name.push(0x69); ...``,
/// ``buf.extend([...])``, ``buf += ...``, ``buf[i] = 0x6c``) is decoded and the
/// fragments for that receiver are concatenated in source order, so the
/// artifact name no longer depends on a fixed list of spellings.
fn assembled_sequences(text: &str) -> Vec<String> {
    let bytes = text.as_bytes();
    let n = bytes.len();
    let mut containers: Vec<(String, String)> = Vec::new();
    let mut push_fragment = |receiver: String, fragment: String| {
        if receiver.is_empty() {
            return;
        }
        if let Some(entry) = containers.iter_mut().find(|(name, _)| *name == receiver) {
            entry.1.push_str(&fragment);
        } else {
            containers.push((receiver, fragment));
        }
    };

    let mut i = 0usize;
    while i < n {
        // ---- element-write method call: .method(args) ----
        let sep_len = if bytes[i] == b'.' {
            1
        } else if bytes[i] == b':' && i + 1 < n && bytes[i + 1] == b':' {
            2
        } else {
            0
        };
        if sep_len > 0 {
            let mut j = i + sep_len;
            while j < n && (bytes[j] == b' ' || bytes[j] == b'\t') {
                j += 1;
            }
            let mstart = j;
            while j < n && is_ident_byte(bytes[j]) {
                j += 1;
            }
            let method = &text[mstart..j];
            if j > mstart && ELEMENT_METHODS.contains(&method) {
                let mut k = j;
                while k < n && bytes[k].is_ascii_whitespace() {
                    k += 1;
                }
                if k < n && bytes[k] == b'(' {
                    if let Some(close) = match_paren(bytes, k) {
                        if let Some(fragment) = literal_fragment(&text[k + 1..close]) {
                            push_fragment(receiver_before(bytes, i), fragment);
                        }
                        i = close + 1;
                        continue;
                    }
                }
            }
            i += sep_len;
            continue;
        }
        // ---- plus-equals append: receiver += expr ----
        if bytes[i] == b'+' && i + 1 < n && bytes[i + 1] == b'=' {
            let receiver = receiver_before(bytes, i);
            let mut k = i + 2;
            while k < n && bytes[k] != b';' && bytes[k] != b'\n' && bytes[k] != b'}' {
                k += 1;
            }
            if let Some(fragment) = literal_fragment(&text[i + 2..k]) {
                push_fragment(receiver, fragment);
            }
            i = k + 1;
            continue;
        }
        // ---- index assignment: receiver[index] = expr ----
        if bytes[i] == b'=' {
            // skip '==', '=>', '!=' comparisons
            let prev = if i > 0 { bytes[i - 1] } else { 0 };
            if prev != b'!' && prev != b'<' && prev != b'>' && prev != b'=' {
                // walk left over spaces to ']'
                let mut close = i;
                while close > 0 && bytes[close - 1].is_ascii_whitespace() {
                    close -= 1;
                }
                if close > 0 && bytes[close - 1] == b']' {
                    if let Some(open) = text[..close - 1].rfind('[') {
                        let receiver = receiver_before(bytes, open);
                        let mut k = i + 1;
                        while k < n && bytes[k] != b';' && bytes[k] != b'\n' && bytes[k] != b'}' {
                            k += 1;
                        }
                        if let Some(fragment) = literal_fragment(&text[i + 1..k]) {
                            push_fragment(receiver, fragment);
                        }
                        i = k + 1;
                        continue;
                    }
                }
            }
        }
        i += 1;
    }
    containers
        .into_iter()
        .filter(|(_, value)| value.chars().count() >= 2)
        .map(|(_, value)| value)
        .collect()
}

/// True when ``text`` exposes a high-confidence source-runtime artifact.
///
/// The artifact name may be assembled at run time from a byte/char array
/// (``[0x6c, 0x69, ...]``), from concatenated string literals, or written
/// element by element into a container (``name.push(0x6c); ...``), so the raw
/// text alone is not enough.  This mirrors the verifier FFI detector.
fn artifact_present(content: &str) -> bool {
    const TOKENS: [&str; 5] = ["libnode", "node::", "v8::", "node_api", "napi_"];
    let text = strip_comments(content);
    let text = text.as_str();
    let mut forms: Vec<String> = Vec::new();
    let compact: String = text
        .chars()
        .filter(|character| !character.is_whitespace())
        .collect();
    forms.push(compact.to_lowercase());
    // Concatenation of every string literal in the file.
    let mut literals = String::new();
    let mut in_string = false;
    let mut quote = '\0';
    for character in text.chars() {
        if in_string {
            if character == quote {
                in_string = false;
            } else {
                literals.push(character);
            }
        } else if character == '"' || character == '\'' {
            in_string = true;
            quote = character;
        }
    }
    forms.push(literals.to_lowercase());
    // Decoded numeric / char arrays.
    for decoded in decode_arrays(text) {
        forms.push(decoded.to_lowercase());
    }
    // Rebuilt element-by-element container writes.
    for assembled in assembled_sequences(text) {
        forms.push(assembled.to_lowercase());
    }
    forms
        .iter()
        .any(|form| TOKENS.iter().any(|token| form.contains(token)))
}

/// Detect a subprocess/FFI bridge to the source runtime even when the author
/// obfuscates the sink.  Whitespace is removed so ``process_mod :: Command ::
/// new`` collapses to ``Command::new``, and the grouped ``use std::{process as
/// P}`` module alias is recognised explicitly.  A bare ``extern "C"`` block is
/// only a bridge when the crate exposes a source-runtime artifact (in this file
/// or, cross-file, in any other file: ``tree_artifact``).
fn delegation_marker(content: &str, tree_artifact: bool) -> Option<String> {
    let compact: String = content
        .chars()
        .filter(|character| !character.is_whitespace())
        .collect();
    for token in [
        "Command::new",
        "child_process",
        "std::process",
        "process::Command",
        "node_modules",
    ] {
        if compact.contains(token) {
            return Some(token.to_string());
        }
    }
    if compact.contains("usestd::processas") {
        return Some("process-module-alias".to_string());
    }
    if let Some(start) = compact.find("usestd::{") {
        if let Some(offset) = compact[start..].find('}') {
            let body = &compact[start + "usestd::{".len()..start + offset];
            if body
                .split(',')
                .any(|member| member.starts_with("processas"))
            {
                return Some("process-module-alias".to_string());
            }
        }
    }
    if compact.contains("extern\"C\"") && (tree_artifact || artifact_present(content)) {
        return Some("extern-c-source-runtime".to_string());
    }
    None
}

// ---------------------------------------------------------------------------
// inputs
// ---------------------------------------------------------------------------

const IO_PLAN: &str = r#"
     Update on a  (cost=0.00..266690.72 rows=0 width=0) (actual time=10.00..11.00 rows=0 loops=1)
         Buffers: shared hit=2 read=4 dirtied=6 written=8
         I/O Timings: read=2 write=4
         ->  Seq Scan on a  (cost=0.00..0.00 rows=10 width=1) (actual time=9.00..10.00 rows=10000000 loops=1)
               Buffers: shared hit=1 read=2 written=4
               I/O Timings: read=1 write=2
"#;

const VERBOSE_JSON: &str = r#"
[
  { "Plan": {
      "Node Type": "Aggregate",
      "Startup Cost": 38.51, "Total Cost": 38.52,
      "Plan Rows": 1, "Plan Width": 4,
      "Plans": [
        { "Node Type": "Seq Scan", "Parent Relationship": "InitPlan",
          "Subplan Name": "CTE e", "Relation Name": "employes",
          "Startup Cost": 0.00, "Total Cost": 38.25,
          "Plan Rows": 11, "Plan Width": 8,
          "Output": ["num_service"] },
        { "Node Type": "CTE Scan", "Parent Relationship": "Outer",
          "CTE Name": "e",
          "Startup Cost": 0.00, "Total Cost": 0.25,
          "Plan Rows": 4, "Plan Width": 4 }
      ] } }
]
"#;

const PGADMIN_JSON: &str = "\"[\n  {\n    \"\"Plan\"\": { \"\"Node Type\"\": \"\"Seq Scan\"\" }\n  }\n]\"";

// ---------------------------------------------------------------------------
// observation checks
// ---------------------------------------------------------------------------

fn check_cleanup_frame() -> Result<(), String> {
    let input = "│  Seq Scan on t  │\nQUERY PLAN\n(1 row)\n";
    let got = cleanup_source(input);
    let want = "  Seq Scan on t  \n\n";
    if got == want {
        Ok(())
    } else {
        Err(format!("cleanup_source returned {got:?}, expected {want:?}"))
    }
}

fn check_max_io() -> Result<(), String> {
    let content = content_of(IO_PLAN)?;
    let io = get_f64(&content, "maxIo").ok_or("maxIo missing")?;
    if approx(io, 3.0) {
        Ok(())
    } else {
        Err(format!("maxIo = {io}, expected 3.0"))
    }
}

fn check_hash_aggregate() -> Result<(), String> {
    let node = parse_node_type("HashAggregate");
    let node = serde_json::to_value(&node).map_err(|e| e.to_string())?;
    let node_type = get_str(&node, "Node Type").unwrap_or("");
    let partial = get_str(&node, "Partial Mode").unwrap_or("");
    let strategy = get_str(&node, "Strategy").unwrap_or("");
    let partitions = get_f64(&node, "Planned Partitions").unwrap_or(f64::NAN);
    if node_type == "Aggregate" && partial == "Simple" && strategy == "Hashed" && partitions == 0.0
    {
        Ok(())
    } else {
        Err(format!(
            "HashAggregate -> Node Type={node_type:?} Partial Mode={partial:?} Strategy={strategy:?} Planned Partitions={partitions}"
        ))
    }
}

fn check_index_scan() -> Result<(), String> {
    let node = parse_node_type("Index Scan using idx on t");
    let node = serde_json::to_value(&node).map_err(|e| e.to_string())?;
    let node_type = get_str(&node, "Node Type").unwrap_or("");
    let index = get_str(&node, "Index Name").unwrap_or("");
    let relation = get_str(&node, "Relation Name").unwrap_or("");
    let direction = get_str(&node, "Scan Direction").unwrap_or("");
    if node_type == "Index Scan" && index == "idx" && relation == "t" && direction == "Forward" {
        Ok(())
    } else {
        Err(format!(
            "Index Scan -> type={node_type:?} index={index:?} relation={relation:?} direction={direction:?}"
        ))
    }
}

fn check_dependency_policy() -> Result<(), String> {
    let dir = manifest_dir();
    let cargo = dir.join("Cargo.toml");
    let text = fs::read_to_string(&cargo).map_err(|e| format!("read Cargo.toml: {e}"))?;
    // The public contract types require `serde`/`serde_json`. The former list
    // also demanded the reference implementation's incidental crates
    // (`regex`, `fancy-regex`, `wasm-bindgen`, `serde-wasm-bindgen`), which the
    // instruction never requires; binding the gate to that set fails any
    // legitimate alternative dependency choice.
    for dep in ["serde", "serde_json"] {
        if !text.contains(dep) {
            return Err(format!("Cargo.toml missing dependency {dep}"));
        }
    }
    for forbidden in ["napi", "neon", "node", "quickjs", "deno"] {
        if text.contains(forbidden) {
            return Err(format!("Cargo.toml contains forbidden dependency {forbidden}"));
        }
    }
    if dir.join("build.rs").exists() {
        return Err("crate contains build.rs".to_string());
    }
    let mut sources = Vec::new();
    read_all_rs(&dir.join("src"), &mut sources);
    let tree_artifact = sources
        .iter()
        .any(|(_path, content)| artifact_present(content));
    for (path, content) in &sources {
        if let Some(token) = delegation_marker(content, tree_artifact) {
            return Err(format!("{} contains {token}", path.display()));
        }
    }
    Ok(())
}

fn check_no_node_delegation() -> Result<(), String> {
    // Behavioural half: parsing works without a Node.js runtime present.
    let content = from_source("Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)")
        .map_err(|e| format!("from_source: {e}"))?;
    let has_plan = content.contains_key("Plan");
    if !has_plan {
        return Err("from_source did not produce a Plan".to_string());
    }
    // Static half: no subprocess/FFI bridge to a TypeScript runtime.
    let mut sources = Vec::new();
    read_all_rs(&manifest_dir().join("src"), &mut sources);
    let tree_artifact = sources
        .iter()
        .any(|(_path, content)| artifact_present(content));
    for (path, content) in &sources {
        if let Some(token) = delegation_marker(content, tree_artifact) {
            return Err(format!("{} contains {token}", path.display()));
        }
    }
    Ok(())
}

fn check_split_balanced() -> Result<(), String> {
    let got = split_balanced("a,(b,c),d", ",");
    let want = vec!["a".to_string(), "(b,c)".to_string(), "d".to_string()];
    if got == want {
        Ok(())
    } else {
        Err(format!("split_balanced = {got:?}, expected {want:?}"))
    }
}

fn check_invalid_plan() -> Result<(), String> {
    match get_plan_content(&json!({"foo": 1})) {
        Err(e) if e == "Invalid plan" => Ok(()),
        other => Err(format!("get_plan_content = {other:?}, expected Err(\"Invalid plan\")")),
    }
}

fn check_from_source_invalid() -> Result<(), String> {
    match from_source("not a plan") {
        Ok(_) => Err("from_source(\"not a plan\") unexpectedly succeeded".to_string()),
        Err(_) => Ok(()),
    }
}

fn check_never_executed() -> Result<(), String> {
    let source = "Hash  (cost=6.12..6.12 rows=212 width=1346) (never executed)";
    let root = root_of(source)?;
    let loops = get_f64(&root, "Actual Loops").unwrap_or(f64::NAN);
    let rows = get_f64(&root, "Actual Rows").unwrap_or(f64::NAN);
    let total = get_f64(&root, "Actual Total Time").unwrap_or(f64::NAN);
    let startup = get_f64(&root, "Actual Startup Time").unwrap_or(f64::NAN);
    if loops == 0.0 && rows == 0.0 && total == 0.0 && startup == 0.0 {
        Ok(())
    } else {
        Err(format!(
            "never executed -> loops={loops} rows={rows} total={total} startup={startup}"
        ))
    }
}

fn check_fractional_rows() -> Result<(), String> {
    let source = r#"
 Limit  (cost=0.00..3.22 rows=1 width=4) (actual time=0.003..0.004 rows=0.25 loops=4)
   ->  Seq Scan on data  (cost=0.00..41.88 rows=13 width=4) (actual time=0.003..0.003 rows=0.25 loops=4)
"#;
    let root = root_of(source)?;
    let rows = get_f64(&root, "Actual Rows").unwrap_or(f64::NAN);
    let marker = root
        .get("*Actual Rows Is Fractional")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if approx(rows, 0.25) && marker {
        Ok(())
    } else {
        Err(format!(
            "fractional rows -> Actual Rows={rows} marker={marker}"
        ))
    }
}

fn check_max_estimate_factor() -> Result<(), String> {
    let analyzed =
        content_of("Seq Scan on foo  (cost=0.00..1.00 rows=10 width=4) (actual time=0.1..0.2 rows=100 loops=1)")?;
    let a = get_f64(&analyzed, "maxEstimateFactor").ok_or("maxEstimateFactor missing (analyzed)")?;
    let plain = content_of("Seq Scan on foo  (cost=0.00..1.00 rows=10 width=4)")?;
    let b = get_f64(&plain, "maxEstimateFactor").ok_or("maxEstimateFactor missing (plain)")?;
    if approx(a, 20.0) && approx(b, 1.0) {
        Ok(())
    } else {
        Err(format!("maxEstimateFactor analyzed={a} plain={b}"))
    }
}

fn check_from_json_double_quotes() -> Result<(), String> {
    let content = from_json(PGADMIN_JSON).map_err(|e| format!("from_json: {e}"))?;
    let root = content
        .get("Plan")
        .and_then(|p| p.as_object())
        .ok_or("no Plan")?;
    let node_type = root.get("Node Type").and_then(|v| v.as_str()).unwrap_or("");
    if node_type == "Seq Scan" {
        Ok(())
    } else {
        Err(format!("from_json Node Type = {node_type:?}"))
    }
}

fn check_verbose_cte() -> Result<(), String> {
    let plan = plan_of(VERBOSE_JSON)?;
    let verbose = plan.get("isVerbose").and_then(|v| v.as_bool()).unwrap_or(false);
    let ctes_len = plan
        .get("ctes")
        .and_then(|c| c.as_array())
        .map(|a| a.len())
        .unwrap_or(0);
    let root_plans_has_output = plan
        .get("content")
        .and_then(|c| c.get("Plan"))
        .and_then(|p| p.get("Plans"))
        .and_then(|p| p.as_array())
        .map(|children| children.iter().any(|c| c.get("Output").is_some()))
        .unwrap_or(true);
    if verbose && ctes_len == 1 && !root_plans_has_output {
        Ok(())
    } else {
        Err(format!(
            "verbose={verbose} ctes={ctes_len} root_plans_has_output={root_plans_has_output}"
        ))
    }
}

fn check_node_id_and_ctes() -> Result<(), String> {
    let plan = plan_of(VERBOSE_JSON)?;
    let root_id = plan
        .get("content")
        .and_then(|c| c.get("Plan"))
        .and_then(|p| get_i64(p, "nodeId"));
    let has_root = find_node_by_id(&plan.as_object().unwrap().clone(), 1).is_some();
    let has_cte = find_node_by_subplan_name(&plan.as_object().unwrap().clone(), "e").is_some();
    if root_id == Some(1) && has_root && has_cte {
        Ok(())
    } else {
        Err(format!(
            "root nodeId={root_id:?} has_root={has_root} has_cte={has_cte}"
        ))
    }
}

fn check_index_scan_and_policy() -> Result<(), String> {
    check_index_scan()?;
    check_dependency_policy()
}

fn check_routing_and_helpers() -> Result<(), String> {
    // pure JSON document routed directly
    let source = "{\"Plan\": {\"Node Type\": \"Seq Scan\"}}";
    let content = from_source(source).map_err(|e| format!("from_source json: {e}"))?;
    let ok_json = content
        .get("Plan")
        .and_then(|p| get_str(p, "Node Type"))
        .map(|t| t == "Seq Scan")
        .unwrap_or(false);
    // text routed to from_text
    let text = from_text("Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)")
        .map_err(|e| format!("from_text: {e}"))?;
    let ok_text = text
        .get("Plan")
        .and_then(|p| get_str(p, "Node Type"))
        .map(|t| t == "Seq Scan")
        .unwrap_or(false);
    let lines = split_into_lines("Seq Scan on t\n  Filter: (a = 1)");
    let ok_lines = !lines.is_empty();
    let show = should_show_prop("Filter", &json!("(a = 1)"))
        && !should_show_prop("Node Type", &json!("Seq Scan"));
    if ok_json && ok_text && ok_lines && show {
        Ok(())
    } else {
        Err(format!(
            "routing json={ok_json} text={ok_text} lines={ok_lines} show={show}"
        ))
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

fn main() {
    type Check = fn() -> Result<(), String>;
    let checks: [(&str, Check); 24] = [
        ("F2P-025", check_node_id_and_ctes),
        ("F2P-026", check_invalid_plan),
        ("F2P-027", check_verbose_cte),
        ("F2P-028", check_max_io),
        ("F2P-029", check_fractional_rows),
        ("F2P-030", check_cleanup_frame),
        ("F2P-031", check_verbose_cte),
        ("F2P-032", check_from_json_double_quotes),
        ("F2P-033", check_max_estimate_factor),
        ("F2P-034", check_fractional_rows),
        ("F2P-035", check_never_executed),
        ("F2P-036", check_invalid_plan),
        ("F2P-037", check_routing_and_helpers),
        ("F2P-038", check_cleanup_frame),
        ("F2P-039", check_never_executed),
        ("F2P-040", check_index_scan_and_policy),
        ("F2P-041", check_hash_aggregate),
        ("F2P-042", check_no_node_delegation),
        ("F2P-043", check_from_json_double_quotes),
        ("F2P-044", check_cleanup_frame),
        ("F2P-045", check_from_source_invalid),
        ("F2P-046", check_never_executed),
        ("F2P-047", check_split_balanced),
        ("F2P-048", check_fractional_rows),
    ];

    let mut results: BTreeMap<String, Value> = BTreeMap::new();
    for (id, check) in checks {
        let value = match check() {
            Ok(()) => json!({"ok": true, "detail": "pass"}),
            Err(e) => json!({"ok": false, "detail": e}),
        };
        results.insert(id.to_string(), value);
    }

    println!("{}", serde_json::to_string(&results).unwrap());
}
