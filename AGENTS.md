# Repository Guidelines

## Project Structure & Module Organization

DataCrew combines FastAPI, LangGraph, and MCP for ecommerce analytics.
- `app/api/` handles HTTP/SSE; `app/application/ask_service.py` coordinates requests.
- `app/agents/` contains orchestration, nodes, prompts, and mock LLM behavior.
- `app/domain/` defines SQL policy; `app/tools/` implements MCP tools and SQL guards; `app/infra/` provides database, cache, memory, storage, and LLM adapters.
- `frontend/src/` contains Vue components, composables, services, and CSS tokens; `frontend/tests/` tests UI/protocol behavior.
- `tests/` holds backend tests; `eval/` holds datasets and evaluation runners; `reports/` stores evaluation reports.
- `deploy/postgres/init/` defines schemas/roles; `scripts/` provides utilities; `demo/` contains Streamlit UI.

## Build, Test, and Development Commands

Use Python 3.11+; CI uses Python 3.12 and Node.js 22. Run backend commands from the root:

```powershell
.\scripts\dev.ps1 install                 # Install development dependencies
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env               # First setup only; preserve existing .env
docker compose up -d postgres redis      # Dependencies for a locally running API
python -m app.main                       # API on :8000; required Windows loop setup
python -m ruff check app eval scripts tests
python -m pytest tests/ -q
```

From `frontend/`: `npm ci` installs dependencies, `npm run dev` serves on :5173, `npm run build` checks TypeScript and builds, and `npm test` runs Vitest. Run one API instance; `dev.ps1 infra` also starts the API container.

## Coding Style & Naming Conventions

Use four-space Python indentation, type hints, `snake_case` functions/modules, and `PascalCase` classes. Ruff configures 100-character lines and rules `E,F,I,UP,B`; mypy is available. Follow two-space Vue/TypeScript style, `PascalCase.vue` components, and `useX.ts` composables. Keep frontend logic in services; reuse `style.css` tokens.

## Testing Guidelines

Use pytest/pytest-asyncio (`tests/test_*.py`) and Vitest (`frontend/tests/*.test.ts`). Add regressions for SQL safety, interrupt/resume, memory, and SSE changes. Full backend testing requires initialized pgvector PostgreSQL, Redis, mock data, and `python eval/build_eval_set.py`; CI seeds with `python scripts/generate_mock_data.py --scale 0.1`. Use a disposable database: fixtures modify persistent memory. Set `LLM_MODE=mock` for credential-free model simulation. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Follow history's `type(scope): summary` pattern, e.g. `fix(security): ...`; scopes are optional and Chinese summaries are common. PRs should explain changes, link relevant issues, report checks and limitations, and include UI screenshots. CI requires Ruff, pytest, frontend build, and Vitest.

## Security & Configuration

Never commit `.env` or credentials; document settings through `.env.example`. Preserve SQL guards, read-only database access, and API authentication. Agents must not access evaluation gold SQL through business tools. Avoid overwriting unrelated working-tree changes.
