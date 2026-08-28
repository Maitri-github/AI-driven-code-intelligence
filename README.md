Schematic is an automated code intelligence and documentation platform. Given any public or private GitHub repository URL, Schematic:

1. Explains Code in Plain Language: Analyzes repository source files and produces structured explanations: Overview, Key Components, Execution Flow, and Notable Risks & Edge Cases.
2. Generates Whole-Repo Architecture Diagrams: Statically analyzes module import statements across languages (Python, JS/TS, Go, Java, Ruby), clusters files into architectural subsystems, and renders interactive Mermaid.js diagrams with dark mode styling and raw source fallbacks.
3. Aggregates API & Reference Documentation: Detects public functions, classes, methods, and REST route handlers (FastAPI, Express, Flask, Gin), extracting parameters, return types, errors, and concise factual purposes.
4. Watches Pull Requests & Posts Automated Review Comments: Includes a GitHub webhook receiver and an Interactive PR Diff Simulator that analyzes incoming diffs, re-evaluates changed files from cache, and flags whether diagrams or documentation have become stale.
5. High-Performance SQLite Caching: Stores analysis results keyed by `(file_path, content_hash)`, enabling sub-second repeat analysis for unchanged files.


 AI Backend — IBM watsonx.ai (Granite Models)

Schematic uses IBM watsonx.ai's REST API:

- Primary Model: `ibm/granite-8b-code-instruct` (optimized for code explanation, AST reasoning, and documentation generation).
- Fallback Model: `ibm/granite-3-8b-instruct` (automatically triggered if the code model is not provisioned in the target region).
- Authentication: Exchanges `WATSONX_API_KEY` for an IBM Cloud IAM Bearer token via `https://iam.cloud.ibm.com/identity/token` with in-memory token expiration caching.
- Endpoint Pattern: `POST {WATSONX_URL}/ml/v1/text/chat?version=2025-02-06`
- Reliability: Built-in exponential backoff retry for HTTP 429 rate limits and transient connection timeouts.
- Graceful Degradation: If `WATSONX_API_KEY` is not supplied, Schematic starts normally and activates an intelligent **Heuristic / Mock Engine**, allowing complete UI exploration and testing without crashes.


 Quick Start & Setup

 Prerequisites
- Python 3.10+
- (Optional) Git CLI installed (if Git is not in PATH, Schematic automatically falls back to direct GitHub archive extraction).

 1. Install Dependencies
```bash
pip install -r backend/requirements.txt
```

 2. Configure Environment Variables
Copy the `.env.example` template to `.env`:
```bash
cp .env.example .env
```

Edit `.env` with your IBM Cloud credentials:
```ini
WATSONX_API_KEY=your_ibm_cloud_iam_api_key
WATSONX_PROJECT_ID=your_watsonx_project_id_guid
WATSONX_URL=https://us-south.ml.cloud.ibm.com
WATSONX_MODEL_ID=ibm/granite-8b-code-instruct
WATSONX_FALLBACK_MODEL_ID=ibm/granite-3-8b-instruct
DEFAULT_FILE_CAP=40
```

> Note: If you don't have watsonx credentials immediately available, you can still launch the app! Schematic will notify you in the UI and seamlessly operate in Mock Mode.

 3. Launch Schematic
```bash
python run.py
```
Open your browser at **http://localhost:8000** to access the dashboard.

---

 4. Architecture & Repository Structure

```
schematic/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI application, static file mounts, routes
│   │   ├── config.py                # Environment & setting management
│   │   ├── models/
│   │   │   └── schemas.py           # Pydantic data schemas
│   │   ├── services/
│   │   │   ├── watsonx_client.py    # IBM watsonx.ai REST client & IAM auth
│   │   │   ├── repo_cloner.py       # Git & Archive cloner with .gitignore parser
│   │   │   ├── code_parser.py       # Multi-language AST/Regex chunker & parser
│   │   │   ├── architecture.py      # Dependency graph & Mermaid diagram builder
│   │   │   ├── doc_generator.py     # Aggregated API doc generator
│   │   │   ├── pr_analyzer.py       # PR diff analysis & comment generator
│   │   │   ├── analyzer.py          # Orchestration pipeline with live progress
│   │   │   └── cache.py             # SQLite caching service (path, hash)
│   │   └── routers/
│   │       ├── analyze.py           # /api/analyze, /api/progress, /api/results
│   │       ├── webhook.py           # /api/webhook/github, /api/webhook/simulate-pr
│   │       ├── config.py            # /api/config/status, /api/config/test-watsonx
│   │       └── cache.py             # /api/cache/stats, /api/cache/clear
│   ├── tests/                       # Pytest test suite
│   └── requirements.txt
├── frontend/                        # Zero-build single page React dashboard
│   ├── index.html                   # HTML entry point (Tailwind + React + Mermaid.js)
│   ├── app.js                       # React application component tree
│   └── style.css                    # Dark mode styles & custom scrollbars
├── .env.example
├── README.md
└── run.py                           # One-click startup script
```

---

 5. Core Features Breakdown

 A. Code Explanation
- Chunks source files at logical function/class boundaries.
- Prompts Granite model for:
  - Overview: Concise summary of file purpose.
  - Key Components: Classes, functions, and responsibilities.
  - Flow: Step-by-step control and data flow.
  - Notable Risks: Edge cases, concurrency issues, security caveats.
- Caches analysis in SQLite by `(file_path, SHA256_content_hash)`.

 B. Architecture & Dependency Diagrams
- Statically identifies module imports (`import x`, `from x import y`, `require(...)`).
- Resolves internal dependencies to project file paths.
- Automatically clusters modules into subsystems:
  - `Entry & Configuration` (e.g. `main.py`, `app.py`, `config.py`)
  - `API & Controllers` (e.g. `routes/`, `controllers/`, `api/`)
  - `Core Business Services` (e.g. `services/`, `domain/`, `core/`)
  - `Data & Models` (e.g. `models/`, `schemas/`, `db/`)
  - `Utilities & Helpers` (e.g. `utils/`, `helpers/`, `clients/`)
- Renders dynamic Mermaid flowcharts in the browser with raw syntax fallbacks and copy actions.

 C. Aggregated API Reference Documentation
- Automatically extracts signatures for all public functions, classes, and REST endpoints.
- Displays HTTP methods, route paths, parameter tables (types, defaults, required flags), return values, and detected exception raises.
- Includes a live search filter by symbol, file name, or route type.

 D. Pull Request Integration & Simulator
- Webhook endpoint `POST /api/webhook/github` listens for `pull_request` events.
- Diffs the PR against the base branch, evaluates changed files, and determines if:
  - Architecture diagram is stale: New import statements or module additions detected.
  - API docs are stale: Function signatures or routes modified.
- Generates a GitHub-ready Markdown comment.
- Includes an in-app PR Diff Simulator so you can test PR comment generation directly in the browser with sample diffs.


 6. Known Limitations & Trade-offs (Prototype Scope)

1. File Scope Cap: By default, analysis is capped at 40 matched source files (configurable in UI or `.env`) to remain responsive and stay within demo token limits. The UI always explicitly displays "N of M files analyzed".
2. Static Import Parsing vs. Dynamic Call-Graph: Dependency extraction is performed via AST and regex pattern matching on import/require statements. Full dynamic call-graph tracing is a future enhancement.
3. AST Chunking: Large files (>150 lines) are chunked at top-level class/function boundaries to fit comfortably into Granite context windows.
4. Security & Sandbox: Cloned repositories are strictly parsed as static text; no code from the target repository is ever executed. Temporary clone directories are deleted immediately after analysis.


 7. Running Automated Tests

Run the full test suite with `pytest`:
```bash
pytest backend/tests -v
```

Tests cover:
- Multi-language AST/regex code parsing (Python, JavaScript, Go, Java, Ruby)
- Architecture graph generation & Mermaid syntax styling
- SQLite cache operations and instant cache hits
- PR diff parsing, changed-file detection, and Markdown comment generation
- FastAPI API endpoints & health checks
