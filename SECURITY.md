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

## Upstream response limits

GET bodies are streamed with cumulative **decoded-byte** caps: 32 MiB for
JSON and text/XML, and 256 MiB for binary history downloads. Python callers
can configure `Fetcher(max_json_bytes=..., max_text_bytes=...)` and pass
`max_bytes=...` per GET; existing positional parameters are unchanged.
Content-Length is not trusted. Oversized responses are closed, never retried
or cached, and count as one circuit-breaker failure. JSON/text/XML use the
existing stale fallback; binary calls return an error with no partial body.
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

These caps bound accepted body bytes, not total process memory: HTTPX may
produce a larger individual decompressed chunk before the cap check, and
parsing/decoding requires additional memory. Previously cached data is not
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
- Dependency supply chain is managed by pinning and review, not by
  vendoring; `pip install` trust applies.
- The dashboard binds locally and has no authentication; do not
  reverse-proxy it to an untrusted network.
