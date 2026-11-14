# DocuNavi

[![CI](https://github.com/Starlight-Tree/DocuNavi/actions/workflows/ci.yml/badge.svg)](https://github.com/Starlight-Tree/DocuNavi/actions/workflows/ci.yml)

English | [日本語](README.ja.md)

DocuNavi is a RAG chatbot that answers questions from PDF documents and cites its sources (FastAPI, LangGraph, Gemini, Postgres + pgvector, Next.js on Cloudflare Workers).
The sample documents (`storage/pdf/`) are fictional.

| Chat (with citations and follow-up suggestions) | Document list (filter by group and type, choose which documents to use) |
|---|---|
| ![Chat screen](docs/images/chat.png) | ![Document list](docs/images/documents.png) |

## Features

- **Answers with citations.** A `[n]` marker opens the source document and page. You choose which documents an answer may use.
- **Measured search.** Layout-aware chunking, hybrid search (BM25 + pgvector), reranking and GraphRAG; each configuration is evaluated in `evals/` ([docs/search-design.md](docs/search-design.md)).
- **Guardrails.** Input and answers are checked, including prompt leaks while streaming. Rule-based, so not a full defense against prompt injection ([docs/guardrails.md](docs/guardrails.md)).
- **Resilient LLM calls.** Hedged requests, model fallback, and a paid-key switch when the free tier runs out ([docs/configuration.md](docs/configuration.md)).
- **Role-based access and login.** Document groups depend on the user's role, decided by the server. Better Auth (Google) JWTs are verified.

## Architecture

```mermaid
flowchart LR
    B[Browser] --> W["Cloudflare Worker<br/>docunavi-web (Next.js)"]
    B -->|"/chat/stream (SSE)"| R["Go SSE relay<br/>Cloud Run"]
    B -->|"/chat etc."| A
    R --> A["FastAPI + LangGraph<br/>Cloud Run"]
    B -->|login| AU["Cloudflare Worker<br/>docunavi-api (Better Auth)"]
    A --> G[Gemini]
    A --> DB[("Postgres + pgvector<br/>Prisma Postgres")]
    AU --> DB
    R2[("Cloudflare R2<br/>PDFs and backups")] -.ingest.-> DB
```

A question goes through LangGraph: input guard → agent (tool-call loop that searches the documents) → output guard. See [docs/architecture.md](docs/architecture.md). Why things are built this way (including the Go relay, which is there for learning): [docs/decisions/](docs/decisions/README.md).

## Quick start

This runs a keyless mock (fixed replies, no document search), just to try the UI and the API together. Real answers need a Postgres with the documents and a Gemini key: see [docs/development.md](docs/development.md) and [docs/configuration.md](docs/configuration.md).

```bash
# API (http://localhost:8000)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload

# UI (another terminal, http://localhost:3000)
cd frontend && npm ci
cp .env.example .env.local   # first time only
npm run dev
```

Tests: `pip install -r requirements-dev.txt && pytest` (mock mode, no real API calls) and `cd frontend && npm test`.

## Documentation

The documents are written in Japanese.

- [docs/README.md](docs/README.md): index of all documents
- [docs/architecture.md](docs/architecture.md): request flow and directory layout
- [docs/configuration.md](docs/configuration.md): environment variables, handling of LLM latency and failures
- [docs/api.md](docs/api.md): API reference
- [docs/decisions/](docs/decisions/README.md): important decisions and their reasons
- [evals/README.md](evals/README.md): evaluation procedures and results

## About this repository

A personal project published as a portfolio, built to explore production concerns: guardrails, LLM failure handling, role-based document access, and measured search quality. It contains no secrets and no production credentials; configuration files and operation records (`docs/deploy.md`) are included to show how deployment works. Pattern-based secret checks are not a complete guarantee.

## License

[MIT](LICENSE)
