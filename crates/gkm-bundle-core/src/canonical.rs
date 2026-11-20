//! The canonical form of a JSON value, and the digests of the record-identical round trip (ADR 0008 decisions
//! 1.2, 1.3). docs/round-trip.md defines them and this module implements that page: RFC 8785 (JCS) with every array
//! sorted by the UTF-8 bytes of its elements' canonical forms.
//!
//! A text has no canonical form when it isn't UTF-8 or isn't JSON, when an object names a member twice, when it
//! holds NaN, Infinity or a number outside the double range, or a string that isn't Unicode text (a lone
//! surrogate). Its error then says why, in the words the Python implementation used before this one (ADR 0010).
//! The parser is written here, not taken from a JSON library, so that each of those rulings is this page's.

use sha2::{Digest, Sha256};
use std::cmp::Ordering;
use std::fmt;

/// The sha256 of no bytes: the records sha256 of a section with no records.
pub const EMPTY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
/// The root's line in the bundle's records sha256.
pub const ROOT_LINE: &str = "$";
/// `record_sha256s` marks a text with no canonical form with this, then the reason; a sha256 never starts with it.
pub const FAIL: &str = "!";
/// Arrays and objects nested deeper than this have no canonical form here (no record comes near it).
pub const MAX_DEPTH: usize = 1000;

const EXACT: f64 = 9_007_199_254_740_992.0; // 2^53: integers up to this magnitude are their own double
const MIN_PER_THREAD: usize = 256; // texts per thread below which `record_sha256s` uses fewer threads
const HEX: &[u8; 16] = b"0123456789abcdef";
const NOT_UNICODE: &str = "a string isn't Unicode text (surrogates not allowed)";

/// Why a value has no canonical form.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanonicalError(pub String);

impl fmt::Display for CanonicalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for CanonicalError {}

pub type Result<T> = std::result::Result<T, CanonicalError>;

fn err<T>(msg: impl Into<String>) -> Result<T> {
    Err(CanonicalError(msg.into()))
}

// --- one value ---------------------------------------------------------------------------------------------------

/// The canonical form of a JSON text given as bytes, which must be UTF-8.
pub fn canonical_text(text: &[u8]) -> Result<String> {
    match std::str::from_utf8(text) {
        Ok(s) => canonical_str(s),
        Err(e) => err(format!("not UTF-8: {e}")),
    }
}

/// The canonical form of a JSON text.
pub fn canonical_str(text: &str) -> Result<String> {
    let mut p = Parser {
        s: text.as_bytes(),
        i: 0,
        depth: 0,
        lone_surrogate: false,
    };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return p.not_json("extra data");
    }
    if p.lone_surrogate {
        return err(NOT_UNICODE);
    }
    Ok(v)
}

/// The sha256 of a JSON text's canonical form, in lower-case hex.
pub fn record_sha256(text: &[u8]) -> Result<String> {
    Ok(sha256_hex(canonical_text(text)?.as_bytes()))
}

/// A string in canonical form, with its quotes.
pub fn string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    write_string(&mut out, s);
    out
}

/// A record's line in its section's records sha256 (without the LF).
pub fn key_line(key: &str, sha: &str) -> String {
    format!("{}\t{sha}", string(key))
}

/// The root sha256 of a bundle whose root members are `(name, JSON text)`, a section `(name, None)`.
pub fn root_sha256(members: &[(&str, Option<&[u8]>)]) -> Result<String> {
    let mut entries: Vec<(&str, String)> = Vec::with_capacity(members.len());
    for &(name, text) in members {
        if entries.iter().any(|(n, _)| *n == name) {
            return err(format!("the root names {} twice", py_repr(name)));
        }
        let value = match text {
            None => "{}".to_string(),
            Some(t) => canonical_text(t)?,
        };
        entries.push((name, value));
    }
    entries.sort_by(|a, b| utf16_cmp(a.0, b.0));
    let mut out = String::from("{");
    for (n, (name, value)) in entries.iter().enumerate() {
        if n > 0 {
            out.push(',');
        }
        write_string(&mut out, name);
        out.push(':');
        out.push_str(value);
    }
    out.push('}');
    Ok(sha256_hex(out.as_bytes()))
}

/// The bundle's records sha256 from each section's records sha256 (a section with none, EMPTY_SHA256, has no
/// line) and the root sha256.
pub fn records_sha256(sections: &[(&str, &str)], root: &str) -> String {
    let mut lines: Vec<String> = sections
        .iter()
        .filter(|(_, sha)| *sha != EMPTY_SHA256)
        .map(|(section, sha)| format!("{}\t{sha}\n", string(section)))
        .collect();
    lines.push(format!("{ROOT_LINE}\t{root}\n"));
    lines.sort();
    sha256_hex(lines.concat().as_bytes())
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut out = String::with_capacity(64);
    for b in digest.iter() {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 15) as usize] as char);
    }
    out
}

// --- many values -------------------------------------------------------------------------------------------------

/// Each text's record sha256, or FAIL and the reason, in order. Up to `threads` threads share the work.
pub fn record_sha256s<T: AsRef<[u8]> + Sync>(texts: &[T], threads: usize) -> Vec<String> {
    let marked = |t: &T| record_sha256(t.as_ref()).unwrap_or_else(|e| format!("{FAIL}{e}"));
    let threads = threads.min(texts.len().div_ceil(MIN_PER_THREAD)).max(1);
    if threads == 1 {
        return texts.iter().map(marked).collect();
    }
    let chunk = texts.len().div_ceil(threads);
    std::thread::scope(|scope| {
        let parts: Vec<_> = texts
            .chunks(chunk)
            .map(|part| scope.spawn(move || part.iter().map(marked).collect::<Vec<_>>()))
            .collect();
        parts
            .into_iter()
            .flat_map(|h| h.join().expect("a hashing thread panicked"))
            .collect()
    })
}

// --- writing -----------------------------------------------------------------------------------------------------

/// RFC 8785 3.2.2.2: `"` and `\` escaped, U+0008, U+0009, U+000A, U+000C and U+000D as `\b \t \n \f \r`, any other
/// U+0000 to U+001F as `\u00xx` (lower-case hex), everything else as itself.
fn write_string(out: &mut String, s: &str) {
    out.push('"');
    let mut start = 0;
    for (i, c) in s.bytes().enumerate() {
        if c >= 0x20 && c != b'"' && c != b'\\' {
            continue;
        }
        out.push_str(&s[start..i]);
        match c {
            b'"' => out.push_str("\\\""),
            b'\\' => out.push_str("\\\\"),
            0x08 => out.push_str("\\b"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            0x0c => out.push_str("\\f"),
            b'\r' => out.push_str("\\r"),
            _ => {
                out.push_str("\\u00");
                out.push(HEX[(c >> 4) as usize] as char);
                out.push(HEX[(c & 15) as usize] as char);
            }
        }
        start = i + 1;
    }
    out.push_str(&s[start..]);
    out.push('"');
}

/// Object member names compare as UTF-16 code units (RFC 8785 3.2.3).
fn utf16_cmp(a: &str, b: &str) -> Ordering {
    a.encode_utf16().cmp(b.encode_utf16())
}

/// A double as RFC 8785 3.2.2.3 writes it: an integer up to 2^53 in magnitude as its digits, any other value in
/// ECMAScript's Number::toString form. `-0` is `0`.
fn double(x: f64) -> String {
    if x.fract() == 0.0 && x.abs() <= EXACT {
        return (x as i64).to_string();
    }
    // The shortest digits that read back as x and, of those, the closest to x, as ECMAScript requires. Ryu gives
    // them; std's `{:e}` gives a shortest form that isn't always the closest (32939954535045.562 as ...563). Then
    // where the decimal point goes: x = 0.<digits> * 10^n.
    let mut buffer = ryu::Buffer::new();
    let shortest = buffer.format_finite(x.abs());
    let (mantissa, exp) = shortest.split_once(['e', 'E']).unwrap_or((shortest, "0"));
    let (int_part, frac_part) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let all = format!("{int_part}{frac_part}");
    let digits = all.trim_start_matches('0');
    let mut n = int_part.len() as i32 + exp.parse::<i32>().expect("ryu writes an integer exponent");
    n -= (all.len() - digits.len()) as i32;
    let digits = digits.trim_end_matches('0');
    let k = digits.len() as i32;
    let mut out = String::with_capacity(26);
    if x < 0.0 {
        out.push('-');
    }
    if k <= n && n <= 21 {
        out.push_str(digits);
        out.extend(std::iter::repeat_n('0', (n - k) as usize));
    } else if 0 < n && n <= 21 {
        out.push_str(&digits[..n as usize]);
        out.push('.');
        out.push_str(&digits[n as usize..]);
    } else if -6 < n && n <= 0 {
        out.push_str("0.");
        out.extend(std::iter::repeat_n('0', (-n) as usize));
        out.push_str(digits);
    } else {
        let e = n - 1;
        out.push_str(&digits[..1]);
        if k > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        out.push(if e >= 0 { '+' } else { '-' });
        out.push_str(&e.abs().to_string());
    }
    out
}

/// A string as Python's repr writes it, for error messages.
fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

// --- reading -----------------------------------------------------------------------------------------------------

/// A JSON reader that writes each value's canonical form as it reads it. Its grammar is RFC 8259's, as Python's
/// `json` module reads it: whitespace is space, TAB, LF and CR; a raw control character in a string is refused.
struct Parser<'a> {
    s: &'a [u8],
    i: usize,
    depth: usize,
    /// A `\u` escape left half a surrogate pair. Python read such a string and refused it only when hashing, after
    /// any other error in the text; this reader keeps that order.
    lone_surrogate: bool,
}

impl Parser<'_> {
    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }

    fn at(&self, literal: &[u8]) -> bool {
        self.s[self.i..].starts_with(literal)
    }

    fn ws(&mut self) {
        while let Some(b' ' | b'\t' | b'\n' | b'\r') = self.peek() {
            self.i += 1;
        }
    }

    fn not_json<T>(&self, what: &str) -> Result<T> {
        let char_at = self.s[..self.i].iter().filter(|&&b| b & 0xc0 != 0x80).count();
        err(format!("not JSON: {what} at char {char_at}"))
    }

    fn value(&mut self) -> Result<String> {
        match self.peek() {
            Some(b'{') => self.nested(Self::object),
            Some(b'[') => self.nested(Self::array),
            Some(b'"') => Ok(string(&self.string()?)),
            Some(b'-') if self.at(b"-Infinity") => err("-Infinity is not a JSON number"),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(b'N') if self.at(b"NaN") => err("NaN is not a JSON number"),
            Some(b'I') if self.at(b"Infinity") => err("Infinity is not a JSON number"),
            Some(b't') if self.at(b"true") => self.literal("true"),
            Some(b'f') if self.at(b"false") => self.literal("false"),
            Some(b'n') if self.at(b"null") => self.literal("null"),
            _ => self.not_json("expecting value"),
        }
    }

    fn literal(&mut self, word: &str) -> Result<String> {
        self.i += word.len();
        Ok(word.to_string())
    }

    fn nested(&mut self, read: fn(&mut Self) -> Result<String>) -> Result<String> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return self.not_json(&format!("nested deeper than {MAX_DEPTH} levels"));
        }
        let v = read(self);
        self.depth -= 1;
        v
    }

    fn object(&mut self) -> Result<String> {
        self.i += 1;
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            return Ok("{}".to_string());
        }
        let mut members: Vec<(String, String)> = Vec::new();
        loop {
            if self.peek() != Some(b'"') {
                return self.not_json("expecting property name enclosed in double quotes");
            }
            let name = self.string()?;
            self.ws();
            if self.peek() != Some(b':') {
                return self.not_json("expecting ':' delimiter");
            }
            self.i += 1;
            self.ws();
            let value = self.value()?;
            members.push((name, value));
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.i += 1;
                    self.ws();
                }
                Some(b'}') => {
                    self.i += 1;
                    break;
                }
                _ => return self.not_json("expecting ',' delimiter"),
            }
        }
        members.sort_by(|a, b| utf16_cmp(&a.0, &b.0));
        if members.windows(2).any(|w| w[0].0 == w[1].0) {
            let mut twice: Vec<&str> = members
                .windows(2)
                .filter(|w| w[0].0 == w[1].0)
                .map(|w| w[0].0.as_str())
                .collect();
            twice.sort();
            twice.dedup();
            let names: Vec<String> = twice.iter().map(|n| py_repr(n)).collect();
            return err(format!("an object names [{}] more than once", names.join(", ")));
        }
        let mut out = String::with_capacity(members.iter().map(|(k, v)| k.len() + v.len() + 4).sum::<usize>() + 2);
        out.push('{');
        for (n, (name, value)) in members.iter().enumerate() {
            if n > 0 {
                out.push(',');
            }
            write_string(&mut out, name);
            out.push(':');
            out.push_str(value);
        }
        out.push('}');
        Ok(out)
    }

    fn array(&mut self) -> Result<String> {
        self.i += 1;
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            return Ok("[]".to_string());
        }
        let mut parts: Vec<String> = Vec::new();
        loop {
            parts.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.i += 1;
                    self.ws();
                }
                Some(b']') => {
                    self.i += 1;
                    break;
                }
                _ => return self.not_json("expecting ',' delimiter"),
            }
        }
        parts.sort_unstable(); // by UTF-8 bytes; equal elements are equal text, so stability doesn't matter
        let mut out = String::with_capacity(parts.iter().map(|p| p.len() + 1).sum::<usize>() + 1);
        out.push('[');
        for (n, part) in parts.iter().enumerate() {
            if n > 0 {
                out.push(',');
            }
            out.push_str(part);
        }
        out.push(']');
        Ok(out)
    }

    /// A string's decoded text. The input is UTF-8, so the text between escapes is copied as it is.
    fn string(&mut self) -> Result<String> {
        let open = self.i;
        self.i += 1;
        let mut out = String::new();
        let mut run = self.i;
        loop {
            let Some(c) = self.peek() else {
                self.i = open;
                return self.not_json("unterminated string starting");
            };
            match c {
                b'"' => {
                    out.push_str(self.text(run, self.i));
                    self.i += 1;
                    return Ok(out);
                }
                b'\\' => {
                    out.push_str(self.text(run, self.i));
                    self.escape(&mut out, open)?;
                    run = self.i;
                }
                0..=0x1f => return self.not_json("invalid control character"),
                _ => self.i += 1,
            }
        }
    }

    fn text(&self, from: usize, to: usize) -> &str {
        std::str::from_utf8(&self.s[from..to]).expect("the input is UTF-8 and a run ends before ASCII")
    }

    fn escape(&mut self, out: &mut String, open: usize) -> Result<()> {
        let at = self.i;
        let Some(e) = self.s.get(self.i + 1).copied() else {
            self.i = open;
            return self.not_json("unterminated string starting");
        };
        self.i += 2;
        let c = match e {
            b'"' => '"',
            b'\\' => '\\',
            b'/' => '/',
            b'b' => '\u{8}',
            b'f' => '\u{c}',
            b'n' => '\n',
            b'r' => '\r',
            b't' => '\t',
            b'u' => {
                let unit = self.hex4(at)?;
                match unit {
                    0xd800..=0xdbff if self.at(b"\\u") => {
                        let back = self.i;
                        self.i += 2;
                        let low = self.hex4(back)?;
                        if (0xdc00..=0xdfff).contains(&low) {
                            char::from_u32(0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00)).expect("a pair")
                        } else {
                            self.i = back; // read the second escape on its own
                            self.surrogate()
                        }
                    }
                    0xd800..=0xdfff => self.surrogate(),
                    _ => char::from_u32(unit).expect("not a surrogate"),
                }
            }
            _ => {
                self.i = at;
                return self.not_json("invalid \\escape");
            }
        };
        out.push(c);
        Ok(())
    }

    fn surrogate(&mut self) -> char {
        self.lone_surrogate = true;
        '\u{fffd}'
    }

    fn hex4(&mut self, at: usize) -> Result<u32> {
        let digits = self.s.get(self.i..self.i + 4).and_then(|h| std::str::from_utf8(h).ok());
        match digits.filter(|h| h.bytes().all(|b| b.is_ascii_hexdigit())) {
            Some(h) => {
                self.i += 4;
                Ok(u32::from_str_radix(h, 16).expect("four hex digits"))
            }
            None => {
                self.i = at;
                self.not_json("invalid \\uXXXX escape")
            }
        }
    }

    /// A number by its value: `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?`. An integer up to 2^53 in magnitude is
    /// exact; any other number counts as its nearest double, correctly rounded.
    fn number(&mut self) -> Result<String> {
        let start = self.i;
        let digit = |p: &Self, j: usize| matches!(p.s.get(j), Some(b'0'..=b'9'));
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        match self.peek() {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => {
                while digit(self, self.i) {
                    self.i += 1;
                }
            }
            _ => {
                self.i = start;
                return self.not_json("expecting value");
            }
        }
        let int_digits = self.i - start - usize::from(self.s[start] == b'-');
        let mut integer = true;
        if self.peek() == Some(b'.') && digit(self, self.i + 1) {
            self.i += 1;
            while digit(self, self.i) {
                self.i += 1;
            }
            integer = false;
        }
        if let Some(b'e' | b'E') = self.peek() {
            let mut j = self.i + 1;
            if let Some(b'+' | b'-') = self.s.get(j) {
                j += 1;
            }
            if digit(self, j) {
                self.i = j;
                while digit(self, self.i) {
                    self.i += 1;
                }
                integer = false;
            }
        }
        let literal = self.text(start, self.i);
        if integer && int_digits <= 16 {
            let v: i64 = literal.parse().expect("at most 16 digits");
            if v.unsigned_abs() <= 1 << 53 {
                return Ok(v.to_string());
            }
        }
        let x: f64 = literal.parse().expect("the JSON number grammar is a subset of Rust's");
        if !x.is_finite() {
            return if integer {
                err(format!("the number {literal} is outside the IEEE-754 double range"))
            } else {
                err("a number outside the IEEE-754 double range")
            };
        }
        Ok(double(x))
    }
}
