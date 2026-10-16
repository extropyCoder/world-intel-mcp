![Phoenix Intelligence Dashboard](docs/dashboard.png)

# World Intelligence MCP Server

[![MCP](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-green)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

Real-time global intelligence across **30+ domains** with **139 MCP tools**, a live ops-center dashboard, a CLI, and a **Qdrant vector store** for enterprise-grade semantic search across accumulated intelligence. All data comes from free, public APIs: no paid subscriptions required.

Built for AI agents that need world awareness: market conditions, geopolitical risk, military posture, aircraft-cohort early warning, refugee and migration flows, supply chain disruptions, cyber threats, and more — all queryable via the Model Context Protocol. The vector store enables natural language queries like *"military activity near Taiwan"* or *"cyber threats targeting healthcare"* across all historical data.

---

## What You Get

| Domain | Tools | Data Sources |
|--------|-------|-------------|
| **Financial Markets** | 7 | Yahoo Finance, CoinGecko, Alternative.me, Mempool |
| **Forex & Currency** | 3 | ECB/Frankfurter (8 major pairs, timeseries, cross rates) |
| **Bonds & Yields** | 2 | FRED, Yahoo Finance (yield curve, bond ETFs, spread analysis) |
| **Earnings** | 2 | Yahoo Finance (mega-cap calendar, surprise history) |
| **SEC Filings** | 3 | SEC EDGAR (full-text search, company filings, 8-K material events) |
| **Company Enrichment** | 1 | Yahoo Finance + GDELT + SEC + GitHub (composite profile) |
| **Macro Composite** | 1 | Weighted 6-signal market verdict (Fear&Greed, VIX, sectors, DXY, BTC, yields) |
| **Economic Indicators** | 6 | AAA fuel prices, EIA energy, FRED macro, World Bank |
| **Central Banks** | 1 | 15 central bank policy rates |
| **BTC Technicals** | 1 | SMA 50/200, golden/death cross, Mayer Multiple |
| **Natural Disasters** | 2 | USGS earthquakes, NASA FIRMS wildfires |
| **Environmental** | 2 | NASA EONET, GDACS disaster alerts |
| **Climate** | 1 | Open-Meteo temperature/precipitation anomalies |
| **Conflict & Security** | 4 | ACLED events, UCDP, unrest detection, humanitarian data |
| **Military & Defense** | 6 | adsb.lol, OpenSky, hexdb.io, surge detection, theater posture, aircraft batch |
| **Infrastructure** | 5 | Cloudflare Radar, submarine cables, cascade analysis, cloud status, RIPEstat BGP/RPKI |
| **Maritime** | 2 | NGA navigation warnings, vessel snapshots |
| **Aviation** | 5 | FAA airport delays, domestic flight snapshot, business-jet/military cohort early warning (adsb.lol live + globe_history heatmaps) |
| **News & Media** | 3 | 119 RSS feeds (4-tier), GDELT, trending keywords |
| **Intelligence Analysis** | 8 | Signal convergence, focal points, instability index, risk scores, escalation |
| **NLP Intelligence** | 4 | Entity extraction, event classification, news clustering, keyword spikes |
| **Strategic Synthesis** | 4 | Strategic posture, world brief, fleet report, population exposure |
| **Geospatial** | 11 | Military bases, ports, pipelines, nuclear facilities, cables, datacenters, spaceports, minerals, exchanges, trade routes, cloud regions |
| **AI & Technology** | 4 | arXiv papers, HuggingFace models, Hacker News, GitHub trending |
| **Cyber Threats** | 1 | URLhaus, Feodotracker, CISA KEV, SANS |
| **Health** | 1 | WHO DON, ProMED, CIDRAP disease outbreaks |
| **Space Weather** | 1 | NOAA SWPC (Kp index, solar flares, alerts) |
| **Social & Sanctions** | 3 | Reddit velocity, OFAC SDN list, nuclear test site monitoring |
| **Country Intelligence** | 3 | Country brief, country stocks, financial centers |
| **Prediction Markets** | 1 | Polymarket event contracts |
| **Elections** | 1 | Global election calendar with risk scoring |
| **Displacement and Migration** | 5 | UNHCR refugee/IDP data, origin -> destination corridors since 1951, year-over-year surge detection; World Bank net migration and migrant stock |
| **Shipping** | 1 | Dry bulk shipping stress index |
| **Government** | 1 | USAspending.gov federal contracts |
| **Traffic** | 2 | Road traffic flow, real-time incidents |
| **Cross-Domain Alerts** | 2 | Alert digest, weekly trends |
| **Monitoring** | 2 | Webcams, server health/status |
| **Vector Search** | 5 | Qdrant semantic search, similarity, timeline, stats |
| **Cross-Domain Analytics** | 3 | Correlation, domain summary, trend detection |
| **Reports** | 1 | PDF/HTML multi-domain intelligence reports |
| **Daily Digest** | 1 | Cited markdown morning brief: top events, headlines, trends, and timeline |
| **AOI Geofences** | 10 | User-defined areas of interest in three shapes (circle, polygon, corridor): define/list/update/delete, a cited multi-domain brief, hotspot escalation scoring, and enter/leave change detection for a user's own area. News is scoped to the place the area sits in (OSM Nominatim reverse geocoding, no key), not to the area's name |
| **Severe Weather** | 2 | NWS active CAP alerts (US), Meteoalarm (39 EU countries) |
| **Space Launches** | 1 | Launch Library 2 upcoming launches |
| **Volcanoes** | 1 | Smithsonian GVP weekly activity report |
| **Tropical Cyclones** | 2 | NHC (Atlantic, E/C Pacific) + JTWC (NW Pacific, N Indian, S Hemisphere) |
| **Situation Brief** | 1 | Cited situational awareness brief over MCP: bounded server-side overview synthesized via local Ollama, with a mechanically-cited fallback |

**Total: 139 tools** across 30+ intelligence domains.

---

## Quick Start

### Install

```bash
git clone https://github.com/marc-shade/world-intel-mcp.git
cd world-intel-mcp
pip install -e .

# Optional extras
pip install -e ".[dashboard]"  # Live ops-center dashboard
pip install -e ".[vector]"     # Qdrant vector store + FastEmbed
pip install -e ".[dev]"        # pytest, respx, coverage
```

### Run as MCP Server

```bash
world-intel-mcp  # stdio mode for Claude Code, Cursor, etc.
```

### Claude Code Configuration

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "world-intel-mcp": {
      "command": "world-intel-mcp"
    }
  }
}
```

### Dashboard

The light basemap uses standard OpenStreetMap tiles, with visible contributor
attribution and no API key. Use the public tile service for light interactive
viewing in accordance with its [tile usage policy](https://operations.osmfoundation.org/policies/tiles/),
not bulk downloading or prefetching. Browsers request tiles directly, so the
provider sees the browser client's IP address, even when the dashboard is reached
through an SSH tunnel. Availability and usage/rate policies apply; the public
service is not a production SLA.

```bash
intel-dashboard              # http://localhost:8501
intel-dashboard --port 9000  # custom port
```

The dashboard binds to `127.0.0.1` by default. For a remote dashboard, keep
that bind and use an SSH local forward, for example:

```bash
ssh -N -L 127.0.0.1:18501:127.0.0.1:8501 user@remote-host
# Open http://localhost:18501 (or http://127.0.0.1:18501)
```

Only `localhost` and `127.0.0.1` Host names are accepted, including custom
forwarded ports. Requests with an Origin must match the page's scheme,
hostname and effective port exactly; the two loopback aliases are not
interchangeable. Cross-origin access is not enabled. This is **browser
isolation, not local-process authentication**: local programs can still access
the dashboard. Do not expose it publicly. The launcher disables proxy-header
trust; if running Uvicorn directly, also pass `--no-proxy-headers` so forwarded
headers cannot rewrite the request scheme.

Dashboard `POST /api/vector-search` accepts a JSON object of at most 16 KiB:
`query` is a nonempty string (at most 2,048 characters before trimming),
`limit` is an integer from 1–50 (default 20), and optional `hours` is a finite
JSON number greater than 0 and at most 8,760 (or `null` for no time filter).
Optional `category` is an exact-match string of at most 128 characters; custom
categories are supported, and `null` or `""` means no category filter. Numeric
strings and booleans are not accepted as numbers. Invalid input returns 400,
oversized bodies 413, unavailable vector storage 503, and internal search
failures a generic 500. Validation happens before request-time initialization.
These limits apply to the dashboard endpoint, not the MCP vector-search API.

### Headless smoke run + local browser (scripted)

**On the remote/headless Linux machine**, from this checkout:

```bash
./scripts/smoke-test.sh
```

This explicitly performs **live public-network checks and PyPI installation**.
It creates a new private temporary venv/state directory on every run, installs
only this project's `dashboard,dev` extras (plus a dotenv version supporting
disable mode), then runs just the existing earthquake, forex and service-status
smoke tests. Additional checks require fresh non-stale/error-free data, plausible
earthquake/rate fields and valid status feeds from every configured provider.
Quiet status feeds may have zero incidents; unavailable feeds fail the run.
No credentials are needed. An upstream outage is a nonzero failure, not a pass
with empty/stale data. No other live smoke tests are selected.

The script starts a background dashboard on **127.0.0.1:8501 only**, verifies the
actual child PID's Linux `/proc` listener, health/root responses, hostile Host
400, hostile Origin 403, same-origin 200, and absence of wildcard CORS. It then
prints PID, private log path, RSS and cumulative CPU time (not resource limits),
and stays in the foreground. **Keep that terminal open. Ctrl+C stops its child
processes and removes its private venv/cache/logs** (normal interrupt exit 130).
It never kills an existing listener; choose another port if 8501 is occupied.

**On your local browser machine**, use a checkout or copy of the standalone
tunnel helper:

```bash
./scripts/smoke-tunnel.sh
```

Answer its SSH `user@host`/host-alias, remote-port (8501), and local-port (18501)
prompts. Complete normal SSH authentication/host-key verification, then open the
printed **http://127.0.0.1:18501/** URL. Ctrl+C closes the local tunnel; stop the
remote script separately. A headless server cannot initiate a local browser's
SSH forwarding session for you: these are two invocations on two machines.
No public bind or automatic host-key acceptance is used. IPv6 SSH destinations
can be configured behind a trusted SSH host alias; the helper accepts plain
hostnames, IPv4 addresses and aliases, not SSH URLs or embedded options.

Options (no extra setup commands required):

- Remote: `./scripts/smoke-test.sh --checks-only` runs the same live checks,
  stops the dashboard and cleans up immediately; success exits 0.
- Remote: `./scripts/smoke-test.sh --port 8502` uses another loopback port.
- Local: `./scripts/smoke-tunnel.sh user@host --remote-port 8502 --local-port 18502`.
- Select an **already installed executable**, not a shell command, with
  `SMOKE_PYTHON=/path/to/python3.12 ./scripts/smoke-test.sh`.

Prerequisites: remote Linux with readable `/proc`, Bash, an installed Python
**>=3.11**, sufficient temporary disk space, and access to public PyPI and the
selected APIs; local Bash, OpenSSH client and browser, plus SSH access to the
remote machine. No curl, desktop browser or GUI is needed remotely.

**Missing `ensurepip`/venv on Debian or Ubuntu:** rerun the same smoke script in
your terminal. Before creating app state, it checks the **selected interpreter**
with `-I`, then offers a **default-NO** prompt for its exact OS venv package (for
Python 3.14, `python3.14-venv`). Answer `yes` to let the script perform that step;
sudo may ask for your password. **Do not run the repository script with sudo.**
Only a root-owned system `apt-get` executable is elevated, with fixed argument
arrays; root users must confirm too. Help, noninteractive runs, blank answers
and declined prompts never install anything.

After confirmation, the script reads the existing APT candidate and simulates
the plan before installing. It allows only the matching venv package and missing
pip/setuptools/wheel archives; interpreter installation, unrelated packages,
upgrades and removals are refused. It does **not** refresh APT indexes, add a
repository/PPA, install Python, or run pip/repository code as root. If the exact
package has no candidate, sudo is unavailable, or the plan is outside that scope,
it stops with an actionable explanation. Administrators must resolve those cases;
the script does not expand its privileges or installation scope automatically.
The selected interpreter is rechecked after installation and is never silently
switched. Ctrl+C forwards an interrupt and waits for APT to finish/stop, rather
than orphaning or force-killing a privileged package transaction.

Python **3.14 is not fully end-to-end smoke-tested here**. Installing its venv
support does not guarantee availability of all third-party binary wheels. If a
later wheel/install step is blocked, explicitly select an already installed
supported interpreter, e.g. `SMOKE_PYTHON=/path/to/python3.12`; no older Python is
auto-selected and no alternate interpreter is installed for you.

Setup stages have finite deadlines (venv 120s, wheel install 600s, selected tests
180s, stronger checks 90s with a 60s async budget); readiness is bounded to 30s
with individually bounded probes. The existing tests retain their bounded
Fetcher retries; stronger source checks make one attempt. Installation resolves
current allowed dependency ranges, not a locked/reproducible deployment. Child
cleanup allows up to five seconds of graceful shutdown before killing its owned
process group; it does not terminate unrelated listeners or services.

Isolation/limits: apart from the explicitly approved OS prerequisite step above,
no existing venv or user packages are changed. Private state
uses a fresh UID-owned 0700 directory, checked for unsafe ancestors/symlinks;
`WORLD_INTEL_CACHE_DB`, HOME, pip configuration and caches all point into it.
Production AOIs/caches, inherited API keys, proxy/CA settings and webhook variables
are not reused. `PYTHON_DOTENV_DISABLED=1` is **tested before app import**, so a
repository `.env` cannot inject production settings. Logs are private and failure
tails receive best-effort URL/credential-label redaction, not universal free-text
secret detection. Temporary state is removed on failure/exit as well as success.
This is process/configuration isolation, not an OS sandbox: use a trusted checkout,
Python executable and package registry. SSH hangup/termination also triggers child
cleanup; abrupt SIGKILL, kernel/host failure or power loss cannot run cleanup.

No collector, historical baseline tool, Qdrant service, PDF/vector SDK or model
package is installed or launched. The existing `OLLAMA_API_URL` setting is set
to an intentionally unsupported scheme, forcing the app's existing brief fallback
without contacting an already running Ollama server. This is not a new app-wide
feature toggle: the dashboard can still initialize its optional vector worker,
but without its SDK/model dependencies it cannot populate vector storage. Opening
the browser starts the dashboard's normal broader public-source requests and
external assets/map tiles; the initial three-source check is not full UI coverage.
Local SSH configuration is operator-trusted. These scripts do not expose a remote
browser, modify firewall/SSH-server settings, or establish production readiness.

### PDF/HTML Reports

```bash
pip install -e ".[pdf]"      # requires: brew install pango (macOS)
intel report                 # full PDF report → ~/.cache/world-intel-mcp/
intel report --format html   # HTML (no native deps needed)
intel report -o brief.pdf    # custom output path
intel report -s markets,cyber,earthquakes  # select sections
```

Map-first ops center: Leaflet map with toggle-able layers (quakes, military, conflict, fires, convergence, nuclear, infrastructure, your AOI geofences with last-sweep counts, UNHCR migration corridors between capitals), 47 live SSE feeds, HUD bar, glassmorphic panels, per-source circuit breaker health.

### CLI

```bash
intel markets              # stock indices
intel earthquakes --min-mag 5.0
intel migration-surges     # year-over-year displacement jumps (UNHCR)
intel migration-flows -o Sudan
intel status               # cache + circuit breaker health
```

---

## Architecture

```
server.py     (MCP stdio) ─┐                                               ┌─ VectorStore (Qdrant)
cli.py        (Click CLI)  ├─> sources/*.py ─> Fetcher ─> CircuitBreaker ─┤
dashboard.py  (SSE)        │    analysis/*.py                              └─ Cache (SQLite)
collector.py  (daemon)    ─┘
```

- **Fetcher**: Centralized async HTTP client (httpx). Retries, per-source rate limiting, stale-data fallback. Auto-stores results in vector store on fresh fetches.
- **CircuitBreaker**: Per-source tracking. 3 consecutive failures trips for 5 minutes. Each RSS feed gets its own breaker.
- **Cache**: SQLite WAL-mode TTL cache. `get()` returns live data, `get_stale()` returns expired data for fallback.
- **VectorStore**: Qdrant + FastEmbed (BAAI/bge-small-en-v1.5, 384-dim). Async background worker queue for non-blocking storage. Enables semantic search across all accumulated intelligence.
- **Collector**: Standalone daemon that fetches all 55 sources in parallel and populates the vector store. Run once or as a daemon (default: 5-minute interval). Includes the AOI change sweep, so a running daemon continuously watches every defined geofence.
- **Sources** (`sources/*.py`): 30+ modules, each exports `async def fetch_*(fetcher, **kwargs) -> dict`.
- **Analysis** (`analysis/*.py`): Cross-domain synthesis — signal aggregation, instability indexing, NLP, company enrichment, macro composite.
- **Config** (`config/*.py`): Curated datasets — 22 hotspots, 70+ bases, 40 ports, 24 pipelines, 24 nuclear facilities, 34 cables, 48 datacenters, 27 spaceports, 82 exchanges.

---

## MCP Tools Reference

### Financial Markets (7)
| Tool | Description |
|------|-------------|
| `intel_market_quotes` | Stock index quotes (S&P 500, Dow, Nasdaq, FTSE, Nikkei) |
| `intel_crypto_quotes` | Top crypto prices and market caps from CoinGecko |
| `intel_stablecoin_status` | Stablecoin peg health (USDT, USDC, DAI, FDUSD) |
| `intel_etf_flows` | Bitcoin spot ETF prices and volumes |
| `intel_sector_heatmap` | US equity sector performance (11 SPDR ETFs) |
| `intel_macro_signals` | 7 macro indicators (Fear & Greed, VIX, DXY, gold, 10Y, BTC) |
| `intel_commodity_quotes` | Commodity futures (gold, silver, crude, natgas, grains) |

### Forex & Currency (3)
| Tool | Description |
|------|-------------|
| `intel_forex_rates` | Latest FX rates from ECB. Filter by base/target currencies |
| `intel_forex_timeseries` | Historical FX rate with trend analysis (configurable days) |
| `intel_major_crosses` | All 8 major pairs + cross rates + DXY proxy |

### Bonds & Yields (2)
| Tool | Description |
|------|-------------|
| `intel_yield_curve` | US Treasury yield curve (2Y-30Y), 2s10s/3m10y spreads, inversion flag |
| `intel_bond_indices` | Bond ETFs: AGG, TLT, HYG, LQD, TIP with price/change |

### Earnings (2)
| Tool | Description |
|------|-------------|
| `intel_earnings_calendar` | Upcoming earnings for 20 mega-cap stocks with EPS estimates |
| `intel_earnings_surprise` | Historical earnings surprise (actual vs estimate, trend) |

### SEC Filings (3)
| Tool | Description |
|------|-------------|
| `intel_sec_filings` | Full-text search across all EDGAR filings |
| `intel_company_filings` | Company filings by ticker (10-K, 10-Q, 8-K) with CIK resolution |
| `intel_recent_8k` | Latest 8-K material events (M&A, exec changes, earnings) |

### Company Enrichment (1)
| Tool | Description |
|------|-------------|
| `intel_company_profile` | Composite profile: stock quote + financials + news + SEC + GitHub |

### Macro Composite (1)
| Tool | Description |
|------|-------------|
| `intel_macro_composite` | Weighted market score (0-100) with verdict: RISK_ON to STRONG_CAUTION |

### Economic (6)
| Tool | Description |
|------|-------------|
| `intel_gas_prices` | Daily US retail gasoline, diesel, and E85 prices from AAA |
| `intel_residential_natgas` | US residential natural gas prices from EIA |
| `intel_electricity_rates` | US electricity retail rates by sector/state from EIA |
| `intel_energy_prices` | Brent/WTI crude oil and natural gas from EIA |
| `intel_fred_series` | FRED economic data (GDP, CPI, unemployment, rates) |
| `intel_world_bank_indicators` | World Bank development indicators by country |

### Central Banks (1)
| Tool | Description |
|------|-------------|
| `intel_central_bank_rates` | Policy rates for 15 major central banks |

### BTC Technicals (1)
| Tool | Description |
|------|-------------|
| `intel_btc_technicals` | Bitcoin SMA 50/200, golden/death cross, Mayer Multiple |

### Natural Disasters (2)
| Tool | Description |
|------|-------------|
| `intel_earthquakes` | USGS earthquakes (configurable magnitude/time/limit) |
| `intel_wildfires` | NASA FIRMS satellite fire hotspots (9 global regions) |

### Environmental (2)
| Tool | Description |
|------|-------------|
| `intel_environmental_events` | NASA EONET natural events |
| `intel_disaster_alerts` | GDACS disaster alerts with severity scoring |

### Conflict & Security (4)
| Tool | Description |
|------|-------------|
| `intel_acled_events` | ACLED armed conflict events |
| `intel_ucdp_events` | Uppsala Conflict Data Program events |
| `intel_unrest_events` | Social unrest with Haversine dedup |
| `intel_humanitarian_summary` | HDX humanitarian crisis datasets |

### Military & Defense (6)
| Tool | Description |
|------|-------------|
| `intel_military_flights` | Military aircraft via adsb.lol (OpenSky fallback) |
| `intel_theater_posture` | Activity across 5 theaters (EU, Indo-Pacific, ME, Arctic, Korea) |
| `intel_aircraft_details` | Aircraft lookup by ICAO24 hex (hexdb.io) |
| `intel_aircraft_batch` | Batch aircraft lookup (multiple hex codes) |
| `intel_military_surge` | Foreign aircraft concentration anomaly detection |
| `intel_usni_fleet` | USNI News naval fleet tracker |

### Infrastructure (5)
| Tool | Description |
|------|-------------|
| `intel_internet_outages` | Cloudflare Radar internet disruptions |
| `intel_cable_health` | Submarine cable corridor health |
| `intel_cascade_analysis` | Infrastructure cascade simulation |
| `intel_service_status` | Cloud platform health (AWS, Azure, GCP, Cloudflare, GitHub) |
| `intel_bgp_status` | RIPEstat BGP status for one prefix or ASN: RIS peer visibility, origin ASNs, RPKI validity (a per-resource health check, not global hijack detection) |

### Maritime (2)
| Tool | Description |
|------|-------------|
| `intel_nav_warnings` | NGA maritime navigation warnings |
| `intel_vessel_snapshot` | Naval activity at 9 strategic waterways |

### Geospatial Datasets (10)
| Tool | Description |
|------|-------------|
| `intel_military_bases` | 70 military bases from 9 operators |
| `intel_strategic_ports` | 40 strategic ports across 6 types |
| `intel_pipelines` | 24 oil/gas/hydrogen pipelines |
| `intel_nuclear_facilities` | 24 nuclear power/enrichment/research facilities |
| `intel_undersea_cables` | 34 submarine communications cables |
| `intel_ai_datacenters` | 48 AI/HPC datacenters worldwide |
| `intel_spaceports` | 27 global spaceports |
| `intel_critical_minerals` | 27 strategic mineral deposits |
| `intel_stock_exchanges` | 82 stock exchanges worldwide |
| `intel_trade_routes` | Major trade routes and chokepoints |

### News & Media (3)
| Tool | Description |
|------|-------------|
| `intel_news_feed` | 119 global RSS feeds with 4-tier source ranking |
| `intel_trending_keywords` | Trending terms with spike detection |
| `intel_gdelt_search` | GDELT 2.0 global news search |

### Intelligence Analysis (8)
| Tool | Description |
|------|-------------|
| `intel_signal_convergence` | Geographic convergence of multi-domain signals |
| `intel_focal_points` | Multi-signal focal point detection |
| `intel_signal_summary` | Country-level signal aggregation |
| `intel_temporal_anomalies` | Activity deviations from baselines |
| `intel_instability_index` | Country Instability Index v2 (0-100) |
| `intel_risk_scores` | ACLED-based conflict risk scoring |
| `intel_hotspot_escalation` | Escalation scores for 22 intel hotspots |
| `intel_country_dossier` | Comprehensive country intelligence dossier |

### NLP Intelligence (4)
| Tool | Description |
|------|-------------|
| `intel_extract_entities` | Named entity extraction (countries, leaders, orgs, CVEs, APTs) |
| `intel_classify_event` | Event classification into 14 threat categories |
| `intel_news_clusters` | Topic clustering by Jaccard similarity |
| `intel_keyword_spikes` | Keyword spike detection with Welford's algorithm |

### Strategic Synthesis (4)
| Tool | Description |
|------|-------------|
| `intel_strategic_posture` | Composite global risk from 9 weighted domains |
| `intel_world_brief` | Structured daily intelligence summary |
| `intel_fleet_report` | Naval fleet activity report with readiness scoring |
| `intel_population_exposure` | Population at risk near active events (105-city dataset) |

### Climate (1)
| Tool | Description |
|------|-------------|
| `intel_climate_anomalies` | Open-Meteo temperature/precipitation anomalies |

### Prediction Markets (1)
| Tool | Description |
|------|-------------|
| `intel_prediction_markets` | Polymarket prediction contracts |

### Elections (1)
| Tool | Description |
|------|-------------|
| `intel_election_calendar` | Global election calendar with risk scoring |

### Displacement and migration (5)
| Tool | Description |
|------|-------------|
| `intel_displacement_summary` | UNHCR refugee/IDP statistics, top 30 origins |
| `intel_migration_flows` | UNHCR origin -> destination corridors (country or region filters), with change vs the prior year |
| `intel_migration_surges` | Largest year-over-year increases and decreases by origin and corridor, plus IDPs, sized against population |
| `intel_migration_history` | One country's yearly series since 1951 as an origin or a host, with its peak and largest waves |
| `intel_net_migration` | World Bank net migration and migrant stock: latest ranking, regions, or one country since 1960 |

### Aviation (5)
| Tool | Description |
|------|-------------|
| `intel_airport_delays` | FAA airport delay status |
| `intel_aviation_domestic` | Global air traffic snapshot from OpenSky |
| `intel_jet_cohort_snapshot` | Live airborne business-jet (ICAO type designators) or military cohort from adsb.lol; learns cohort membership as a side effect |
| `intel_jet_activity_anomaly` | Peak concurrent airborne cohort aircraft in a UTC half-hour vs the same slot on prior same-weekday dates (holiday-adjacent dates swapped out); robust z, alert level 1-5, deficit flag |
| `intel_jet_excess_origins` | Infers takeoffs/landings from heatmap tracks, density-clusters endpoints, and reports clusters above their baseline median: departure/arrival anchor, regional, or diffuse |

**Cohort early warning.** The three `intel_jet_*` tools watch a cohort of aircraft
(business jets by ICAO type designator, or military) for activity that departs
from its calendar baseline, and locate where an excess came from. Data:
adsb.lol's live API and its public half-hour `globe_history` heatmaps (readsb
format, ODbL 1.0, no key). Heatmaps are ~14 MB each and immutable once
published, so they are cached on disk next to the cache DB
(`WORLD_INTEL_HEATMAP_CACHE_MB`, default 1024) and fetched at most one at a
time with a 2 s floor. The cohort is *learned* from live snapshots (run
`intel_jet_cohort_snapshot` or the collector daemon, which now includes
`bizjet_cohort` and `military_cohort`); the analyses only use members first
seen `min_learning_lead_hours` (default 24) before the window, because a
cohort learned during a window inflates that window (measured: 1,002 vs a
206 baseline median when the gate is bypassed). Until the cohort has that
lead, the tools answer `warming_up`. The baseline is a handful of
same-weekday slots fetched on demand, not a year-long backfill, so there
are no learned holiday profiles or annual ratios.

Approach inspired by [kylemcdonald/ews](https://github.com/kylemcdonald/ews)
(Early Warning System: business-jet cohorts against calendar baselines, and
the excess-origin detector). This is an independent implementation over a
different, openly licensed data source; no ews code is used.

### Cyber Threats (1)
| Tool | Description |
|------|-------------|
| `intel_cyber_threats` | Aggregated cyber intel (URLhaus, CISA KEV, SANS) |

### Space Weather (1)
| Tool | Description |
|------|-------------|
| `intel_space_weather` | Solar activity (Kp index, X-ray flux, SWPC alerts) |

### AI & Technology (4)
| Tool | Description |
|------|-------------|
| `intel_ai_releases` | arXiv AI papers, HuggingFace models |
| `intel_hacker_news` | Hacker News top stories |
| `intel_trending_repos` | GitHub trending repositories |
| `intel_arxiv_papers` | arXiv paper search |

### Health (1)
| Tool | Description |
|------|-------------|
| `intel_disease_outbreaks` | WHO DON, ProMED, CIDRAP outbreaks |

### Social & Sanctions (3)
| Tool | Description |
|------|-------------|
| `intel_social_signals` | Reddit geopolitical discussion velocity |
| `intel_sanctions_search` | OFAC SDN list search |
| `intel_nuclear_monitor` | Seismic monitoring near nuclear test sites |

### Shipping & Trade (1)
| Tool | Description |
|------|-------------|
| `intel_shipping_index` | Dry bulk shipping stress index |

### Government (1)
| Tool | Description |
|------|-------------|
| `intel_usa_spending` | USAspending.gov federal contracts |

### Country Intelligence (3)
| Tool | Description |
|------|-------------|
| `intel_country_brief` | Quick country situation summary |
| `intel_country_stocks` | Stock exchanges and listings by country |
| `intel_financial_centers` | Global financial centers ranking |

### Extended Geospatial (1)
| Tool | Description |
|------|-------------|
| `intel_cloud_regions` | Cloud provider regions worldwide |

### Traffic (2)
| Tool | Description |
|------|-------------|
| `intel_traffic_flow` | Road traffic flow data |
| `intel_traffic_incidents` | Real-time traffic incidents |

### Cross-Domain Alerts (2)
| Tool | Description |
|------|-------------|
| `intel_alert_digest` | Cross-domain alert aggregation |
| `intel_weekly_trends` | Weekly trend analysis |

### Monitoring (2)
| Tool | Description |
|------|-------------|
| `intel_webcams` | Public webcam locations and live previews |
| `intel_status` | Server health, cache stats, circuit breaker status |

### Vector Search (5)
| Tool | Description |
|------|-------------|
| `intel_semantic_search` | Natural language search across all accumulated intelligence |
| `intel_similar_events` | Find events similar to a given data point |
| `intel_timeline` | Chronological view of intelligence for a domain/category |
| `intel_vector_stats` | Vector store collection statistics |
| `intel_collect` | Trigger an on-demand collection cycle |

### Cross-Domain Analytics (3)
| Tool | Description |
|------|-------------|
| `intel_cross_correlate` | Find correlated signals across all domains for a given topic |
| `intel_domain_summary` | Per-category summary of stored intelligence (counts, sources, recency) |
| `intel_trend_detection` | Detect activity surges/drops by comparing recent vs baseline periods |

### Reports (1)

| Tool | Description |
|------|-------------|
| `intel_generate_report` | Generate a PDF or HTML intelligence report covering 18 domains in parallel |

### AOI Geofences (10)
| Tool | Description |
|------|-------------|
| `intel_aoi_define` | Define a named area of interest: point + radius in km (1-2000) |
| `intel_aoi_define_polygon` | Define a polygon AOI from 3-64 [lat, lon] vertices (a border region, a strait, an EEZ) |
| `intel_aoi_define_corridor` | Define a corridor AOI: 2-64 waypoints plus a width in km (a shipping lane, supply road, cable run) |
| `intel_aoi_update` | Rename an AOI or change its center/radius in place (a rename keeps its change history) |
| `intel_aoi_list` | List all user-defined AOIs |
| `intel_aoi_delete` | Delete a user-defined AOI by name |
| `intel_aoi_brief` | Cited brief for an AOI: earthquakes, military flights, wildfires, conflict events, aviation, nearby infrastructure, and news mentions, all filtered to the AOI's radius |
| `intel_aoi_escalation` | Hotspot escalation scoring (same engine as the 22 built-in hotspots) applied to a user AOI |
| `intel_aoi_changes` | What entered or left an AOI since the last sweep (earthquakes, military flights, ACLED events, wildfire clusters, news); the first call sets a baseline |
| `intel_aoi_digest` | One change sweep across every AOI (or a named subset) with a combined markdown digest; a scheduler can call it on an interval |

### Daily Digest (1)
| Tool | Description |
|------|-------------|
| `intel_daily_digest` | Cited markdown morning brief: top events by domain, recent headlines, and (with the vector store) trends and a 24h timeline; every item carries a [n] citation |

### Severe Weather (2)
| Tool | Description |
|------|-------------|
| `intel_weather_alerts` | US National Weather Service active CAP alerts (US only) |
| `intel_meteoalarm_alerts` | Meteoalarm severe-weather warnings for 39 European countries (per-country feeds) |

### Tropical Cyclones (2)
| Tool | Description |
|------|-------------|
| `intel_cyclones` | NHC active storms: Atlantic, Eastern and Central Pacific |
| `intel_jtwc_cyclones` | JTWC warnings: Northwest Pacific, North Indian Ocean, Southern Hemisphere |

### Space Launches (1)
| Tool | Description |
|------|-------------|
| `intel_launch_schedule` | Upcoming rocket launches from Launch Library 2 (cached 1 h for the free tier) |

### Volcanoes (1)
| Tool | Description |
|------|-------------|
| `intel_volcano_activity` | Smithsonian GVP / USGS Weekly Volcanic Activity Report (weekly, curated) |

### Situation Brief (1)
| Tool | Description |
|------|-------------|
| `intel_situation_brief` | Cited situational awareness brief, generated on demand over MCP: a bounded server-side overview (earthquakes, military flights, ACLED conflict events, wildfires, cyber threats, disease outbreaks, news, space weather, strategic posture, alert digest), synthesized via local Ollama or a mechanically-cited fallback when Ollama is unreachable |

---

## Watching your own area (geofences/AOIs)

Static infrastructure results (bases, ports, nuclear, cables, datacenters, spaceports) draw on this repo's curated strategic datasets, which are global and deliberately sparse, not exhaustive local registries. A quiet AOI brief means nothing from those curated sets is in range, not that your area has no infrastructure.

Before the AOI family, only `intel_signal_convergence` accepted a real
point-plus-radius, `intel_military_flights` took a bbox, and hotspot
escalation scoring was restricted to the 22 hardcoded `INTEL_HOTSPOTS`.
The `intel_aoi_*` tools let you name your own area (a city, a border
region, a facility) and get the same cited, multi-domain treatment.
Geofences survive the antimeridian (a Bering Strait or Fiji AOI queries
both sides of the dateline), and pipelines/undersea cables are matched
as line features via great-circle segment distance, not just by their
endpoints.

Define an AOI once, then brief, score, edit, and watch it. Three
shapes: a circle (point + radius), a polygon (3-64 vertices, for a
border region or strait a radius cannot express), or a corridor (a
waypoint route + width, for a shipping lane or supply road):

```
intel_aoi_define(name="Pittsburgh", lat=40.4406, lon=-79.9959, radius_km=50)
intel_aoi_define_polygon(name="Taiwan Strait", vertices=[[22.5, 118.0], [22.5, 121.5], [26.5, 122.0], [26.5, 118.5]])
intel_aoi_define_corridor(name="Suez Approach", waypoints=[[29.9, 32.55], [27.5, 34.0], [24.0, 36.0]], width_km=80)
intel_aoi_brief(name="Taiwan Strait")
intel_aoi_escalation(name="Pittsburgh")
intel_aoi_update(name="Pittsburgh", radius_km=100)   # resize/rename in place
intel_aoi_changes(name="Suez Approach")  # what entered/left since last sweep
intel_aoi_digest()  # one-call sweep across ALL your areas
```

The same nine operations are available from the terminal: `intel aoi define|define-polygon|define-corridor|list|update|delete|brief|escalation|changes`.

Membership is exact for the shape (an event inside a polygon's
bounding circle but outside the polygon is excluded); corridor
distances are measured to the route. Line-feature infrastructure
(pipelines, undersea cables) matches the bounding circle for
non-circle shapes, disclosed in `data_gaps`.

`intel_aoi_changes` is the alerting primitive: the first call records a
baseline, and every later call reports what entered and left the fence
per domain, with failed fetches reported as `data_gaps` rather than
counted as departures.

`intel_aoi_brief` filters every geo-capable domain to the 50 km radius
around Pittsburgh: earthquakes, military flights (bbox derived from the
radius), wildfires (region-mapped, since NASA FIRMS has no point+radius
query), ACLED conflict events, a sample of nearby aviation traffic,
nearby static infrastructure (military bases, ports, pipelines, nuclear
facilities, undersea cables, datacenters, spaceports) with distances in
km, and news headline mentions of "Pittsburgh". Every item in the
response carries a `[n]` citation into a numbered `sources` list, and
`data_gaps` names any domain that couldn't be scoped to the AOI (for
example, wildfires when the AOI falls outside NASA FIRMS's coverage
regions, or conflict events when ACLED credentials aren't configured)
instead of silently omitting it.

`intel_aoi_escalation` runs the same baseline/military/conflict/social-
unrest scoring engine that powers `intel_hotspot_escalation` for the 22
built-in hotspots, but scoped to your AOI's own radius instead of a fixed
2-degree window.

AOIs persist in a dedicated table inside the same SQLite cache database
the server already uses (`~/.cache/world-intel-mcp/cache.db` by default,
or `$WORLD_INTEL_CACHE_DB`), so a scheduled agent can watch any named
area across restarts with `intel_aoi_list` / `intel_aoi_delete` to manage
them.

---

## Vector Store

The optional Qdrant vector store accumulates intelligence over time for semantic retrieval. All data fetched through the Fetcher is automatically embedded and stored.

Search uses Qdrant's Query API (`query_points`), requiring Python
`qdrant-client>=1.10.0` and a Query API-capable server (introduced in Qdrant
1.10). Use a maintained server release compatible with your client; upgrading
the Python package does not upgrade or secure a separately deployed server.
Search and collection stats are tested offline with real in-memory clients
1.10.0 and 1.19.1; this is not a remote-service or embedding-model integration
guarantee. See the
[dependency audit follow-up](docs/dependency-audit-2026-10-15.md#qdrant-search-compatibility-follow-up)
for coverage. Stats retain `vectors_count` as `null` when the client does not
report it; `points_count` is not substituted for an unknown vector count.

### Setup

```bash
# Install Qdrant (Docker)
docker run -p 6333:6333 qdrant/qdrant

# Install vector dependencies
pip install -e ".[vector]"

# Run the collector daemon (populates vector store 24/7)
intel-collector --daemon              # every 5 minutes
intel-collector --daemon --interval 120  # every 2 minutes
intel-collector --sources markets,cyber  # specific domains only
intel-collector                        # single collection cycle
intel-collector --daemon --sources aoi --interval 900  # AOI watch only: sweep every geofence every 15 min
```

The `aoi` group runs `intel_aoi_digest` as a sweep: each cycle diffs every
defined AOI against its stored snapshot and advances it, so the daemon
interval is your geofence watch cadence. Change digests are logged and
(when the vector store is enabled) stored for semantic search. Set
`WORLD_INTEL_AOI_WEBHOOK` to get a POST whenever a sweep finds something
entered or left a geofence (quiet sweeps never fire); with
`WORLD_INTEL_AOI_WEBHOOK_FORMAT=text` the body is raw markdown with a
`Title` header, which ntfy-style sinks render directly.

### Running as a macOS launchd Service

`scripts/collector-daemon.sh` manages the collector as a `launchd` agent so it
survives reboots. It fills in `com.agentic.intel-collector.plist.template`
with this checkout's own path (resolved from the script's own location, so
it works from any clone) and installs the result to
`~/Library/LaunchAgents/`.

```bash
scripts/collector-daemon.sh start    # install + load the launchd job
scripts/collector-daemon.sh status   # check state and log info
scripts/collector-daemon.sh logs     # tail stdout (logs err for stderr)
scripts/collector-daemon.sh stop     # unload the launchd job
scripts/collector-daemon.sh restart
scripts/collector-daemon.sh render   # print the filled-in plist without installing it
```

### Semantic Search Examples

Once data accumulates, AI agents can query across all domains:

- *"military activity near Taiwan strait"* — finds military flights, naval warnings, theater posture data
- *"cyber threats targeting healthcare"* — finds URLhaus, CISA KEV entries related to healthcare
- *"economic indicators suggesting recession"* — finds yield curve inversions, macro signals, FRED data

The vector store uses FastEmbed (ONNX-based, BAAI/bge-small-en-v1.5) for embeddings — no GPU required, ~3 second cold start.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ACLED_ACCESS_TOKEN` | No | ACLED conflict events |
| `NASA_FIRMS_API_KEY` | No | Satellite wildfire data |
| `EIA_API_KEY` | No | Energy price data |
| `CLOUDFLARE_API_TOKEN` | No | Internet outage data |
| `FRED_API_KEY` | No | Macro economic data (also used for yield curve) |
| `OPENSKY_CLIENT_ID` | No | Military flight fallback |
| `OPENSKY_CLIENT_SECRET` | No | Military flight fallback |
| `OLLAMA_API_URL` | No | Ollama server for AI-generated briefs (default: `http://localhost:11434`) |
| `OLLAMA_MODEL` | No | Ollama model for AI-generated briefs (default: `llama3.2`) |
| `WORLD_INTEL_LOG_LEVEL` | No | Logging level (default: INFO) |
| `WORLD_INTEL_AOI_WEBHOOK` | No | URL POSTed when an AOI sweep finds changes (collector daemon); quiet sweeps don't fire |
| `WORLD_INTEL_AOI_WEBHOOK_FORMAT` | No | `json` (default: title/totals/markdown payload) or `text` (raw markdown body + `Title` header, ntfy-style) |

Everything else uses free, unauthenticated public APIs.

### Fetcher proxy and CA environment trust

Fetcher-owned HTTPX clients ignore proxy and CA environment settings by default
(`trust_env=False`). This includes HTTP_PROXY/HTTPS_PROXY/ALL_PROXY, NO_PROXY
(and supported lowercase proxy names), plus SSL_CERT_FILE/SSL_CERT_DIR. TLS
verification stays enabled. Stock MCP, dashboard and collector entrypoints use
this default; there is no new CLI flag or `WORLD_INTEL_TRUST_ENV` switch.

Default owned **GETs** additionally use strict-direct public-address transport:
all DNS answers are checked before dialing numeric IPs, private/special addresses
are rejected (including redirect targets), and each new connection is revalidated.
Original Host and HTTPS TLS identity are preserved. Operator **POSTs** use a
separate unrestricted pool so intentional localhost webhooks still work; POSTs
never follow redirects. Other separately constructed service/SDK clients are
not covered. See [transport boundaries and limits](SECURITY.md#fetcher-get-profiles-and-public-address-transport).

Python integrations can deliberately enable HTTPX environment trust, or configure
an explicit CA context without enabling proxy/environment discovery:

```python
import ssl
from world_intel_mcp.cache import Cache
from world_intel_mcp.circuit_breaker import CircuitBreaker
from world_intel_mcp.fetcher import Fetcher

cache, breaker = Cache(), CircuitBreaker()
# Trusted/non-strict GET mode: delegates routing to HTTPX/env proxies.
# Opt in only when the environment and destinations are operator-trusted:
fetcher = Fetcher(cache, breaker, trust_env=True)

# Alternative: keep strict-public GETs and explicitly load a verifying CA:
tls = ssl.create_default_context(cafile="/absolute/path/to/reviewed-ca-bundle.pem")
fetcher_with_ca = Fetcher(cache, breaker, trust_env=False, verify=tls)
```

Use the configured Fetcher in your Python application/bootstrap; existing CLI
launchers do not read an environment opt-in. `trust_env` requires an actual bool,
and `verify` accepts only a bool or `SSLContext` (not a path string). Do not disable
TLS verification to work around CA setup: strict GETs reject insecure contexts.
`fetcher.get_transport_mode` reports `strict-public`, `trusted-environment`, or
`trusted-injected`. An open injected `httpx.AsyncClient` retains its own proxy/CA/
trust settings unchanged and is outside Fetcher's address guarantee. Opt-in
`trust_env=True` likewise selects **non-strict** HTTPX routing; it does not silently
bypass your proxies or add strict enforcement behind them. These are constructor
choices, never tool/source/body-selected profiles. This is not full application
SSRF protection; trusted profiles, operator POSTs and separate clients remain
separate trust boundaries.

---

## Development

```bash
pip install -e ".[dev]"
pytest                       # 973 tests (991 total, 18 live-network smoke tests deselected by default)
pytest --cov=world_intel_mcp # with coverage
pytest tests/test_forex.py -v # single module
```

### Adding a New Source

1. Create `sources/your_source.py` with `async def fetch_your_data(fetcher: Fetcher, **kwargs) -> dict`
2. Use `fetcher.get_json(url, source="your-source", cache_key=..., cache_ttl=300)` — automatic caching, retries, circuit breaking, rate limiting
3. In `server.py`: add `Tool(...)` to `TOOLS`, add `case` to `_dispatch()` (use inline import)
4. Add tests using `respx` to mock HTTP (see `tests/test_forex.py` for pattern)
5. Optionally add to `dashboard/app.py` (SSE) and `cli.py` (Click)

---

## License

MIT
