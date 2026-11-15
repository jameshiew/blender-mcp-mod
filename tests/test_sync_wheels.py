import hashlib
import importlib
import io
import ssl
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError

import pytest
from conftest import REPO_ROOT

URL = "https://example.invalid/package-1-py3-none-any.whl"


@pytest.fixture
def sync_wheels(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "tools"))
    return importlib.import_module("sync_wheels")


@pytest.fixture
def delays(sync_wheels, monkeypatch):
    delays = []
    monkeypatch.setattr(sync_wheels.time, "sleep", delays.append)
    return delays


@pytest.mark.parametrize(
    "error",
    [
        URLError(ssl.SSLEOFError("TLS handshake closed")),
        TimeoutError("Timed out"),
        HTTPError(URL, 429, "Too Many Requests", Message(), None),
        HTTPError(URL, 503, "Service Unavailable", Message(), None),
    ],
)
def test_download_retries_connection_failures(
    sync_wheels, delays, monkeypatch, error, capsys
):
    fetch = Mock(side_effect=[error, io.BytesIO(b"wheel")])
    monkeypatch.setattr(sync_wheels, "urlopen", fetch)

    assert sync_wheels.download(URL) == b"wheel"
    assert fetch.call_count == 2
    fetch.assert_called_with(URL, timeout=60)
    assert delays == [1]
    assert "retrying in 1s" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("Connection reset"),
        TimeoutError("Timed out"),
        IncompleteRead(b"partial", 10),
        ssl.SSLEOFError("TLS connection closed"),
    ],
)
def test_download_retries_interrupted_reads(sync_wheels, delays, monkeypatch, error):
    response = MagicMock()
    response.__enter__.return_value.read.side_effect = error
    fetch = Mock(side_effect=[response, io.BytesIO(b"wheel")])
    monkeypatch.setattr(sync_wheels, "urlopen", fetch)

    assert sync_wheels.download(URL) == b"wheel"
    assert fetch.call_count == 2
    assert delays == [1]
    response.__exit__.assert_called_once()


def test_download_stops_after_four_attempts(sync_wheels, delays, monkeypatch):
    error = URLError("Network unavailable")
    fetch = Mock(side_effect=error)
    monkeypatch.setattr(sync_wheels, "urlopen", fetch)

    with pytest.raises(URLError) as raised:
        sync_wheels.download(URL)

    assert raised.value is error
    assert fetch.call_count == 4
    assert delays == [1, 2, 4]


@pytest.mark.parametrize(
    "error",
    [
        HTTPError(URL, 403, "Forbidden", Message(), None),
        HTTPError(URL, 404, "Not Found", Message(), None),
        ssl.SSLCertVerificationError("Certificate verification failed"),
        URLError(ssl.SSLCertVerificationError("Certificate verification failed")),
    ],
)
def test_download_does_not_retry_permanent_failures(
    sync_wheels, delays, monkeypatch, error
):
    fetch = Mock(side_effect=error)
    monkeypatch.setattr(sync_wheels, "urlopen", fetch)

    with pytest.raises(type(error)) as raised:
        sync_wheels.download(URL)

    assert raised.value is error
    assert fetch.call_count == 1
    assert delays == []


@pytest.fixture
def wheel_target(sync_wheels, monkeypatch, tmp_path):
    monkeypatch.setattr(sync_wheels, "ROOT", tmp_path)
    (tmp_path / "extension").mkdir()
    checksum = hashlib.sha256(b"wheel").hexdigest()
    (tmp_path / "uv.lock").write_text(f"""[[package]]
name = "project"
source = {{ virtual = "." }}
[package.dev-dependencies]
addon = [{{ name = "package" }}]

[[package]]
name = "package"
wheels = [{{ url = "{URL}", hash = "sha256:{checksum}" }}]
""")
    return tmp_path / "extension" / "wheels" / URL.rsplit("/", 1)[1]


def test_sync_verifies_retried_download_and_reuses_cache(
    sync_wheels, delays, wheel_target, monkeypatch, capsys
):
    fetch = Mock(side_effect=[URLError("Connection closed"), io.BytesIO(b"wheel")])
    monkeypatch.setattr(sync_wheels, "urlopen", fetch)

    sync_wheels.main()
    assert wheel_target.read_bytes() == b"wheel"
    assert f"Downloading {wheel_target.name}" in capsys.readouterr().out
    sync_wheels.main()
    assert fetch.call_count == 2
    assert delays == [1]


def test_sync_rejects_checksum_mismatch_without_overwriting(
    sync_wheels, delays, wheel_target, monkeypatch
):
    wheel_target.parent.mkdir()
    wheel_target.write_bytes(b"old wheel")
    fetch = Mock(return_value=io.BytesIO(b"corrupted download"))
    monkeypatch.setattr(sync_wheels, "urlopen", fetch)

    with pytest.raises(ValueError, match="Checksum mismatch"):
        sync_wheels.main()

    assert wheel_target.read_bytes() == b"old wheel"
    assert fetch.call_count == 1
    assert delays == []
