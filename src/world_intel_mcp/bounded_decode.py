"""Bounded gzip/deflate output, independent of HTTPX's unbounded decoders.

Limits cover this module's work, not transport allocations or parsed objects.
Gzip permits concatenated members, but no padding or non-member trailing bytes.
"""

import math
import time
import zlib

INPUT_CHUNK = 16 * 1024
OUTPUT_CHUNK = 64 * 1024
MAX_GZIP_MEMBERS = 1024
DEFAULT_PROCESSING_TIMEOUT = 120.0


class BodyLimitError(ValueError):
    """Terminal body size, encoding, integrity or processing-policy failure."""


class ResponseTooLarge(BodyLimitError):
    """An encoded or decoded body exceeded its budget."""


def positive_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("byte limit must be a positive integer")
    return value


def processing_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("processing_timeout must be finite and positive")
    return float(value)


class BoundedDecoder:
    """Feed at most INPUT_CHUNK bytes; each yielded allocation is capped.

    Consumers must exhaust feed() before calling it again. No flush() is used:
    zlib's flush length is a buffer hint, not an output limit. eof validates the
    trailer/checksum; incomplete streams fail at finish().
    """

    def __init__(self, encoding: str, max_bytes: int, *, deadline: float):
        if encoding not in ("identity", "gzip", "deflate"):
            raise BodyLimitError("unsupported content encoding")
        self.encoding = encoding
        self.max_bytes = positive_bytes(max_bytes)
        self.deadline = deadline
        self.total = 0
        self.members = 0
        self.decoder = None
        self.prefix = b""

    def check_time(self):
        if time.monotonic() >= self.deadline:
            raise BodyLimitError("body processing deadline exceeded")

    def feed(self, data: bytes):
        self.check_time()
        if len(data) > INPUT_CHUNK:
            raise ValueError("decoder input chunk exceeds internal limit")
        if self.encoding == "identity":
            self.total += len(data)
            if self.total > self.max_bytes:
                raise ResponseTooLarge(f"decoded body exceeds cap {self.max_bytes} bytes")
            if data:
                yield data
            return
        if not data:
            return
        if self.encoding == "deflate" and self.decoder is None:
            # Initial bounded wrapped/raw detection. Probe only the first two
            # bytes; never retry an entire body or switch after output begins.
            data = self.prefix + data
            self.prefix = b""
            if len(data) < 2:
                self.prefix = data
                return
            probe = zlib.decompressobj(zlib.MAX_WBITS)
            try:
                probe.decompress(data[:2], 1)
                self.decoder = probe
                data = data[2:]
            except zlib.error:
                self.decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        drain = False
        while data or drain:
            self.check_time()
            if self.decoder is None or self.decoder.eof:
                if self.encoding == "deflate":
                    raise BodyLimitError("trailing data after deflate stream")
                if self.members >= MAX_GZIP_MEMBERS:
                    raise BodyLimitError("gzip member limit exceeded")
                self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                self.members += 1
            limit = min(OUTPUT_CHUNK, self.max_bytes - self.total + 1)
            try:
                output = self.decoder.decompress(data[:INPUT_CHUNK], limit)
            except zlib.error as exc:
                raise BodyLimitError("invalid compressed body") from exc
            self.total += len(output)
            if self.total > self.max_bytes:
                raise ResponseTooLarge(f"decoded body exceeds cap {self.max_bytes} bytes")
            remainder = data[INPUT_CHUNK:]
            data = (self.decoder.unused_data if self.decoder.eof else self.decoder.unconsumed_tail) + remainder
            drain = not self.decoder.eof and len(output) == limit
            # Yield even empty output so callers can cooperate on empty members.
            yield output

    def finish(self):
        self.check_time()
        if self.encoding != "identity" and (self.decoder is None or not self.decoder.eof):
            raise BodyLimitError("truncated compressed body")


def decode_gzip(body: bytes, max_bytes: int, *, timeout: float = DEFAULT_PROCESSING_TIMEOUT) -> bytes:
    """Synchronous application-gzip boundary (also used in a heatmap thread)."""
    decoder = BoundedDecoder("gzip", max_bytes, deadline=time.monotonic() + processing_timeout(timeout))
    result = bytearray()
    for offset in range(0, len(body), INPUT_CHUNK):
        for output in decoder.feed(body[offset:offset + INPUT_CHUNK]):
            result.extend(output)
    decoder.finish()
    return bytes(result)
