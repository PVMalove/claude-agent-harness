//! The canonical JSON of the file ledger, written by Rust.
//!
//! Python writes every record with `json.dumps(value, ensure_ascii=False, indent=2,
//! sort_keys=True) + "\n"` (`_canonical` in `harness/orchestration/ledger/lifecycle.py`), and the
//! `record_sha256`, `brief_sha256` and `report_sha256` of a record hash that text. This module
//! writes the same bytes, so a hash computed in either language holds in the other. The golden
//! fixture in `tests/fixtures` is checked by both languages.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// The canonical text of `value`, with the final line feed.
pub fn canonical_json(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, 0);
    out.push('\n');
    out
}

fn indent(out: &mut String, depth: usize) {
    out.push('\n');
    for _ in 0..depth {
        out.push_str("  ");
    }
}

fn write_value(out: &mut String, value: &Value, depth: usize) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => out.push_str(&python_number(&number.to_string())),
        Value::String(text) => write_string(out, text),
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                indent(out, depth + 1);
                write_value(out, item, depth + 1);
            }
            indent(out, depth);
            out.push(']');
        }
        Value::Object(members) if members.is_empty() => out.push_str("{}"),
        Value::Object(members) => {
            // Python sorts keys by code point, which is the byte order of UTF-8.
            let mut keys: Vec<&String> = members.keys().collect();
            keys.sort();
            out.push('{');
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                indent(out, depth + 1);
                write_string(out, key);
                out.push_str(": ");
                write_value(out, &members[key], depth + 1);
            }
            indent(out, depth);
            out.push('}');
        }
    }
}

/// A string as `json.dumps(..., ensure_ascii=False)` writes it: only `"`, `\` and the control
/// characters below U+0020 are escaped. U+007F and everything non-ASCII stay as they are.
fn write_string(out: &mut String, text: &str) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            control if control < ' ' => out.push_str(&format!("\\u{:04x}", u32::from(control))),
            other => out.push(other),
        }
    }
    out.push('"');
}

/// A JSON number as Python writes the value it parses to. An integer keeps its digits whatever
/// its size; a float is written as `repr(float)` does.
fn python_number(literal: &str) -> String {
    if !literal.contains(['.', 'e', 'E']) {
        return if literal == "-0" {
            "0".to_owned()
        } else {
            literal.to_owned()
        };
    }
    match literal.parse::<f64>() {
        Ok(float) if float.is_infinite() => {
            if float < 0.0 { "-Infinity" } else { "Infinity" }.to_owned()
        }
        Ok(float) => python_float(float),
        Err(_) => literal.to_owned(),
    }
}

/// `repr(float)` of CPython: the shortest digits that read back as the same float, in positional
/// notation for decimal exponents from -4 to 15 and in exponent notation (`1e-05`, `1e+16`)
/// outside that range.
fn python_float(float: f64) -> String {
    let sign = if float.is_sign_negative() { "-" } else { "" };
    if float == 0.0 {
        return format!("{sign}0.0");
    }
    // `{:e}` gives the shortest round-trip digits as `d.ddde<exp>`.
    let scientific = format!("{:e}", float.abs());
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let body = if !(-4..16).contains(&exponent) {
        let rest = &digits[1..];
        let fraction = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let exponent_sign = if exponent < 0 { '-' } else { '+' };
        format!(
            "{}{fraction}e{exponent_sign}{:02}",
            &digits[..1],
            exponent.abs()
        )
    } else if exponent >= 0 {
        let integer_len = usize::try_from(exponent).unwrap_or(0) + 1;
        if digits.len() <= integer_len {
            format!("{digits}{}.0", "0".repeat(integer_len - digits.len()))
        } else {
            format!("{}.{}", &digits[..integer_len], &digits[integer_len..])
        }
    } else {
        format!(
            "0.{}{digits}",
            "0".repeat(usize::try_from(-exponent - 1).unwrap_or(0))
        )
    };
    format!("{sign}{body}")
}

/// The lower-case hex SHA-256 of `bytes`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// The SHA-256 of the canonical JSON of a record: the hash a ledger writes as `record_sha256`,
/// `brief_sha256` or `report_sha256`.
pub fn object_sha256(record: &Map<String, Value>) -> String {
    sha256_hex(canonical_json(&Value::Object(record.clone())).as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    const GOLDEN_INPUT: &str = include_str!("../tests/fixtures/canonical_golden.json");
    const GOLDEN_EXPECTED: &str = include_str!("../tests/fixtures/canonical_golden.expected");

    #[test]
    fn the_golden_fixture_is_written_byte_for_byte_as_python_writes_it() {
        let value: Value = serde_json::from_str(GOLDEN_INPUT).unwrap();

        assert_eq!(canonical_json(&value), GOLDEN_EXPECTED);
    }
}
