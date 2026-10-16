"""Content-only feed parsing: downloaded bodies are never resource locators."""

from io import BytesIO


def parse_feed_content(content: str | bytes):
    """Parse in-memory RSS/Atom without feedparser's URL/path auto-detection.

    Match feedparser's ordinary inline-text conversion to UTF-8. Preserve byte
    input exactly so its XML declaration/BOM still controls encoding detection.
    Do not accept paths or arbitrary file-like objects at this boundary.
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    elif not isinstance(content, bytes):
        raise TypeError("feed content must be str or bytes")

    # Keep this lazy so sources retain their existing missing-feedparser errors.
    import feedparser

    with BytesIO(content) as stream:
        return feedparser.parse(stream)
