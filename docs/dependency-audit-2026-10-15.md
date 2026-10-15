# Dependency and browser-asset audit — 2026-10-15

## Scope and evidence

Audit window: **2026-10-15 15:14–15:35 UTC**. Tools: `uv 0.13.1`, isolated
`pip-audit 2.10.1`, Python 3.12.15. Public sources: PyPI's vulnerability API
(pip-audit default), GitHub's reviewed advisory API, npm registry metadata, and
OSV's npm version-query API. Only public package names/versions and public
assets were queried; no environment contents, application credentials or OSINT
requests were sent. Third-party installations/resolution were wheel-only; no
npm lifecycle scripts or downloaded JavaScript were executed.

The installed inventory is the available **test environment**,
`/tmp/opencode/dashboard-security-tests`, not a claim about a production
deployment. Vector extras were not installed there. Fresh resolutions included
**runtime + dashboard + vector + pdf + dev**, targeting Linux CPython 3.11 and
3.12. A separate vector compatibility environment installed the revised vector
minimums without downloading embedding models or contacting Qdrant.

Temporary inventories, JSON scanner output, asset bytes and comparison evidence
are under `/tmp/opencode/dependency-audit/`. They are audit evidence, not a new
project lockfile or installation requirements file.

## Direct inventory and reviewed floor changes

Upper bounds already present on MCP/Pydantic are unchanged. A dash means not
installed in the main test environment. Fresh latest direct versions were the
same for Python 3.11 and 3.12.

| Package / group | Previous declared lower bound | Installed | Fresh latest | Revised lower bound |
|---|---:|---:|---:|---:|
| mcp / runtime | 1.8.0 | 1.30.0 | 1.30.0 | **1.28.1** |
| httpx[socks] / runtime | 0.27.0 | 0.28.1 | 0.28.1 | unchanged |
| pydantic / runtime | 2.10.0 | 2.14.1 | 2.14.1 | unchanged |
| feedparser / runtime | 6.0.0 | 6.0.14 | 6.0.14 | unchanged |
| click / runtime | 8.1.0 | 8.5.0 | 8.5.0 | unchanged |
| jinja2 / runtime | 3.1.0 | 3.1.6 | 3.1.6 | **3.1.6** |
| rich / runtime | 13.0.0 | 15.0.0 | 15.0.0 | unchanged |
| starlette / dashboard | 0.37.0 | 1.8.0 | 1.8.0 | **1.3.1** |
| uvicorn / dashboard | 0.29.0 | 0.54.0 | 0.54.0 | unchanged |
| qdrant-client / vector | 1.7.0 | — | 1.19.1 | **1.9.0** |
| fastembed / vector | 0.7.0 | — | 0.9.0 | **0.8.0** |
| weasyprint / pdf | 62.0 | 70.0 | 70.0 | **70.0** |
| pytest / dev | 8.0.0 | 9.1.1 | 9.1.1 | **9.0.3** |
| pytest-asyncio / dev | 0.23.0 | 1.4.0 | 1.4.0 | **1.3.0** |
| pytest-cov / dev | 4.0.0 | 7.1.0 | 7.1.0 | unchanged |
| respx / dev | 0.21.0 | 0.23.1 | 0.23.1 | unchanged |

The MCP floor additionally requires httpx >=0.27.1, Pydantic >=2.11 and
Uvicorn >=0.31.1 on Python 3.11/3.12. Individual declared minima therefore are
not necessarily jointly installable. The lowest-direct **wheel-only** sample selected
feedparser 6.0.13 rather than the declared 6.0.0; source-only older releases
were still checked individually against advisories without building them.

## Scanner results

`pip-audit` was run with `--no-deps --disable-pip --strict --aliases on -f json`
on exact-version inventories. This avoids pip resolution/build execution inside
the scanner. uv performed the separate wheel-only dependency resolution.

| Inventory | Packages | Findings |
|---|---:|---|
| Installed test environment, excluding this local editable project | 57 | No known matches |
| Fresh latest, all extras, Python 3.12 | 81 | No known matches |
| Fresh latest, all extras, Python 3.11 | 82 | No known matches |
| Old direct minimums, individually | 16 | 44 scanner records: **23 distinct package/advisory IDs**, 6 packages |
| Old lowest-direct wheel-only graph, Python 3.12 | 77 | 91 records: **48 distinct package/advisory IDs**, 8 packages |
| Revised direct minimums, individually | 16 | No known matches |
| Revised lowest-direct wheel-only graph, Python 3.12 | 86 | No known matches |

The service returned duplicate PYSEC records/aliases in some responses; these
are not separate vulnerabilities. Counts above explicitly distinguish raw
records from deduplicated package/advisory pairs. "No known matches" is only
the provider's result at audit time, not proof of safety or of reachability.

### Direct-minimum advisory inventory

The affected column is the concrete version audited, **not** a claim that every
version between it and the fix is affected. The authoritative linked advisories
contain full ranges and exploit prerequisites.

| Package, affected version audited | Advisory / CVE | Reported first patched version |
|---|---|---|
| Jinja2 3.1.0 | [GHSA-h5c8-rqwp-cp95](https://github.com/advisories/GHSA-h5c8-rqwp-cp95), CVE-2024-22195 | 3.1.3 |
| Jinja2 3.1.0 | [GHSA-h75v-3vvj-5mfj](https://github.com/advisories/GHSA-h75v-3vvj-5mfj), CVE-2024-34064 | 3.1.4 |
| Jinja2 3.1.0 | [GHSA-q2x7-8rv6-6q7h](https://github.com/advisories/GHSA-q2x7-8rv6-6q7h), CVE-2024-56326 | 3.1.5 |
| Jinja2 3.1.0 | [GHSA-gmj6-6f8f-6699](https://github.com/advisories/GHSA-gmj6-6f8f-6699), CVE-2024-56201 | 3.1.5 |
| Jinja2 3.1.0 | [GHSA-cpwx-vrp4-4pq7](https://github.com/advisories/GHSA-cpwx-vrp4-4pq7), CVE-2025-27516 | 3.1.6 |
| MCP 1.8.0 | [GHSA-3qhf-m339-9g5v](https://github.com/advisories/GHSA-3qhf-m339-9g5v), CVE-2025-53366 | 1.9.4 |
| MCP 1.8.0 | [GHSA-j975-95f5-7wqh](https://github.com/advisories/GHSA-j975-95f5-7wqh), CVE-2025-53365 | 1.10.0 |
| MCP 1.8.0 | [GHSA-9h52-p55h-vw2f](https://github.com/advisories/GHSA-9h52-p55h-vw2f), CVE-2025-66416 | 1.23.0 |
| MCP 1.8.0 | [GHSA-jpw9-pfvf-9f58](https://github.com/advisories/GHSA-jpw9-pfvf-9f58), CVE-2026-52869 | 1.27.2 |
| MCP 1.8.0 | [GHSA-vj7q-gjh5-988w](https://github.com/advisories/GHSA-vj7q-gjh5-988w), CVE-2026-59950 | 1.28.1 |
| pytest 8.0.0 | [GHSA-6w46-j5rx-g56g](https://github.com/advisories/GHSA-6w46-j5rx-g56g), CVE-2025-71176 | 9.0.3 |
| qdrant-client 1.7.0 (provider mapping; see below) | [GHSA-7m75-x27w-r52r](https://github.com/advisories/GHSA-7m75-x27w-r52r), CVE-2024-3829 | 1.9.0 |
| Starlette 0.37.0 | [GHSA-f96h-pmfr-66vw](https://github.com/advisories/GHSA-f96h-pmfr-66vw), CVE-2024-47874 | 0.40.0 |
| Starlette 0.37.0 | [GHSA-2c2j-9gv5-cj73](https://github.com/advisories/GHSA-2c2j-9gv5-cj73), CVE-2025-54121 | 0.47.2 |
| Starlette 0.37.0 | [GHSA-86qp-5c8j-p5mr](https://github.com/advisories/GHSA-86qp-5c8j-p5mr), CVE-2026-48710 | 1.0.1 |
| Starlette 0.37.0 | [GHSA-wqp7-x3pw-xc5r](https://github.com/advisories/GHSA-wqp7-x3pw-xc5r), CVE-2026-48818 | 1.1.0 |
| Starlette 0.37.0 | [GHSA-x746-7m8f-x49c](https://github.com/advisories/GHSA-x746-7m8f-x49c), CVE-2026-48817 | 1.1.0 |
| Starlette 0.37.0 | [GHSA-jp82-jpqv-5vv3](https://github.com/advisories/GHSA-jp82-jpqv-5vv3), CVE-2026-54282 | 1.3.0 |
| Starlette 0.37.0 | [GHSA-82w8-qh3p-5jfq](https://github.com/advisories/GHSA-82w8-qh3p-5jfq), CVE-2026-54283 | 1.3.1 |
| WeasyPrint 62.0 | [GHSA-983w-rhvv-gwmv](https://github.com/advisories/GHSA-983w-rhvv-gwmv), CVE-2025-68616 | 68.0 |
| WeasyPrint 62.0 | [GHSA-jhhc-3hcp-qhm5](https://github.com/advisories/GHSA-jhhc-3hcp-qhm5), CVE-2026-49452 | Not supplied; listed range <=68.1 |
| WeasyPrint 62.0 | [GHSA-jf6q-chmf-3h3v](https://github.com/advisories/GHSA-jf6q-chmf-3h3v), CVE-2026-55073 | 70.0 |
| WeasyPrint 62.0 | [GHSA-r543-q48m-4c9j](https://github.com/advisories/GHSA-r543-q48m-4c9j), CVE-2026-106443 | 70.0 |

Range review also included advisories not matched at the old earliest version:
[MCP GHSA-hvrp-rf83-w775](https://github.com/advisories/GHSA-hvrp-rf83-w775)
affects 1.23.0–1.27.1, fixed 1.27.2;
[Starlette GHSA-7f5h-v6xp-fcq8](https://github.com/advisories/GHSA-7f5h-v6xp-fcq8)
affects 0.39.0–0.49.0, fixed 0.49.1. The new floors exclude these intermediate
affected ranges too. A scan of only one old minimum cannot find all such cases.

### Additional transitive findings in the old lowest-direct sample

qdrant-client 1.7.0 forced `urllib3<2`; FastEmbed 0.7.x forced `Pillow<12`
on supported Python 3.11/3.12. Thus these older direct selections could not
resolve currently patched transitive releases. The revised client/FastEmbed
floors remove those upper constraints; the reviewed sample resolves urllib3
2.8.0 and Pillow 12.3.0. This **permits** safe resolution, but does not force
all existing environments to upgrade those transitives.

| Affected version audited | Advisory / CVE | Reported first patched version |
|---|---|---|
| urllib3 1.26.20 | [GHSA-pq67-6m6q-mj2v](https://github.com/advisories/GHSA-pq67-6m6q-mj2v), CVE-2025-50181 | 2.5.0 |
| urllib3 1.26.20 | [GHSA-gm62-xv2j-4w53](https://github.com/advisories/GHSA-gm62-xv2j-4w53), CVE-2025-66418 | 2.6.0 |
| urllib3 1.26.20 | [GHSA-2xpw-w6gg-jr37](https://github.com/advisories/GHSA-2xpw-w6gg-jr37), CVE-2025-66471 | 2.6.0 |
| urllib3 1.26.20 | [GHSA-38jv-5279-wg99](https://github.com/advisories/GHSA-38jv-5279-wg99), CVE-2026-21441 | 2.6.3 |
| urllib3 1.26.20 | [GHSA-qccp-gfcp-xxvc](https://github.com/advisories/GHSA-qccp-gfcp-xxvc), CVE-2026-44431 | 2.7.0 |
| urllib3 1.26.20 | [GHSA-vxq7-64xx-v4gw](https://github.com/advisories/GHSA-vxq7-64xx-v4gw), CVE-2026-97689 | 2.8.0 |
| urllib3 1.26.20 | [GHSA-8988-9cw3-xx77](https://github.com/advisories/GHSA-8988-9cw3-xx77), CVE-2026-97687 | 2.8.0 |
| Pillow 11.3.0 | [GHSA-cfh3-3jmp-rvhc](https://github.com/advisories/GHSA-cfh3-3jmp-rvhc), CVE-2026-25990 | 12.1.1 |
| Pillow 11.3.0 | [GHSA-whj4-6x5x-4v2j](https://github.com/advisories/GHSA-whj4-6x5x-4v2j), CVE-2026-40192 | 12.2.0 |
| Pillow 11.3.0 | [GHSA-wjx4-4jcj-g98j](https://github.com/advisories/GHSA-wjx4-4jcj-g98j), CVE-2026-42308 | 12.2.0 |
| Pillow 11.3.0 | [GHSA-5xmw-vc9v-4wf2](https://github.com/advisories/GHSA-5xmw-vc9v-4wf2), CVE-2026-42309 | 12.2.0 |
| Pillow 11.3.0 | [GHSA-r73j-pqj5-w3x7](https://github.com/advisories/GHSA-r73j-pqj5-w3x7), CVE-2026-42310 | 12.2.0 |
| Pillow 11.3.0 | [GHSA-pwv6-vv43-88gr](https://github.com/advisories/GHSA-pwv6-vv43-88gr), CVE-2026-42311 | 12.2.0 |
| Pillow 11.3.0 | [GHSA-8v84-f9pq-wr9x](https://github.com/advisories/GHSA-8v84-f9pq-wr9x), CVE-2026-54059 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-45hq-cxwh-f6vc](https://github.com/advisories/GHSA-45hq-cxwh-f6vc), CVE-2026-55379 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-4x4j-2g7c-83w6](https://github.com/advisories/GHSA-4x4j-2g7c-83w6), CVE-2026-55798 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-phj9-mv4w-65pm](https://github.com/advisories/GHSA-phj9-mv4w-65pm), CVE-2026-55380 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-5x94-69rx-g8h2](https://github.com/advisories/GHSA-5x94-69rx-g8h2), CVE-2026-54060 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-9hw9-ch79-4vh6](https://github.com/advisories/GHSA-9hw9-ch79-4vh6), CVE-2026-59205 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-6r8x-57c9-28j4](https://github.com/advisories/GHSA-6r8x-57c9-28j4), CVE-2026-59199 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-62p4-gmf7-7g93](https://github.com/advisories/GHSA-62p4-gmf7-7g93), CVE-2026-54058 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-xj96-63gp-2gmr](https://github.com/advisories/GHSA-xj96-63gp-2gmr), CVE-2026-59197 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-fj7v-r99m-22gq](https://github.com/advisories/GHSA-fj7v-r99m-22gq), CVE-2026-59198 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-jjj6-mw9f-p565](https://github.com/advisories/GHSA-jjj6-mw9f-p565), CVE-2026-59200 | 12.3.0 |
| Pillow 11.3.0 | [GHSA-vjc4-5qp5-m44j](https://github.com/advisories/GHSA-vjc4-5qp5-m44j), CVE-2026-59204 | 12.3.0 |

## Exposure and compatibility decisions

- MCP runs over stdio here, not its HTTP/WebSocket server transports. Some MCP
  advisories therefore concern unused features; upgrading the declared floor is
  still prudent. Dashboard Host/Origin guards remain an independent control.
- Several Starlette issues concern forms, static files on Windows or class-based
  endpoints not used here. None of those observations justify keeping a known
  affected framework as a supported dashboard minimum.
- Jinja sandbox/compiler advisories do not establish exploitability of the
  report renderer, which assembles escaped HTML rather than accepting templates.
- Report rendering already escapes values and supplies a deny-all URL loader.
  It does not enable presentational hints or forward `stylesheets`/`xmp_metadata`
  arguments. The WeasyPrint SSRF/loader/EPS advisories are still reason to stop
  supporting old releases. The hints advisory supplies no fixed version;
  its listed range excludes 70.0, but that is not an independent proof of a fix.
- **Qdrant mapping caveat:** GHSA-7m75-x27w-r52r is attached to the Python client
  by the advisory provider, but describes the **Qdrant server's snapshot recovery**
  and a server-side commit. Updating this client does **not** patch a deployed
  Qdrant server. Independently update/audit that service. The client floor change
  is additionally justified by its old forced urllib3<2 constraint.
- FastEmbed 0.8.0 allows Pillow 12.x; it is not itself reported vulnerable in
  these scans. Existing environments must explicitly update/audit Pillow rather
  than assuming a FastEmbed upgrade updates all already-satisfied dependencies.
- pytest-asyncio 0.23.0 actually failed collection with pytest 9.0.3
  (`AttributeError: 'Package' object has no attribute 'obj'`). The 1.3.0 floor is
  a tested compatibility companion to pytest's 9.0.3 security floor, not an
  invented advisory for the plugin.
- Actual minimum-version compatibility checks used MCP 1.28.1, Starlette 1.3.1,
  Jinja2 3.1.6, WeasyPrint 70.0, pytest 9.0.3 and pytest-asyncio 1.3.0 with the
  other installed test dependencies. Vector tests separately used real
  qdrant-client 1.9.0 / FastEmbed 0.8.0 wheels, with embedding/service operations
  mocked and network connections disabled. No embedding model was downloaded.

## Browser assets

`docs/dashboard-assets.json` records the exact URLs, byte lengths and SHA-384
SRI digests used in the HTML. Leaflet stays 1.9.4; markercluster stays 1.5.3;
DOMPurify is pinned from floating `@3` to **3.4.17**, the npm registry's reviewed
latest release. The DOMPurify API/configuration used by `safe()` is unchanged.

Each of the five JS/CSS bodies was downloaded twice with identity encoding;
the copies matched. Each also matched `package/dist/...` inside the exact npm
release tarball; each tarball's `dist.integrity` SHA-512 was verified against
registry metadata. Both CDNs returned `Access-Control-Allow-Origin: *`, allowing
the HTML's `crossorigin="anonymous"` SRI requests. These are verified hashes of
the fetched bytes, **not** independent proof that the publisher or registry is
trustworthy. No npm lifecycle scripts were run and no assets were vendored.

OSV returned no matches for DOMPurify 3.4.17, Leaflet 1.9.4 or markercluster
1.5.3. GitHub's DOMPurify advisory list was also reviewed, including
[GHSA-6688-9rhm-gjv2](https://github.com/advisories/GHSA-6688-9rhm-gjv2)
(<=3.4.15, patched 3.4.16),
[GHSA-c2j3-45gr-mqc4](https://github.com/advisories/GHSA-c2j3-45gr-mqc4)
(<=3.4.11, patched 3.4.12), and
[GHSA-v2wj-7wpq-c8vv](https://github.com/advisories/GHSA-v2wj-7wpq-c8vv)
(3.1.3–3.3.1, patched 3.3.2). Some advisories require IN_PLACE/hooks/DOM modes
not used here. The old floating URL does not reveal which historical version a
particular browser actually loaded; it is not evidence of a specific exploit.

Google Fonts' client-dependent CSS cannot have a stable cross-client SRI digest.
It is retained, explicitly excepted in the offline policy test, to preserve
appearance. That CSS and subordinate font files remain external trust/privacy
dependencies. Leaflet CSS images and map tiles are also **not covered by the
parent stylesheet's SRI**. Exact versions/SRI fail closed on changed CDN bytes
but do not provide offline availability, cover subresources, or protect against
a compromised publisher at the time the approved hash was chosen.

## Reproducing and updating

Use disposable environments and public indexes; do not run a resolver that builds
unreviewed sdists merely to audit metadata. Example (uv is audit tooling here,
not a newly required project package manager):

```bash
uv venv /tmp/opencode/dependency-audit --python 3.12
uv pip install --python /tmp/opencode/dependency-audit/bin/python \
  --only-binary=:all: --default-index https://pypi.org/simple pip-audit==2.10.1
uv pip compile pyproject.toml --all-extras --only-binary=:all: \
  --python-version 3.12 --default-index https://pypi.org/simple --no-annotate --no-header \
  -o /tmp/opencode/resolved-all.txt
/tmp/opencode/dependency-audit/bin/pip-audit \
  -r /tmp/opencode/resolved-all.txt --no-deps --disable-pip --strict \
  --aliases on -f json -o /tmp/opencode/audit.json
```

Repeat resolution for Python 3.11, and with `--resolution lowest-direct` as a
separate sample. For installed inventories, collect exact public distribution
names/versions via `importlib.metadata`, excluding the local project, then pass
that inventory to the same scanner. Do not substitute the scanner environment
for the actual target environment. Recheck direct declared minima individually;
they are not a jointly installable lock. Review advisory IDs, aliases, ranges,
feature reachability and transitive constraints before changing a minimum.

For a CDN update: choose an exact release, review its advisories, fetch exact
files twice, verify against the npm release archive and registry digest, compute
`sha384-` plus Base64(SHA384(file bytes)), update both HTML and asset manifest,
then run offline asset tests. Re-review the explicitly unhashed Google Fonts
exception and subordinate resource trust. Never copy an unverified SRI string
from a suggestion or assume a package tarball hash is the asset's SRI hash.

No uv lockfile or repository constraints file is introduced: existing CI uses
pip and `CONTRIBUTING.md` explicitly makes pyproject the source of truth. CI now
requests the dashboard extra it tests. Reproducible production deployments
should separately retain a reviewed, platform/Python/extras-specific hashed
resolution and rescan it on updates. Adding a repository-wide deployment lock
or changing package managers needs a separate maintainer decision.

## Limits and follow-up

- **Pre-existing vector compatibility issue:** the latest resolved
  qdrant-client 1.19.1 wheel's public client has no `search()` method
  (`query_points()` exists), confirmed by source and offline runtime class
  inspection, while `vector_store.py` calls `client.search()`.
  The prior range already permitted this release. The tested 1.9.0 minimum
  retains that API. A reviewed compatible deployment constraint or separate
  vector API migration is required; a vulnerability-clean latest resolution
  must not be treated as a fully tested vector deployment. No new upper cap or
  runtime API migration is imposed by this audit.
- Scan the **deployed** environment after upgrading, not just these temporary
  inventories. Already-installed vulnerable transitives may remain satisfiable.
- This audit does not prove that **every** graph admitted by the ranges is safe;
  lowest-direct is one graph, not lowest-transitive or exhaustive resolution.
  The base MCP dependency can also bring Starlette without the dashboard extra's
  explicit floor. Audit all installed transitives even for stdio-only installs.
- PyPI/OSV/GitHub advisories can be incomplete, stale, duplicated or misattributed.
  No malware/source-code, native-library, OS-package, Qdrant/Ollama server,
  embedding-model or real service integration audit was performed.
- Resolution coverage is Linux CPython 3.11/3.12, not all Python versions,
  platforms, accelerators or optional native runtimes. PDF tests require native
  Pango/font libraries; pure Python dependency resolution does not audit them.
- Pinning, SRI, hashed resolutions and clean scanner output do not eliminate
  supply-chain risk. Keep monitoring advisories and reviewing updates.

## Validation completed

- Existing test environment: **1,496 passed**, 18 live smoke tests deselected.
- Revised security-minimum environment: **1,496 passed**, 18 smoke tests
  deselected (MCP/Starlette/Jinja/WeasyPrint/pytest/plugin versions listed above).
- Vector-minimum environment: **63 passed** with socket/DNS connections disabled;
  real client/embedding constructor signatures were inspected without loading
  models. These are mock-backed service tests, not real Qdrant integration.
- The wheel built successfully using the local project and a wheel-installed
  Hatchling backend; its dashboard HTML exactly matched the source, including
  all five integrity digests, and its metadata contained the revised floors.
- Offline asset tests enforce the exact reviewed assets, SHA-384 format,
  anonymous CORS and the single documented Google Fonts stylesheet exception.
  No visual browser/pixel comparison or live CDN rendering test was performed.
