"""sources: one input bundle, its checksum (--sha256, else a `<file>.sha256` sidecar), and HTTP streaming.

No network: HTTP goes through a fake `urlopen`. The URLs and sha256s are synthetic.
"""

from __future__ import annotations

import email.message
import urllib.error

import pytest
from _input_helpers import FIXTURE, local_input, sha256_of

from gkm_bundle_parquet import transport
from gkm_bundle_parquet.errors import InputError
from gkm_bundle_parquet.model import InputFile
from gkm_bundle_parquet.sources import parse_sidecar, resolve_input
from gkm_bundle_parquet.transport import USER_AGENT, open_stream

BASE = "https://data.example.org"
L = "v1/"


def _sha(c: str) -> str:
    return c * 64


# ---------------------------------------------------------------------------------------------------------------
# Fake HTTP


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None, fail_after: int | None = None):
        self._body, self.status, self._pos, self._fail_after = body, status, 0, fail_after
        self.headers = email.message.Message()
        for k, v in {"Content-Length": str(len(body)), **(headers or {})}.items():
            self.headers[k] = v

    def readinto(self, b) -> int:
        if self._fail_after is not None and self._pos >= self._fail_after:
            raise ConnectionResetError("synthetic drop")
        end = len(self._body) if self._fail_after is None else self._fail_after
        n = min(len(b), end - self._pos, 7)
        b[:n] = self._body[self._pos : self._pos + n]
        self._pos += n
        return n

    def read(self) -> bytes:
        out, self._pos = self._body[self._pos :], len(self._body)
        return out

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _http_error(url, code, headers=None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, "synthetic", msg, None)


@pytest.fixture
def fake_http(monkeypatch):
    """Install a scripted urlopen: `script` is a list of callables (request) -> response | raises."""
    calls, sleeps, script = [], [], []

    def urlopen(req, timeout=None):
        calls.append(req)
        return script.pop(0)(req)

    monkeypatch.setattr(transport, "_urlopen", urlopen)
    monkeypatch.setattr(transport, "_sleep", sleeps.append)
    return calls, sleeps, script


def _raise(exc):
    def f(req):
        raise exc

    return f


# ---------------------------------------------------------------------------------------------------------------
# resolve_input: local files and checksum discovery


def test_local_with_sidecar():
    inp = resolve_input(str(FIXTURE))
    assert inp.expected_sha256 == sha256_of(FIXTURE) and inp.sha256_source == "sidecar"
    assert inp.name == FIXTURE.name and inp.size == FIXTURE.stat().st_size


def test_explicit_wins():
    inp = resolve_input(str(FIXTURE), sha256="A" * 64)
    assert inp.expected_sha256 == "a" * 64 and inp.sha256_source == "explicit"


def test_explicit_must_be_hex():
    with pytest.raises(InputError, match="64-digit"):
        resolve_input(str(FIXTURE), sha256="abc")


def test_missing_file(tmp_path):
    with pytest.raises(InputError, match="No such file"):
        resolve_input(str(tmp_path / "nope.json.gz"))


def test_unsupported_scheme():
    with pytest.raises(InputError, match="https"):
        resolve_input(f"http://data.example.org/{L}bundle.json.gz")


@pytest.mark.parametrize(
    "text, ok",
    [
        (f"{_sha('a')}  bundle_2026-10.json.gz\n", True),
        (f"{_sha('a')} *bundle_2026-10.json.gz\n", True),
        (f"{_sha('A')}\n", True),
        (f"{_sha('a')}  other.json.gz\n", False),
        ("not a checksum\n", False),
        (f"{_sha('a')}  a\n{_sha('b')}  b\n", False),
        ("", False),
    ],
)
def test_parse_sidecar(text, ok):
    if ok:
        assert parse_sidecar(text, "bundle_2026-10.json.gz", where="x") == _sha("a")
    else:
        with pytest.raises(InputError):
            parse_sidecar(text, "bundle_2026-10.json.gz", where="x")


def test_no_sidecar_no_checksum(tmp_path):
    p = tmp_path / "bundle.json.gz"
    p.write_bytes(FIXTURE.read_bytes())
    inp = resolve_input(str(p))
    assert (inp.expected_sha256, inp.sha256_source) == (None, None)


def test_a_sidecar_naming_another_file_is_refused(tmp_path):
    p = tmp_path / "bundle.json.gz"
    p.write_bytes(b"x")
    (tmp_path / "bundle.json.gz.sha256").write_text(f"{_sha('a')}  other.json.gz\n")
    with pytest.raises(InputError, match="names 'other.json.gz'"):
        resolve_input(str(p))
    assert resolve_input(str(p), sha256=_sha("b")).expected_sha256 == _sha("b")  # --sha256: the sidecar isn't read


def test_url_sidecar(fake_http):
    calls, _, script = fake_http
    name = "bundle_2026-10.json.gz"
    script.append(lambda req: FakeResponse(f"{_sha('a')}  {name}\n".encode()))
    inp = resolve_input(f"{BASE}/{L}{name}")
    assert (inp.expected_sha256, inp.sha256_source) == (_sha("a"), "sidecar")
    assert calls[0].full_url == f"{BASE}/{L}{name}.sha256" and calls[0].get_header("User-agent") == USER_AGENT


def test_url_without_a_sidecar(fake_http):
    _, _, script = fake_http
    script.append(_raise(_http_error("x", 404)))
    inp = resolve_input(f"{BASE}/{L}bundle.json.gz")
    assert (inp.expected_sha256, inp.sha256_source, inp.size) == (None, None, None)


# ---------------------------------------------------------------------------------------------------------------
# open_stream over HTTP


def _url_input(url):
    return InputFile(source=url, name=url.rsplit("/", 1)[-1], expected_sha256=None, sha256_source=None)


def test_http_stream_resumes_with_range(fake_http):
    calls, sleeps, script = fake_http
    body = bytes(range(256)) * 40
    script.append(lambda req: FakeResponse(body, headers={"ETag": '"v1"'}, fail_after=1000))

    def resumed(req):
        assert req.get_header("Range") == "bytes=1000-" and req.get_header("If-range") == '"v1"'
        return FakeResponse(
            body[1000:], status=206, headers={"Content-Range": f"bytes 1000-{len(body) - 1}/{len(body)}"}
        )

    script.append(resumed)
    with open_stream(_url_input(f"{BASE}/{L}x.json.gz")) as fh:
        assert fh.read() == body
    assert len(calls) == 2 and len(sleeps) == 1
    assert all(c.get_header("User-agent") == USER_AGENT for c in calls)
    assert calls[0].get_header("Accept-encoding") == "identity"


def test_http_stream_refuses_a_changed_file(fake_http):
    _, _, script = fake_http
    body = b"y" * 100
    script.append(lambda req: FakeResponse(body, headers={"ETag": '"v1"'}, fail_after=50))
    script.append(lambda req: FakeResponse(b"z" * 120))  # If-Range failed: the server sends the whole new file
    with pytest.raises(InputError, match="could not resume at byte 50"):
        with open_stream(_url_input(f"{BASE}/{L}x.json.gz")) as fh:
            fh.read()


def test_retry_429_and_5xx(fake_http):
    calls, sleeps, script = fake_http
    script.append(_raise(_http_error("x", 429, {"Retry-After": "3"})))
    script.append(_raise(_http_error("x", 503)))
    script.append(_raise(urllib.error.URLError("synthetic")))
    script.append(lambda req: FakeResponse(b"ok"))
    with open_stream(_url_input(f"{BASE}/{L}x.json.gz")) as fh:
        assert fh.read() == b"ok"
    assert len(calls) == 4 and sleeps == [3.0, 2.0, 4.0]


def test_no_retry_on_404(fake_http):
    calls, _, script = fake_http
    script.append(_raise(_http_error("x", 404)))
    with pytest.raises(InputError, match="HTTP 404"):
        open_stream(_url_input(f"{BASE}/{L}x.json.gz"))
    assert len(calls) == 1


def test_closing_a_stream_whose_request_failed(fake_http):
    # The request raises in __init__, so close() (called by the finalizer) must not need a response.
    _, _, script = fake_http
    script.append(_raise(_http_error("x", 404)))
    stream = transport._HttpStream.__new__(transport._HttpStream)
    with pytest.raises(InputError, match="HTTP 404"):
        stream.__init__(f"{BASE}/{L}x.json.gz")
    stream.close()
    assert stream.closed


def test_gives_up(fake_http):
    calls, _, script = fake_http
    script.extend([_raise(_http_error("x", 500))] * transport.MAX_ATTEMPTS)
    with pytest.raises(InputError, match="giving up"):
        open_stream(_url_input(f"{BASE}/{L}x.json.gz"))
    assert len(calls) == transport.MAX_ATTEMPTS


def test_local_stream():
    with open_stream(local_input(FIXTURE)) as fh:
        assert fh.read() == FIXTURE.read_bytes()
