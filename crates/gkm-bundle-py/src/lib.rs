//! The Python bindings of the Rust core (ADR 0010): the extension module `gkm_bundle_parquet._core`, which
//! `canonical.py` names. A JSON text is a `str` or a bytes-like object; a `str` that isn't Unicode text (a lone
//! surrogate) has no canonical form.

use gkm_bundle_core::canonical as core;
use pyo3::create_exception;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::{PyBackedBytes, PyBackedStr};
use pyo3::types::{PyDict, PyString};

create_exception!(
    _core,
    CanonicalError,
    PyValueError,
    "A value has no canonical form: a name written twice, NaN or Infinity, a number outside the double range, text \
     that isn't Unicode, or text that isn't JSON."
);

const NOT_UNICODE: &str = "a string isn't Unicode text (surrogates not allowed)";

fn refused(e: core::CanonicalError) -> PyErr {
    CanonicalError::new_err(e.0)
}

/// A JSON text held without copying: `str` as UTF-8, or bytes. `NotUnicode` is a `str` with a lone surrogate.
enum Text {
    Str(PyBackedStr),
    Bytes(PyBackedBytes),
    NotUnicode,
}

impl Text {
    fn of(obj: &Bound<'_, PyAny>) -> PyResult<Text> {
        if obj.is_instance_of::<PyString>() {
            return Ok(obj.extract::<PyBackedStr>().map(Text::Str).unwrap_or(Text::NotUnicode));
        }
        match obj.extract::<PyBackedBytes>() {
            Ok(b) => Ok(Text::Bytes(b)),
            Err(_) => Err(PyTypeError::new_err(format!(
                "a JSON text is str or bytes, not {}",
                obj.get_type().name()?
            ))),
        }
    }

    fn bytes(&self) -> Option<&[u8]> {
        match self {
            Text::Str(s) => Some(s.as_bytes()),
            Text::Bytes(b) => Some(b),
            Text::NotUnicode => None,
        }
    }
}

fn str_of<'a>(s: &'a Bound<'_, PyString>) -> PyResult<std::borrow::Cow<'a, str>> {
    s.to_cow().map_err(|_| CanonicalError::new_err(NOT_UNICODE))
}

/// The canonical form of a JSON text.
#[pyfunction]
fn canonical_text(text: &Bound<'_, PyAny>) -> PyResult<String> {
    match Text::of(text)?.bytes() {
        Some(b) => core::canonical_text(b).map_err(refused),
        None => Err(CanonicalError::new_err(NOT_UNICODE)),
    }
}

/// The sha256 of a JSON text's canonical form.
#[pyfunction]
fn record_sha256(text: &Bound<'_, PyAny>) -> PyResult<String> {
    match Text::of(text)?.bytes() {
        Some(b) => core::record_sha256(b).map_err(refused),
        None => Err(CanonicalError::new_err(NOT_UNICODE)),
    }
}

/// Each text's record sha256, or FAIL and the reason, in order. The texts are hashed with the GIL released, on up
/// to `workers` threads.
#[pyfunction]
fn record_sha256s(py: Python<'_>, texts: Vec<Bound<'_, PyAny>>, workers: usize) -> PyResult<Vec<String>> {
    let held = texts.iter().map(Text::of).collect::<PyResult<Vec<_>>>()?;
    let bytes: Vec<&[u8]> = held.iter().map(|t| t.bytes().unwrap_or_default()).collect();
    let mut out = py.detach(|| core::record_sha256s(&bytes, workers));
    for (i, t) in held.iter().enumerate() {
        if t.bytes().is_none() {
            out[i] = format!("{}{NOT_UNICODE}", core::FAIL);
        }
    }
    Ok(out)
}

/// A string in canonical form, with its quotes.
#[pyfunction]
fn string(s: &Bound<'_, PyString>) -> PyResult<String> {
    Ok(core::string(&str_of(s)?))
}

/// A record's line in its section's records sha256 (without the LF).
#[pyfunction]
fn key_line(key: &Bound<'_, PyString>, sha: &str) -> PyResult<String> {
    Ok(core::key_line(&str_of(key)?, sha))
}

/// The root sha256 of a bundle whose root members are `(name, JSON text)`, a section `(name, None)`.
#[pyfunction]
fn root_sha256(members: &Bound<'_, PyAny>) -> PyResult<String> {
    let mut held: Vec<(Bound<'_, PyString>, Option<Text>)> = Vec::new();
    for item in members.try_iter()? {
        let (name, text): (Bound<'_, PyString>, Option<Bound<'_, PyAny>>) = item?.extract()?;
        held.push((name, text.as_ref().map(Text::of).transpose()?));
    }
    let names = held.iter().map(|(n, _)| str_of(n)).collect::<PyResult<Vec<_>>>()?;
    let mut parts: Vec<(&str, Option<&[u8]>)> = Vec::with_capacity(held.len());
    for ((_, text), name) in held.iter().zip(&names) {
        let bytes = match text {
            None => None,
            Some(t) => Some(t.bytes().ok_or_else(|| CanonicalError::new_err(NOT_UNICODE))?),
        };
        parts.push((name, bytes));
    }
    core::root_sha256(&parts).map_err(refused)
}

/// The bundle's records sha256 from each section's records sha256 and the root sha256.
#[pyfunction]
fn records_sha256(sections: &Bound<'_, PyDict>, root: &str) -> PyResult<String> {
    let mut held: Vec<(String, String)> = Vec::with_capacity(sections.len());
    for (section, sha) in sections.iter() {
        held.push((str_of(section.cast::<PyString>()?)?.into_owned(), sha.extract()?));
    }
    let pairs: Vec<(&str, &str)> = held.iter().map(|(s, h)| (s.as_str(), h.as_str())).collect();
    Ok(core::records_sha256(&pairs, root))
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("CanonicalError", m.py().get_type::<CanonicalError>())?;
    m.add("EMPTY_SHA256", core::EMPTY_SHA256)?;
    m.add("ROOT_LINE", core::ROOT_LINE)?;
    m.add("FAIL", core::FAIL)?;
    m.add_function(wrap_pyfunction!(canonical_text, m)?)?;
    m.add_function(wrap_pyfunction!(record_sha256, m)?)?;
    m.add_function(wrap_pyfunction!(record_sha256s, m)?)?;
    m.add_function(wrap_pyfunction!(string, m)?)?;
    m.add_function(wrap_pyfunction!(key_line, m)?)?;
    m.add_function(wrap_pyfunction!(root_sha256, m)?)?;
    m.add_function(wrap_pyfunction!(records_sha256, m)?)?;
    Ok(())
}
