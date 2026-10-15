# Security Policy

## Reporting a vulnerability

Open a GitHub issue with the label `security`, or email the address on
the maintainer's GitHub profile if the details should stay private
until fixed. Reports that include a runnable reproduction get fixed
fastest. Please state the attacker position your finding assumes (see
the threat model below): a report whose precondition is already
arbitrary local write access to the user's files will be assessed
against that boundary.

## Threat model

world-intel-mcp is a single-user, local tool. It runs as an MCP stdio
server, a CLI, a local dashboard, or a local collector daemon, under
one user account, on that user's machine. It aggregates free public
OSINT feeds. Its trust boundaries:

1. **Upstream data is untrusted and unauthenticated by design.** The
   server aggregates public APIs and RSS feeds (USGS, GDELT, adsb.lol,
   NASA FIRMS, and about 115 others). None of these are signed. The
   server preserves provenance (`source` labels, per-feed propaganda
   risk tiers, `_stale` markers, `data_gaps`, `cited` flags) so a
   consumer can judge the data; it cannot make public data
   trustworthy. Analysis outputs are derived from these inputs and
   inherit their trust level. Treat every output as open-source
   intelligence, not as verified ground truth.

2. **The local machine is inside the trust boundary.** The SQLite
   cache, the AOI store, and the optional Qdrant vector store live on
   the user's own machine and are trusted by the process that wrote
   them. An attacker who can already write the user's files or reach
   the user's local services can feed the server false data; that same
   position also allows patching this package's code, so in-database
   integrity signing would not remove the attack, only relocate it.
   Hardening applied at this boundary: the cache database is created
   with owner-only permissions (0600), all SQL is parameterized, and
   the codebase contains no shell execution, no `eval`/`exec`, and no
   deserialization of non-JSON formats.

3. **MCP tool arguments are untrusted input.** Arguments are validated
   (ranges, types) where they parameterize queries, are never
   interpolated into SQL (parameterized statements only), never reach
   a shell (no subprocess usage exists in the package), and never
   select filesystem paths (report output paths are server-generated,
   not caller-supplied).

4. **Optional local services are the operator's responsibility.**
   Qdrant (vector search) and Ollama (brief generation) are reached at
   `localhost` URLs. Bind them to loopback, as their defaults do; if
   you expose them on a network, that exposure is outside this
   project's control. Do not point `OLLAMA_API_URL` at an untrusted
   host: its responses are rendered in briefs.

5. **Stale data is a feature with a marker, not a hidden fallback.**
   When an upstream fails, the fetcher may serve expired cache entries
   so dashboards do not go blank. Such responses carry `_stale: true`,
   and circuit-breaker state is visible via `intel_status`. Consumers
   that must not act on stale data must check the marker.

## Credential-safe diagnostics

Fetcher HTTP diagnostics retain source, exception class, status (when present),
and scheme/hostname/port, but omit URL userinfo, **all** paths, queries and
fragments. This also covers NASA FIRMS keys in path segments and query keys
with arbitrary names/casing/percent encoding. Stale-cache and cache-write logs
do not print cache keys, which can themselves contain authenticated URLs.

MCP argument logs use a copied structure: credential-labelled values (API/map
keys, tokens, secrets, passwords, auth/cookie headers and credential objects)
are masked recursively, with case-insensitive, percent-decoded key matching.
Recognized URLs and common labelled credentials in diagnostic text are scrubbed
too. Actual arguments, outbound requests, cached data and ordinary result fields
are not rewritten. Known `error`, `_error` and `errors` fields are sanitized in
MCP/dashboard output copies. Within those error subtrees, credential-labelled
values are masked even when they contain dictionaries or lists; equivalent
keys outside error subtrees remain ordinary data. Vector, report, collector,
webhook and OAuth/Ollama errors use these helpers. The generic coroutine wrappers
in AOI, alerts, posture, exposure, company, macro composite, situation, daily
digest, dossier, world brief and fleet, plus the audited caught-exception logger
calls in source parsers, also use safe exception summaries. Handled exceptions
in these paths do not render raw tracebacks or chained exception strings.

Package startup installs drop filters on the standard `httpx` logger and
`httpcore` connection/HTTP1/HTTP2/proxy/SOCKS loggers, including their DEBUG
header/exception traces. This applies across CLI, MCP, dashboard and collector
entrypoints regardless of the root log level. The MCP SDK's
`mcp.server.lowlevel.server` logger is also suppressed because it logs raw
request objects at DEBUG; redacted application tool-call diagnostics remain.
No root handler, record factory
or arbitrary application log records are globally rewritten.

An operator with a separate credential-safe logging pipeline can call
`diagnostics.suppress_transport_logs(enabled=False)` to remove only this
package's drop filters, preserving other filters, handlers and levels.
Calling it with the default `enabled=True` restores suppression idempotently.
Opting out can expose raw transport credentials unless that pipeline protects
them; package startup enables suppression by default.

This is **not universal free-text secret detection**. Unlabelled values in
queries/prose, normal upstream result data, filesystem paths, non-exception
data-driven source logs, newly named third-party loggers, custom HTTP
trace/event hooks, external telemetry and operator logging reconfiguration are
outside this coverage. Library callers bypassing MCP/dashboard output boundaries
must use the helpers for additional error paths. Local validation messages in
`analysis/dossier.py`, `analysis/jet_activity.py`, `sources/migration.py` and
`sources/jet_cohort.py`, and heatmap parse errors in the latter, still use plain
exception text at source; these are not generic HTTP-exception wrappers, and
known error fields are sanitized when passing through MCP/dashboard boundaries.
Do not put credentials in
ordinary free-text fields or assume all historic logs have been scrubbed.

## Cache filesystem boundary

On POSIX systems the cache creates new directories with mode `0700` and its
database with `0600` **before** SQLite opens it. SQLite WAL/SHM files inherit
the database's restrictive mode. Existing main/WAL/SHM/journal files must be
regular, singly linked, owned by the current effective UID, and not writable
by group/others. Verified owned files with read-only group/other access are
tightened through their file descriptors before opening SQLite. Unsafe paths
are refused, not repaired or silently bypassed.

Directory components may not be symlinks, foreign-owned, or shared-writable;
root-owned ancestors and sticky shared ancestors (such as `/tmp`) are allowed.
The immediate database directory must be owned by the current UID and not
shared-writable. Existing explicit parent directories need not be `0700` and
are not chmodded; dedicated default/fallback directories are made private.
Use canonical directory paths where system aliases are symlinks.

If the ordinary home-cache location is unavailable, the fallback is
`<system-temp>/world-intel-mcp-<effective-uid>/cache.db`, with verified ownership
and a private directory. The old shared `world-intel-mcp/cache.db` is never
used. A precreated unsafe UID-specific directory blocks fallback rather than
being trusted. Fallback is reused across runs but remains subject to system
temporary-file cleanup; the application performs no automatic cleanup.
Explicit `db_path`, `WORLD_INTEL_CACHE_DB`, and `XDG_CACHE_HOME` failures now
surface to the caller instead of silently selecting different storage.

Directory descriptors and no-follow opens reduce path races, but SQLite still
opens by pathname to manage its sidecars. This does not protect against root
or same-UID path replacement, non-POSIX permission behavior, or permissive ACLs
not reflected in mode bits. POSIX no-follow/descriptor support is required;
unsupported platforms fail closed. AOI connections share the path prepared by
`Cache`; independently constructed AOI stores are not hardened by this change.

## Upstream response limits

GET bodies are streamed with cumulative **decoded-byte** caps: 32 MiB for
JSON and text/XML, and 256 MiB for binary history downloads. Python callers
can configure `Fetcher(max_json_bytes=..., max_text_bytes=...)` and pass
`max_bytes=...` per GET; existing positional parameters are unchanged.
Content-Length is not trusted. Oversized responses are closed, never retried
or cached, and count as one circuit-breaker failure. JSON/text/XML use the
existing stale fallback; binary calls return an error with no partial body.
There is also an **encoded-byte** budget, defaulting to the selected decoded
cap plus 1 MiB. Set `max_encoded_bytes=...` on `Fetcher` or an individual GET
to override it. Constructor and per-GET `processing_timeout=...` select a finite
positive body-reader deadline (default **120 seconds**, per attempt), in addition
to HTTPX's network inactivity timeout. These new options are keyword-only.

GETs explicitly negotiate only `gzip, deflate`, including injected clients and
redirects. Absent Content-Encoding and `identity` are accepted; br/zstd, unknown,
stacked, empty or malformed encodings are rejected before body reads. This is
intentional even when optional compression libraries are installed. Encoded
bodies are read through HTTPX's **raw** iterator and a package-owned stdlib zlib
decoder, not HTTPX's automatic decompression. Input slices are at most 16 KiB;
every zlib output request is at most `min(64 KiB, remaining decoded budget + 1)`.
The sentinel byte detects overflow without retaining a partial result. There
are no unbounded decompress/flush calls. Wrapped deflate is tried on an initial
two-byte prefix, with raw-deflate fallback only there, before any output. An
ambiguous raw stream with a valid zlib prefix is not retried as raw later.
Gzip permits at most **1,024 concatenated members**, sharing one decoded budget;
checksums and termination are required, and **all padding/trailing garbage is
rejected**, including zero padding. Size, encoding, integrity, member-limit and
processing-deadline errors are terminal, with no retry/cache/vector writes.

The reader yields cooperatively between bounded operations. Its deadline starts
after final response headers reach the reader, includes body-stream waits, and
excludes connection/header acquisition, rate limiting, auth/hooks, parsing and
cache writes. It cannot interrupt a synchronous native call or guarantee an
instantaneous cancellation/closure under a misbehaving custom transport.
Redirect and HTTP-error bodies exposed to the fetcher are discarded without
reading them. Client authentication is applied on the initial send only;
redirects preserve HTTPX's Authorization stripping and retention decisions.

The default fetcher client has no challenge authentication or body-reading
hooks. For an injected `httpx.AsyncClient`, the streaming-cap guarantee
requires authentication flows and hooks that do not consume response bodies
themselves. HTTPX may buffer intermediate authentication responses (for
example, Digest challenges) before returning control to the bounded reader;
those internal reads are not capped here. Basic auth and explicit Authorization
headers do not require challenge-body reads.

Pre-buffered HTTPX responses are supported as already-decoded content, never
decoded twice. The reader checks their decoded size and any observed download
count, but cannot undo their prior allocations or recover unobserved encoded
size. Custom transports must supply raw encoded bytes on unconsumed streams;
their own buffering/decompression is outside this boundary. Standard HTTPcore
uses bounded network reads, but arbitrary custom streams may allocate a large
raw chunk before yielding control here.

Heatmap application gzip (`.bin.gz` data, independently of HTTP Content-Encoding)
uses the same bounded gzip primitive. `HEATMAP_MAX_COMPRESSED_BYTES` and
`HEATMAP_MAX_EXPANDED_BYTES` default separately to **256 MiB**; raw heatmaps must
fit both. Override `max_compressed_bytes`, `max_expanded_bytes` and
`processing_timeout` on `HeatmapDiskCache` (or directly on `parse_heatmap`). Disk
cache reads stop at the input cap plus a sentinel byte; downloaded and cached
bodies undergo expanded-size validation before record parsing. The heatmap
deadline covers application decompression plus parsing (checked every 4,096
records), not disk I/O or cache compression/writes. Parsing runs in a thread;
cancelling its await does **not** kill that thread. Byte/record bounds and the
thread's deadline constrain work, not the memory footprint of its Python objects.

These caps bound accepted body bytes and fetcher-controlled decompression output,
**not total process memory**. Accumulation/final copies, decompressor state,
text/JSON objects, heatmap sets/tracks, transport/native allocations and concurrent
requests require additional memory. There are no general JSON depth/node limits
or process isolation in this patch. Previously cached JSON/text data is not
re-sized by these network limits. Operator-configured outbound POST sinks
are outside these GET limits; this is not a URL/SSRF policy.

## Report rendering

Report titles and upstream values are rendered as escaped text, not trusted
HTML. Numeric formatting escapes invalid text and shows nonfinite or
overflowing values as unavailable. PDF reports use self-contained CSS and an
explicit deny-all URL resource loader: network, file, and data URLs cannot supply
images, stylesheets, web fonts, or attachments. Normal system font discovery
remains available. A blocked resource may be omitted while the PDF completes.

MCP report output paths are generated by the server. The CLI's `--output`
path is an explicit local operator choice, not an MCP caller parameter.

## Browser assets and dependency updates

The dashboard pins Leaflet 1.9.4, markercluster 1.5.3 and DOMPurify 3.4.17.
Their external JS/CSS tags use reviewed SHA-384 Subresource Integrity digests
and `crossorigin="anonymous"`; the exact URLs/hashes and verification record
are in [the browser asset manifest](docs/dashboard-assets.json). Hashes were
computed from downloaded bytes and checked against exact npm release files,
not guessed. Updating a version requires re-reviewing advisories and verifying
new bytes/hashes before changing the manifest and HTML together.

Google Fonts' client-dependent CSS is explicitly excepted from stable SRI to
preserve the existing typography. It and downloaded fonts remain external
trust/privacy dependencies. SRI on a stylesheet does **not** cover its images
or fonts; Leaflet images and map tiles also remain outside that integrity check.
CDN outages or changed bytes can prevent rendering; there is no unsafe sanitizer
fallback. Pinning/SRI do not prove publisher or registry integrity.

[The 2026-10-15 dependency audit](docs/dependency-audit-2026-10-15.md) records
installed/resolved inventories, advisory identifiers/ranges, reviewed floor
changes, commands, tests and unresolved issues. Python dependencies remain
ranges, not a repository-wide lock. Audit the entire deployed environment after
updates, including transitives; old installed Pillow/urllib3 versions can remain
satisfiable. Keep native libraries, separate Qdrant/Ollama services and embedding
models under their own update policy. Clean advisory output is not an exhaustive
security or runtime-compatibility guarantee.

## Supported versions

Only the latest release receives fixes.

## Out of scope

- Findings that assume arbitrary local file write, code execution, or
  root on the user's machine (boundary 2 above).
- The authenticity of upstream public OSINT data (boundary 1).
- The network exposure of separately installed services (boundary 4).

## Gaps / not covered

- No cryptographic integrity protection on the cache or vector store;
  see boundary 2 for why that is not the chosen control.
- Dependency supply chain still relies on publishers, registries and install
  tooling; reviewed minimums and browser SRI do not remove that trust.
- The dashboard binds locally and has no authentication; do not
  reverse-proxy it to an untrusted network.
