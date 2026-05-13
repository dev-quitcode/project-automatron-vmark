# Project Automatron

> Автономна система розробки програмного забезпечення з розділенням Інтелекту (Architect) та Механіки (Builder), керована LangGraph Orchestrator.

## Архітектура

```
Human ←→ [Web UI (Next.js)] ←WebSocket→ [API Gateway (FastAPI)]
                                              ↓
                                    [Orchestrator (LangGraph)]
                                      ↙              ↘
              [Architect Node]              [Builder Node]
              (LLM API call)                (Docker + Cline CLI)
```

## Стек

| Компонент    | Технологія                          |
|--------------|-------------------------------------|
| Orchestrator | Python 3.12 + LangGraph + FastAPI   |
| Architect    | litellm (Claude/GPT/Gemini)         |
| Builder      | Cline CLI 2.x (headless `-y` mode)  |
| Web UI       | Next.js 15 + Tailwind + shadcn/ui   |
| State        | SQLite (LangGraph checkpoints)      |
| Containers   | Docker (Golden Image Ubuntu 24.04)  |

## Quick Start

```bash
# 1. Build golden image
make golden

# 2. Copy and configure secrets
cp .env.example .env
mkdir -p secrets
echo "your-key" > secrets/openai_api_key.txt

# 3. Start development
make dev

# 4. Open Web UI
open http://localhost:3000
```

## Project Structure

```
├── orchestrator/          # Python backend (FastAPI + LangGraph)
├── web-ui/                # Next.js frontend
├── docker/                # Dockerfiles (golden-image, orchestrator, web-ui)
├── docs/                  # Architecture & deployment docs
├── docker-compose.yml     # Full stack deployment
└── Makefile               # Dev/build/deploy commands
```

## Documentation

- [Implementation Plan](IMPLEMENTATION_PLAN.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Deployment v2 Kamal E2E](docs/DEPLOYMENT_V2_KAMAL_E2E.md)

## License

Private / Proprietary
