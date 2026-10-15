"""Adversarial offline checks of allocations at the zlib boundary."""

import gzip
import random
import time
import zlib

import pytest

from world_intel_mcp import bounded_decode as bd


def deflate(body, raw=False):
    compressor = zlib.compressobj(wbits=-15 if raw else 15)
    return compressor.compress(body) + compressor.flush()


def decode(wire, encoding, cap, split=bd.INPUT_CHUNK):
    decoder = bd.BoundedDecoder(encoding, cap, deadline=time.monotonic() + 10)
    out = bytearray()
    for pos in range(0, len(wire), split):
        out.extend(b"".join(decoder.feed(wire[pos:pos + split])))
        list(decoder.feed(b""))
    decoder.finish()
    return bytes(out)


@pytest.mark.parametrize("encoding,compress", [
    ("gzip", gzip.compress), ("deflate", deflate),
    ("deflate", lambda b: deflate(b, True)),
])
@pytest.mark.parametrize("split", [1, 16384])
def test_exact_and_plus_one_and_bounded_calls(monkeypatch, encoding, compress, split):
    original = bd.zlib.decompressobj
    calls = []
    active_cap = 256 * 1024

    class Observed:
        def __init__(self, *args):
            self.inner = original(*args)
            self.emitted = 0

        def decompress(self, data, max_length=0):
            assert 0 < max_length <= bd.OUTPUT_CHUNK
            assert max_length <= active_cap - self.emitted + 1
            assert len(data) <= bd.INPUT_CHUNK
            result = self.inner.decompress(data, max_length)
            assert len(result) <= max_length
            self.emitted += len(result)
            calls.append((len(result), max_length))
            return result

        def flush(self, *args):
            pytest.fail("flush does not enforce an output limit")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(bd.zlib, "decompressobj", Observed)
    body = b"a" * (256 * 1024)
    assert decode(compress(body), encoding, len(body), split) == body
    calls.clear()
    active_cap = 100
    with pytest.raises(bd.ResponseTooLarge):
        decode(compress(body), encoding, 100, split)
    assert max(n for n, _ in calls) <= 101
    assert calls[-1][1] <= 101


def test_concatenated_gzip_shared_cap_and_member_limit():
    wire = gzip.compress(b"a") + gzip.compress(b"bc")
    assert decode(wire, "gzip", 3, 1) == b"abc"
    with pytest.raises(bd.ResponseTooLarge):
        decode(wire, "gzip", 2)
    empty = gzip.compress(b"")
    assert decode(empty * 1024, "gzip", 1) == b""
    with pytest.raises(bd.BodyLimitError, match="member limit"):
        decode(empty * 1025, "gzip", 1)


@pytest.mark.parametrize("encoding,wire", [
    ("gzip", b""), ("gzip", gzip.compress(b"x")[:-1]),
    ("gzip", gzip.compress(b"x")[:-8] + b"\0" * 8),
    ("gzip", gzip.compress(b"x") + b"garbage"),
    ("gzip", gzip.compress(b"x") + b"\0"),
    ("deflate", deflate(b"x")[:-1]),
    ("deflate", deflate(b"x") + b"garbage"),
    ("deflate", deflate(b"x") + deflate(b"y")),
])
def test_truncation_checksum_trailing_and_padding_fail(encoding, wire):
    with pytest.raises(bd.BodyLimitError):
        decode(wire, encoding, 10, 1)


def test_application_gzip_uses_same_bounded_decoder(monkeypatch):
    monkeypatch.setattr(gzip, "decompress", lambda *_: pytest.fail("unbounded gzip"))
    assert bd.decode_gzip(gzip.compress(b"ok"), 2) == b"ok"
    with pytest.raises(bd.ResponseTooLarge):
        bd.decode_gzip(gzip.compress(b"x" * 100000), 8)


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("split", [1, bd.INPUT_CHUNK])
def test_deflate_prefix_split_with_full_incompressible_input(raw, split):
    body = random.Random(123).randbytes(bd.INPUT_CHUNK * 2 + 1)
    wire = deflate(body, raw)
    # The first byte can leave a pending prefix before a full-sized next feed.
    decoder = bd.BoundedDecoder("deflate", len(body), deadline=time.monotonic() + 10)
    out = bytearray(b"".join(decoder.feed(wire[:1])))
    for pos in range(1, len(wire), split):
        out.extend(b"".join(decoder.feed(wire[pos:pos + split])))
    decoder.finish()
    assert out == body


def test_deadline_and_input_contract():
    decoder = bd.BoundedDecoder("gzip", 10, deadline=0)
    with pytest.raises(bd.BodyLimitError, match="deadline"):
        list(decoder.feed(b"x"))
    decoder.deadline = time.monotonic() + 10
    with pytest.raises(ValueError, match="internal limit"):
        list(decoder.feed(b"x" * (bd.INPUT_CHUNK + 1)))


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), None])
def test_finite_deadline_validation(value):
    with pytest.raises(ValueError):
        bd.processing_timeout(value)
