# Project Automatron — Повний технічний blueprint

> Версія документу: 1.0 · Дата: 2026-04-27 · Призначення: повний опис системи з можливістю відбудови з нуля
>
> Цей документ є **єдиним джерелом істини** про фактичну реалізацію Automatron на момент гілки `main` (комiт `c62d4a9`). Він свідомо описує **те, що є в коді**, а не те, що було заплановано в [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) (який описує LangGraph+Cline архітектуру, від якої зробили розворот).

---

## Зміст

1. [Огляд та позиціонування продукту](#1-огляд-та-позиціонування-продукту)
2. [Бізнес-логіка та повний користувацький флоу](#2-бізнес-логіка-та-повний-користувацький-флоу)
3. [Архітектура верхнього рівня](#3-архітектура-верхнього-рівня)
4. [Структура репозиторію — повна](#4-структура-репозиторію--повна)
5. [Конфігурація та змінні середовища](#5-конфігурація-та-змінні-середовища)
6. [Backend: Orchestrator (Python/FastAPI)](#6-backend-orchestrator-pythonfastapi)
7. [REST API — повний реєстр ендпоінтів](#7-rest-api--повний-реєстр-ендпоінтів)
8. [WebSocket: події і кімнати](#8-websocket-події-і-кімнати)
9. [Дані та схема SQLite](#9-дані-та-схема-sqlite)
10. [GitHub-інтеграція (Issues, PR, Webhooks, Actions)](#10-github-інтеграція-issues-pr-webhooks-actions)
11. [LLM-шар: провайдери, ролі, каталог, промпти](#11-llm-шар-провайдери-ролі-каталог-промпти)
12. [Local Preview Engine](#12-local-preview-engine)
13. [Frontend: Next.js Web UI](#13-frontend-nextjs-web-ui)
14. [Docker, Docker Compose, мережі](#14-docker-docker-compose-мережі)
15. [CI/CD самого Automatron](#15-cicd-самого-automatron)
16. [Спостережуваність та трасування](#16-спостережуваність-та-трасування)
17. [Безпека та секрети](#17-безпека-та-секрети)
18. [Сумісність з застарілими модулями (рудименти)](#18-сумісність-з-застарілими-модулями-рудименти)
19. [Тести](#19-тести)
20. [Покрокова інструкція з відбудови з нуля](#20-покрокова-інструкція-з-відбудови-з-нуля)
21. [Чек-лист готовності та смоук-тести](#21-чек-лист-готовності-та-смоук-тести)
22. [Відомі проблеми та технічний борг](#22-відомі-проблеми-та-технічний-борг)
23. [Глосарій](#23-глосарій)

---

## 1. Огляд та позиціонування продукту

### 1.1 Що це

**Project Automatron** — приватна (proprietary) автономна платформа доставки програмного забезпечення, яка приймає на вхід **GitHub репозиторій з README/PRD** і самостійно проводить його через цикл:

```
README → План (Architect LLM) → Approve → GitHub Issues
       → Copilot Coding Agent → Pull Requests
       → AI Reviewer LLM → Merge → Local Preview (Docker)
       → Deploy на VPS (GitHub Actions)
```

### 1.2 Що НЕ робить (свідомі обмеження)

- Не пише код локально та не запускає Cline CLI/builder-агента у власних контейнерах. Усі коміти робить **GitHub Copilot Coding Agent** (assignee `copilot-swe-agent[bot]`).
- Не має автентифікації — система розрахована на приватну мережу/VPN.
- Не керує множинними тенантами — один інстанс = один оператор.
- Не валідує сценарії, де `repo_url` веде не на GitHub.

### 1.3 Цільова інфраструктура

- Один Linux VPS (Ubuntu 22.04+/Debian 12+) з Docker 24+
- Доменне ім'я з TLS (Traefik у проді, Nginx — fallback профіль)
- GitHub PAT з правами `repo`, `admin:repo_hook`, `workflow`, опціонально `admin:org`
- Одна-три LLM-провайдерських API-ключі (OpenAI/Anthropic/Google)

---

## 2. Бізнес-логіка та повний користувацький флоу

### 2.1 Стейдж-машина проекту

Кожен проект — це **скінченний автомат** із такими станами (поле `project_stage` у таблиці `projects`, тип [`ProjectStage`](../web-ui/src/lib/types.ts#L14)):

| Stage | Що означає | Хто переводить |
|---|---|---|
| `intake` | Створено запис, ще не запущено | Користувач (POST /projects) |
| `planning` | Architect LLM генерує план | `start_project` |
| `awaiting_plan_approval` | План згенеровано, чекаємо approve | `analyze_and_plan` (фінал) |
| `repo_preparing` | (legacy/optional) Репо готується | RepositoryManager |
| `scaffolding` | (legacy) Scaffold | Не використовується в GitHub-режимі |
| `building` | Issues створено, Copilot будує | `apply_plan` |
| `awaiting_preview_approval` | Preview готовий, чекаємо approve | `_detect_and_set_preview_url` |
| `ready_for_deploy` | Можна тиснути Deploy | (manual / preview approval) |
| `deploying` | GH Actions deploy у процесі | DeploymentManager |
| `deployed` | Live | GH Actions success |
| `frozen` | Ескалаційний ліміт перевищено | (legacy) |
| `error` | Будь-яка фатальна помилка | `start_project` exception |

Паралельно є грубіший статус (`status` поле, тип [`ProjectStatus`](../web-ui/src/lib/types.ts#L1)):
`pending | planning | building | preview | ready_for_deploy | deploying | deployed | paused | frozen | error | deleted`.

### 2.2 Повний хронологічний флоу

#### Крок 1 — Створення проекту

1. Користувач у Web UI відкриває `New Project` ([NewProjectDialog.tsx](../web-ui/src/components/project/NewProjectDialog.tsx)).
2. Заповнює:
   - `name` (авто-витягується з `repo_url` через [parseRepoName](../web-ui/src/components/project/NewProjectDialog.tsx#L23))
   - `repo_url` (https://github.com/owner/repo або `owner/repo`)
   - LLM-конфіг для трьох ролей (architect, builder, reviewer) — провайдер + модель з динамічного каталогу
   - Optional: список Figma URL та/або один `.fig` файл
3. POST `/api/projects` ([routes.py:206](../orchestrator/orchestrator/api/routes.py#L206)):
   - Парсить `repo_url` через [`_parse_repo_url`](../orchestrator/orchestrator/orchestrator.py#L62) — приймає `github.com/o/r`, `https://...`, або `owner/repo`
   - Якщо передано лише `repo` без `owner`, підставляє `settings.github_default_org or settings.github_owner`
   - Створює запис у `projects` через [`create_project`](../orchestrator/orchestrator/models/project.py#L366)
4. Якщо передано `.fig` — POST `/api/projects/{id}/figma-file` ([routes.py:280](../orchestrator/orchestrator/api/routes.py#L280)):
   - Розпаковує zip, шукає `document.json`, передає в [`_summarise_figma_node`](../orchestrator/orchestrator/orchestrator.py#L154)
   - Зберігає короткий текст-summary в `figma_file_context` (макс 100 рядків frame-дерева)

#### Крок 2 — Старт побудови (`POST /api/projects/{id}/start`)

1. Виконує preflight через [`PreflightService.run("start", project=...)`](../orchestrator/orchestrator/validation/preflight.py#L51):
   - `_github_configuration_checks` — наявність `GITHUB_TOKEN`, `GITHUB_OWNER`
   - `_llm_provider_checks` — для кожної ролі: чи є API-ключ для обраного провайдера, чи модель присутня в каталозі
2. Якщо preflight `blocking == True` → HTTP 409 із детальним JSON (`PreflightResult.to_dict()`)
3. Інакше — фоновий таск `orch_start(project_id)` ([orchestrator.py:908](../orchestrator/orchestrator/orchestrator.py#L908))

`start_project` створює `GitHubOrchestrator` і викликає `analyze_and_plan`:

##### `analyze_and_plan` ([orchestrator.py:270](../orchestrator/orchestrator/orchestrator.py#L270))

1. Витягує `owner/repo` (з БД або повторно парсить `repo_url`/`intake_text`)
2. **Авто-реєструє webhook** через [`GitHubClient.register_webhook`](../orchestrator/orchestrator/github/issues.py#L273):
   - Тільки якщо `AUTOMATRON_PUBLIC_URL` задано
   - Перевіряє `GET /repos/{o}/{r}/hooks` на дублікат за URL
   - Створює hook з `events=["pull_request"]`, `content_type=json`, опціонально `secret`
3. Записує в activity log та `trace_events`
4. Читає `README.md` та `docs/PRD.md` через `GitHubClient.read_file` (Contents API, base64-decode)
5. Якщо є Figma URL і токен — викликає [`_fetch_figma_context`](../orchestrator/orchestrator/orchestrator.py#L113):
   - Парсить ключ файлу та `node-id` з URL
   - Викликає `GET /v1/files/{key}` або `/v1/files/{key}/nodes?ids=...`
   - Walker по дереву віддає до 100 рядків з frame/component іменами та видимими текстами
6. Завантажує промпт `architect_github_v1.txt`
7. Будує user-message: `Repository: o/r\n\n## README\n{readme}\n\n---\n\n{prd}\n\n## Figma Design Context\n{figma}\n\nProduce the architecture document, stories document, and issue plan now.`
8. Стрімить через `litellm.acompletion(stream=True)`:
   - кожен chunk → `emit_architect_chunk` (event `architect:message`, `is_streaming: true`)
   - повна відповідь акумулюється
9. Парсить три блоки регулярним шуканням `\`\`\`{tag}\n...\n\`\`\`` ([`_parse_tagged_block`](../orchestrator/orchestrator/orchestrator.py#L50)):
   - `markdown:architecture` → `architecture_md`
   - `markdown:stories` → `stories_md`
   - `json:issue_plan` → JSON `{epics: [{title, description, stories: [{title, tasks: [{title, file, component, description, implementation_notes, acceptance_criteria, validation}]}]}]}`
10. Будує людиночитаний `plan_md` через [`_build_plan_md`](../orchestrator/orchestrator/orchestrator.py#L1018) (Architecture секція + Tasks з `[ ]` чекбоксами по epic/story)
11. UPDATE `projects` SET `plan_md`, `issue_plan_json`, `project_stage='awaiting_plan_approval'`, `status='planning'`
12. Емітує:
    - `architect:message` (streaming=false, повний `plan_md`)
    - `status:update` з `progress={completed:0, total:N}`
    - `human:required` з `reason="Review the plan and approve to create GitHub Issues."`

#### Крок 3 — Approve плану (`POST /api/projects/{id}/approve-plan`)

1. Записує approval через `record_approval(project_id, "plan", True)` → `approval_history_json` оновлюється, `plan_approved=1`, `plan_approved_at=now()`
2. Фоновий таск `resume_project(id, "plan", True)` ([orchestrator.py:920](../orchestrator/orchestrator/orchestrator.py#L920))
3. Якщо approval_type=="plan" + approved=True → `apply_plan`

##### `apply_plan` ([orchestrator.py:421](../orchestrator/orchestrator/orchestrator.py#L421))

1. Завантажує `issue_plan_json`, перевіряє наявність `epics`
2. UPDATE `project_stage='building'`, `status='building'`
3. Пушить `docs/ARCHITECTURE.md` та `docs/STORIES.md` через [`GitHubClient.push_file`](../orchestrator/orchestrator/github/issues.py#L56) (Contents API PUT з base64)
4. Для кожного epic:
   - Створює Milestone через [`create_milestone`](../orchestrator/orchestrator/github/issues.py#L91): `POST /repos/{o}/{r}/milestones {title, description, state:open}`. На 422 — шукає в існуючих та повертає номер
   - Для кожної story створює label (обрізаний до 50 символів через `[:50]`, колір `bfd4f2`)
   - Для кожного task:
     - Тіло генерує [`_render_issue_body`](../orchestrator/orchestrator/orchestrator.py#L182): Epic + Story → Overview → Scope (file/component) → Implementation notes → Acceptance criteria (як `- [ ]`) → Validation (у code-fence)
     - Створює Issue з трирівневим fallback:
       1. з `assignees: ["copilot"]` (на старіших налаштуваннях)
       2. без assignees
       3. без milestone і labels (bare create)
     - Зберігає у БД через `create_github_issue(uuid, project_id, issue_number, title, epic, story, copilot_workspace_url=html_url)`
5. Емітує `issues:updated` з повним списком та `status:update` з `progress`

#### Крок 4 — Призначення Copilot (`POST /api/projects/{id}/assign-copilot`)

[`assign_to_copilot`](../orchestrator/orchestrator/orchestrator.py#L944):

- Бере всі issues зі статусом `open`
- Для кожної викликає [`GitHubClient.trigger_copilot_agent`](../orchestrator/orchestrator/github/issues.py#L157):
  ```
  POST /repos/{o}/{r}/issues/{n}/assignees
  {
    "assignees": ["copilot-swe-agent[bot]"],
    "agent_assignment": {"target_repo": "o/r", "base_branch": "main"}
  }
  ```
- Повертає `{assigned, failed}` лічильники

Альтернатива — `POST /api/projects/{id}/issues/{issue_number}/assign-copilot` для одного issue.

#### Крок 5 — Webhook PR-флоу

GitHub викликає `POST /api/webhooks/github` ([webhook_github.py:94](../orchestrator/orchestrator/api/webhook_github.py#L94)):

1. Перевіряє підпис HMAC-SHA256 за `X-Hub-Signature-256` (skipped при відсутності `GITHUB_WEBHOOK_SECRET`)
2. Фільтрує: тільки `X-GitHub-Event: pull_request`, action `opened|reopened|closed`
3. **Знаходить пов'язані issues**:
   - Strategy 1: regex на тілі PR — `(closes|fixes|resolves|fixed|closed|resolved)\s+#(\d+)` ([webhook_github.py:27](../orchestrator/orchestrator/api/webhook_github.py#L27))
   - Strategy 2 (fallback): GraphQL `closingIssuesReferences` через [`_linked_issue_numbers`](../orchestrator/orchestrator/api/webhook_github.py#L48) — Copilot часто не пише `Closes`
4. Для кожного `issue_number`:
   - Знаходить запис у БД через `find_github_issue_by_repo(owner, repo, issue_number)` (JOIN через `projects` з `github_repo_owner/name`)
   - На `opened|reopened`: `update_github_issue_pr(project_id, issue_number, pr_number, pr_url)` → status `pr_open`, тригерить `orch_review_pr` у фоні
   - На `closed`: статус → `merged` (якщо `merged==true`) або `closed`; на merged тригерить `orch_sync_issues`
5. Емітує `issues:updated`

##### `review_pr` ([orchestrator.py:650](../orchestrator/orchestrator/orchestrator.py#L650))

1. `GET /repos/{o}/{r}/pulls/{n}` з `Accept: application/vnd.github.diff` — текстовий diff
2. `GET /repos/{o}/{r}/issues/{issue_number}` — issue body як task spec
3. Системний промпт хардкоджений у методі (огляд diff проти acceptance criteria, формат: PASSED/ISSUES FOUND, summary, bullets)
4. Викликає `call_llm` (non-streaming) з reviewer-моделлю проекту, `max_tokens=2048`, перші 12000 символів diff
5. Парсить: `passed = response.startswith("PASSED")`
6. UPDATE issue: `status='pr_reviewed'`, `pr_review_json=...`
7. Постить коментар на PR через `POST /repos/{o}/{r}/issues/{pr}/comments` з `## Automatron AI Review\n{text}\n\n*Reviewed by Automatron (model: ...)*`
8. Емітує `pr:review_ready`

#### Крок 6 — Sync (manual or auto)

`POST /api/projects/{id}/sync-issues` → [`sync_issues`](../orchestrator/orchestrator/orchestrator.py#L573):

1. Бере локальні issues
2. `GET /repos/{o}/{r}/issues?state=all&per_page=100`, фільтрує PR (мають `pull_request` поле)
3. Для кожного: якщо GitHub `closed` → `merged|closed`. Якщо `open` без `pr_number` — викликає [`find_pr_for_issue`](../orchestrator/orchestrator/github/issues.py#L232):
   - Стратегія 1: timeline `cross-referenced` events
   - Стратегія 2: пошук `closes #N | fixes #N | resolves #N | #N` у тілах PR
4. Якщо всі `merged|closed` і `preview_url` ще немає — викликає [`_detect_and_set_preview_url`](../orchestrator/orchestrator/orchestrator.py#L631)

#### Крок 7 — Local Preview

[`run_preview_locally`](../orchestrator/orchestrator/preview.py#L117) у `orchestrator/workspaces/{project_id}/repo/`:

1. Клонує (або `git pull`) з `https://x-access-token:{token}@github.com/{o}/{r}.git`
2. [`_detect_project_type`](../orchestrator/orchestrator/preview.py#L35): читає `package.json` deps:
   - `next` → `nextjs`
   - `vite` або `@vitejs/plugin-react` → `vite`
   - інакше Node-проект → `node`
   - `pyproject.toml`/`requirements.txt` → `python`
   - інакше → `unknown` (skip)
3. [`_ensure_dockerfile`](../orchestrator/orchestrator/preview.py#L53): якщо немає `Dockerfile` — пише мінімальний:
   - **nextjs**: `node:22-alpine`, `npm install`, `npm run build`, `EXPOSE 3000`, `CMD npm start`
   - **vite**: `node:22-alpine`, `npm run build`, `npm install -g serve`, `serve -s dist -l 3000`
   - **node**: `node:22-alpine`, `npm install`, `EXPOSE 3000`, `npm start`
   - **python**: `python:3.12-slim`, `pip install -r requirements.txt || pip install -e .`, `uvicorn app.main:app --host 0.0.0.0 --port 8000`
4. [`_find_free_port`](../orchestrator/orchestrator/preview.py#L20): сканує діапазон `PORT_RANGE_START..PORT_RANGE_END` (default 7000-7999)
5. `docker rm -f preview-{project_id}` (idempotent)
6. `docker build -t automatron-preview-{project_id} .`
7. `docker run -d --name preview-{project_id} -p {host_port}:{internal_port} --restart unless-stopped image`
8. Polling 20×3s на `http://localhost:{host_port}` — перший response < 500 = ready
9. Записує `preview_url` через `update_project_preview`, статус `ready`, емітує `status:update`

#### Крок 8 — Audit (опціонально)

`POST /api/projects/{id}/audit` → [`audit_codebase`](../orchestrator/orchestrator/orchestrator.py#L740):

1. Якщо `workspaces/{id}/repo` існує — читає файли через [`_read_source_files`](../orchestrator/orchestrator/orchestrator.py#L75) (skip `node_modules`, `.git`, `.next`, `dist`, `build`, `.venv`, `__pycache__`, `.cache`, `coverage`, `.turbo`, `out`, `.mypy_cache`; включає `.ts/.tsx/.js/.jsx/.py/.css/.md`; truncate file > 5000 chars; total cap 40000)
2. Reviewer LLM з системним промптом "identify missing/broken/scaffold code, prefer minimum-change fixes" → видає numbered findings
3. Architect LLM з `architect_github_v1.txt` + findings → новий `json:issue_plan`
4. Створює нові GitHub issues так само, як в `apply_plan`

#### Крок 9 — Deploy (для самого Automatron)

Не для дочірніх проектів — для самого Automatron є `.github/workflows/deploy.yml`. Дочірні проекти Copilot будує і мерджить, а deploy відбувається через preview або вручну.

(У `deployment/manager.py` є код для SSH-deploy, але він не викликається з GitHubOrchestrator у поточному коді — він залишений для майбутнього/legacy шляху).

---

## 3. Архітектура верхнього рівня

```
┌──────────────────────────────────────────────────────┐
│  Browser  →  Next.js 15 (Web UI)                     │
│             ├─ REST fetch → /api/*                   │
│             └─ Socket.IO  → /socket.io/* (WS)        │
└─────────────────────┬────────────────────────────────┘
                      │
              [Traefik / Nginx]
                      │
┌─────────────────────▼────────────────────────────────┐
│  FastAPI app (uvicorn :8000)                         │
│  ┌────────────────────────────────────────────────┐  │
│  │ ASGI app = socketio.ASGIApp(sio, fastapi_app)  │  │
│  └─────┬────────────────────────────────┬─────────┘  │
│        │ REST (FastAPI router)          │ WS (sio)   │
│  ┌─────▼─────────┐   ┌──────────────────▼─────────┐  │
│  │ api/routes.py │   │ api/websocket.py + events  │  │
│  └─────┬─────────┘   └──────────────────┬─────────┘  │
│        └─────────────┬─────────────────-┘            │
│                      ▼                               │
│  ┌────────────────────────────────────────────────┐  │
│  │  GitHubOrchestrator (orchestrator.py)          │  │
│  │   ├ analyze_and_plan   ┐                       │  │
│  │   ├ apply_plan         │  call_llm[_streaming] │  │
│  │   ├ sync_issues        ├─→ litellm → OpenAI/   │  │
│  │   ├ review_pr          │            Anthropic/ │  │
│  │   ├ audit_codebase     ┘            Google     │  │
│  │   └ assign_to_copilot                          │  │
│  └─────────────────────────────────────────────-──┘  │
│                      │                               │
│  ┌────────┬─────────┬─┴────────┬───────────┐         │
│  │GitHub  │Repository│ GH      │Preview    │         │
│  │Client  │Manager   │Actions  │Engine     │         │
│  │(issues │(git+repo)│Manager  │(docker    │         │
│  │.py)    │          │(secrets)│ subproc)  │         │
│  └────────┴─────────┴────────--┴────-──────┘         │
│                      │                               │
│  ┌────────────────────▼───────────────────────────┐  │
│  │  aiosqlite → /app/data/automatron.db (WAL)     │  │
│  │   tables: projects, sessions, task_logs,       │  │
│  │   chat_messages, deploy_runs, trace_events,    │  │
│  │   activity_logs, github_issues                 │  │
│  └─-──────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
                      │
            ┌─────────┴──────────┐
            ▼                    ▼
   GitHub REST/GraphQL    Docker daemon (preview containers)
   api.github.com         /var/run/docker.sock
                          ports 7000-7999
```

### Ключові принципи

- **Один моноліт-процес**: FastAPI + Socket.IO + бізнес-логіка в одному uvicorn worker
- **Async скрізь**: HTTP через `httpx.AsyncClient`, БД через `aiosqlite`, LLM через `litellm.acompletion`
- **Background tasks** через FastAPI `BackgroundTasks` — асинхронні цикли орхестрації не блокують HTTP-handler
- **Per-project room** у Socket.IO: `project:{uuid}` — реалтайм-події ізольовано
- **Stateful, але не sharded**: вся пам'ять стану — у SQLite

---

## 4. Структура репозиторію — повна

```
project-automatron-vmark/
├── .env.example                         # Шаблон env-змінних (47 рядків)
├── .github/
│   └── workflows/
│       └── deploy.yml                   # Деплой самого Automatron на 91.98.68.42
├── .gitignore                           # 54 рядки: Python+Node+Docker+IDE+data/+secrets/
├── IMPLEMENTATION_PLAN.md               # ~49 KB, історичний, описує LangGraph-архітектуру
├── Makefile                             # 80 рядків: dev|build|up|test|secrets|clean
├── README.md                            # Quick Start, architecture diagram, links
├── docker-compose.yml                   # orchestrator + web-ui + nginx (profile=production)
├── docker-compose.dev.yml               # overlay: ports 8000,3000
├── docker-compose.prod.yml              # overlay: Traefik labels для automatron.quitcode.com
├── docker/
│   ├── golden-image/Dockerfile          # Ubuntu 24.04 + Node 22 + Python 3.12 + Cline 2.7.0 (legacy)
│   ├── nginx/default.conf               # WS + API + UI proxy
│   ├── orchestrator/Dockerfile          # python:3.12-slim + uv + git + ssh-client
│   └── web-ui/Dockerfile                # Multi-stage node:22-alpine, output: standalone
├── docs/
│   ├── ARCHITECTURE.md                  # Описує (legacy) LangGraph 8-нодну архітектуру
│   ├── DEPLOYMENT.md                    # Інструкція з deploy через docker-compose
│   ├── E2E_REMEDIATION_BACKLOG.md       # Pending fixes after E2E run
│   ├── E2E_TEST_RUN_POSTMORTEM.md       # 2026-03-11/12 тестовий прогон (рукописний)
│   ├── how-it-works.html                # (HTML версія документації)
│   └── PROJECT_BLUEPRINT.md             # ← цей документ
├── orchestrator/
│   ├── README.md
│   ├── pyproject.toml                   # 60 рядків, hatchling, ruff+mypy+pytest
│   ├── orchestrator/                    # Python пакет
│   │   ├── __init__.py
│   │   ├── main.py                      # FastAPI factory + ASGIApp wrapper (78 LOC)
│   │   ├── config.py                    # Pydantic Settings (114 LOC)
│   │   ├── orchestrator.py              # GitHubOrchestrator (1035 LOC) — ядро
│   │   ├── preview.py                   # Local preview engine (196 LOC)
│   │   ├── observability.py             # trace_event helper (48 LOC)
│   │   ├── execution_contract.py        # (legacy) machine-readable contract (379 LOC)
│   │   ├── api/
│   │   │   ├── routes.py                # REST endpoints (463 LOC)
│   │   │   ├── socket_server.py         # Shared sio instance (7 LOC)
│   │   │   ├── webhook_github.py        # GitHub PR webhook (174 LOC)
│   │   │   └── websocket.py             # Socket.IO handlers + emit helpers (189 LOC)
│   │   ├── deployment/
│   │   │   └── manager.py               # SSH-deploy stub (118 LOC, не використовується)
│   │   ├── github/
│   │   │   └── issues.py                # GitHubClient REST/GraphQL (362 LOC)
│   │   ├── github_actions/
│   │   │   └── manager.py               # Environments+Secrets+Workflows (482 LOC)
│   │   ├── llm/
│   │   │   ├── catalog.py               # /v1/models fetch+cache (248 LOC)
│   │   │   ├── configuration.py         # provider/model normalization (99 LOC)
│   │   │   ├── prompts.py               # (legacy) load_prompt (38 LOC)
│   │   │   └── provider.py              # litellm wrapper streaming/non (247 LOC)
│   │   ├── models/
│   │   │   ├── project.py               # Велика SQLite-модель (1075 LOC)
│   │   │   └── session.py               # sessions CRUD (62 LOC, legacy)
│   │   ├── plan_parser/
│   │   │   ├── parser.py                # PLAN.md frontmatter+checkbox (183 LOC, legacy)
│   │   │   └── writer.py                # mark_task_complete (106 LOC, legacy)
│   │   ├── repository/
│   │   │   └── manager.py               # GitHub repo create + git ops (397 LOC)
│   │   └── validation/
│   │       ├── preflight.py             # PreflightService (270 LOC)
│   │       └── runtime.py               # PreviewRuntimeSpec (151 LOC, legacy)
│   ├── prompts/
│   │   ├── architect_github_v1.txt      # (active) GitHub-issues prompt
│   │   ├── architect_v1.txt             # (legacy) PLAN.md+execution_contract prompt
│   │   ├── builder_v1.txt               # (legacy) coder agent rules
│   │   └── reviewer_v1.txt              # (legacy) status classifier
│   ├── scripts/
│   │   ├── init-generic.sh
│   │   ├── init-nextjs.sh               # create-next-app + tailwind + health endpoint
│   │   ├── init-python.sh
│   │   └── init-react-vite.sh
│   ├── tests/                           # 16 файлів pytest
│   └── workspaces/                      # Local cloned repos для preview
├── tmp_start_backend.cmd                # Локальний batch-launcher
└── web-ui/
    ├── next.config.js                   # output: standalone, env: API_URL/WS_URL
    ├── next-env.d.ts
    ├── package.json                     # Next 15, React 19, Zustand 5, socket.io-client 4.8
    ├── postcss.config.js
    ├── tailwind.config.js
    ├── tsconfig.json
    └── src/
        ├── app/
        │   ├── globals.css
        │   ├── layout.tsx
        │   ├── page.tsx                 # Dashboard з grid карток
        │   ├── project/[id]/page.tsx    # Велика сторінка проекту (~1150 LOC)
        │   └── projects/page.tsx
        ├── components/
        │   ├── layout/  (AppLayout, Header, Sidebar)
        │   ├── project/ (ChatPanel, IssueCard, IssuesBoard, NewProjectDialog, PlanEditor, ProjectCard)
        │   └── ui/      (AlertPanel, LogStream, ProgressBar, StatusBadge)
        ├── hooks/
        │   └── useWebSocket.ts          # Хук для всіх WS-подій
        ├── lib/
        │   ├── api.ts                   # fetch-обгортки для всіх REST-маршрутів
        │   ├── llmOptions.ts            # default LLM config
        │   ├── socket.ts                # io() з reconnection
        │   ├── types.ts                 # TS-типи (260 LOC)
        │   └── utils.ts
        └── stores/
            └── projectStore.ts          # Zustand store
```

---

## 5. Конфігурація та змінні середовища

### 5.1 Файл [`config.py`](../orchestrator/orchestrator/config.py)

`Settings(BaseSettings)` читає через Pydantic з `.env` (encoding utf-8). Усі поля:

| Поле / ENV | Default | Призначення |
|---|---|---|
| `openai_api_key` / `OPENAI_API_KEY` | `""` | OpenAI |
| `anthropic_api_key` / `ANTHROPIC_API_KEY` | `""` | Anthropic |
| `google_api_key` / `GOOGLE_API_KEY` | `""` | Google AI |
| `github_token` / `GITHUB_TOKEN` | `""` | PAT з repo+admin:repo_hook+workflow |
| `github_webhook_secret` / `GITHUB_WEBHOOK_SECRET` | `""` | HMAC-secret для webhook |
| `automatron_public_url` / `AUTOMATRON_PUBLIC_URL` | `""` | Для авто-реєстрації webhook'ів |
| `figma_access_token` / `FIGMA_ACCESS_TOKEN` | `""` | Для context-fetch |
| `github_owner` / `GITHUB_OWNER` | `""` | user/org для repo-create |
| `github_owner_type` / `GITHUB_OWNER_TYPE` | `"user"` | `user` або `org` |
| `github_default_org` / `GITHUB_DEFAULT_ORG` | `""` | Fallback для bare repo names |
| `github_api_url` / `GITHUB_API_URL` | `https://api.github.com` | Для GHE можна перенаправити |
| `github_repo_visibility` | `"private"` | private/public |
| `github_environment_name` | `"production"` | GH Environments name |
| `github_actions_ci_workflow_name` | `"CI"` | Для status sync |
| `github_actions_deploy_workflow_name` | `"Deploy"` | |
| `git_author_name` | `"Automatron Bot"` | git commits |
| `git_author_email` | `"automatron@example.local"` | |
| `architect_model` | `"gpt-5.3-codex"` | Default architect |
| `architect_prompt_version` | `"v1"` | |
| `builder_model` | `"gpt-5.3-codex"` | |
| `builder_cline_timeout` | `900` | Legacy Cline timeout |
| `reviewer_model` | `"gpt-5.3-codex"` | |
| `golden_image` | `"automatron/golden:latest"` | Legacy |
| `workspace_base_path` | `"/var/automatron/workspaces"` | На Windows автоматично перевизначається на `cwd/workspaces` через `workspace_base_dir` |
| `port_range_start..end` | `7000..7999` | Preview ports |
| `deploy_ssh_key_path` / `deploy_ssh_options` | `""` | Legacy SSH-deploy |
| `sqlite_db_path` | `./data/automatron.db` | Авто-нормалізує відносні шляхи відносно `orchestrator/` |
| `checkpoint_db_path` | `./data/checkpoints.db` | LangGraph (не використовується) |
| `host` / `port` | `0.0.0.0` / `8000` | uvicorn |
| `debug` | `False` | Парсить `1/true/yes/on/dev` як True |

Спеціальна логіка:
- `_parse_debug_flag` ([config.py:62](../orchestrator/orchestrator/config.py#L62)) — толерантний до різних рядкових представлень
- `_normalize_sqlite_paths` — створює директорії автоматично
- `workspace_base_dir` — на Windows (`os.name == 'nt'`) ігнорує POSIX-шляхи і використовує `cwd/workspaces`

### 5.2 Файл `.env.example`

Базові змінні для копіювання в `.env`:
```env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=AI...
ARCHITECT_MODEL=gpt-5.3-codex
BUILDER_MODEL=gpt-5.3-codex
REVIEWER_MODEL=gpt-5.3-codex
GITHUB_TOKEN=ghp_...
GITHUB_OWNER=your-org-or-user
GITHUB_OWNER_TYPE=user
GITHUB_API_URL=https://api.github.com
GITHUB_REPO_VISIBILITY=private
GITHUB_ENVIRONMENT_NAME=production
GITHUB_ACTIONS_CI_WORKFLOW_NAME=CI
GITHUB_ACTIONS_DEPLOY_WORKFLOW_NAME=Deploy
GIT_AUTHOR_NAME=Automatron Bot
GIT_AUTHOR_EMAIL=automatron@example.local
SQLITE_DB_PATH=./data/automatron.db
CHECKPOINT_DB_PATH=./data/checkpoints.db
GOLDEN_IMAGE=automatron/golden:latest
WORKSPACE_BASE_PATH=/var/automatron/workspaces
PORT_RANGE_START=7000
PORT_RANGE_END=7999
HOST=0.0.0.0
PORT=8000
DEBUG=true
BUILDER_CLINE_TIMEOUT=300
DEPLOY_SSH_KEY_PATH=
DEPLOY_SSH_OPTIONS=
```

Додатково в [docker-compose.yml](../docker-compose.yml) проставляється:
- `SQLITE_DB_PATH=/app/data/automatron.db`
- `CHECKPOINT_DB_PATH=/app/data/checkpoints.db`
- `WORKSPACE_BASE_PATH=/var/automatron/workspaces`
- `GOLDEN_IMAGE=automatron/golden:latest`

### 5.3 Frontend env

[`web-ui/next.config.js`](../web-ui/next.config.js) — два публічні env:
- `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`)
- `NEXT_PUBLIC_WS_URL` (default `ws://localhost:8000`)

В docker-compose `web-ui` build args передають `API_URL`/`WS_URL` змінні (default `http://localhost:8000`/`ws://localhost:8000`; у проді — `https://automatron.quitcode.com`/`wss://automatron.quitcode.com`).

---

## 6. Backend: Orchestrator (Python/FastAPI)

### 6.1 Точка входу

[`orchestrator/main.py`](../orchestrator/orchestrator/main.py):

```python
fastapi_app = create_app()              # Тільки REST + healthcheck
combined_app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
app = combined_app                      # ← експортується як ASGI app
```

`uvicorn orchestrator.main:app` отримує combined ASGI, де Socket.IO керує своїми шляхами (`/socket.io/*`), а решта йде до FastAPI.

`lifespan` контекст: на старті — `init_db(settings.sqlite_db_path)`.

CORS налаштовано як `allow_origins=["*"]` ([main.py:42](../orchestrator/orchestrator/main.py#L42)) — прийнятно для приватного середовища.

REST префікс `/api`, прикріплено два роутери:
- `api_router` ([routes.py](../orchestrator/orchestrator/api/routes.py))
- `webhook_router` ([webhook_github.py](../orchestrator/orchestrator/api/webhook_github.py))

Health: `GET /health → {"status": "ok"}`.

### 6.2 Залежності — [`pyproject.toml`](../orchestrator/pyproject.toml)

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "langgraph>=1.0.9",                  # ← legacy, не використовується в коді
    "langgraph-checkpoint-sqlite>=3.0.0", # ← legacy
    "langchain-core>=0.3.0",             # ← використовується для HumanMessage/SystemMessage
    "litellm>=1.55.0",
    "docker>=7.0.0",                     # ← не імпортується в активному коді
    "python-socketio>=5.11.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "python-frontmatter>=1.1.0",         # ← в plan_parser
    "aiosqlite>=0.20.0",
    "httpx>=0.27.0",
    "PyNaCl>=1.5.0",                     # ← для шифрування GH-secrets
    "python-multipart>=0.0.9",           # ← для UploadFile
]
[project.optional-dependencies]
dev = ["pytest>=8.0.0", "pytest-asyncio>=0.24.0", "ruff>=0.8.0", "mypy>=1.13.0"]
```

Python 3.12+, build через `hatchling`.

### 6.3 Архітектурні шари

```
api/             ← HTTP/WS handler shell, без бізнес-логіки
  routes.py
  websocket.py
  webhook_github.py
  socket_server.py

orchestrator.py  ← Domain layer: GitHubOrchestrator
preview.py       ← Окремий модуль для local preview

github/          ← REST/GraphQL клієнт GitHub
  issues.py

repository/      ← Git workspace + repo provisioning (legacy шлях)
  manager.py

github_actions/  ← Environments+Secrets+Workflow runs (для legacy CI/CD)
  manager.py

deployment/      ← SSH-deploy виконавець (legacy)
  manager.py

llm/             ← LLM-провайдер абстракція
  configuration.py
  catalog.py
  provider.py
  prompts.py

models/          ← SQLite репозиторій
  project.py
  session.py

validation/      ← Preflight + runtime spec resolver
  preflight.py
  runtime.py

plan_parser/     ← PLAN.md (legacy, для старого графа)
  parser.py
  writer.py

execution_contract.py  ← (legacy) Machine-readable contract builder
observability.py       ← trace_event helper
config.py              ← Settings
main.py                ← App factory
```

---

## 7. REST API — повний реєстр ендпоінтів

Усі під префіксом `/api`. Body — JSON, окрім файлових ендпоінтів.

### 7.1 Проекти

| Метод | Шлях | Body | Відповідь | Коментар |
|---|---|---|---|---|
| GET | `/projects` | – | `Project[]` | Сортування DESC by `created_at` |
| POST | `/projects` | `CreateProjectRequest` | `Project` | Створення; парсить `repo_url` |
| GET | `/projects/{id}` | – | `Project` | 404 якщо не існує |
| DELETE | `/projects/{id}` | – | `{status: "deleted", project_id}` | Soft-delete: `status='deleted'`, `stage='error'` |
| POST | `/projects/{id}/start` | – | `{status: "started"}` | Перед стартом — preflight; 409 при `blocking` |
| POST | `/projects/{id}/stop` | – | `{status: "stopped"}` | `status='stopped'`, `stage='stopped'` |
| POST | `/projects/{id}/approve-plan` | `{feedback?}` | `{status: "resuming"}` | Тригерить `apply_plan` |
| POST | `/projects/{id}/approve` | те саме | те саме | Alias для `approve-plan` |
| POST | `/projects/{id}/preflight` | `{phase: "start"\|"deploy"}` | `PreflightResponse` | Не стартує проект |

### 7.2 Plan

| Метод | Шлях | Body | Відповідь |
|---|---|---|---|
| GET | `/projects/{id}/plan` | – | `{plan_md}` |
| PUT | `/projects/{id}/plan` | `{plan_md}` | `{status: "updated"}` |

### 7.3 LLM конфіг та каталог

| Метод | Шлях | Body | Відповідь |
|---|---|---|---|
| GET | `/llm/providers?force_refresh=` | – | `ProviderModelCatalog[]` |
| GET | `/llm/providers/{provider}/models?force_refresh=` | – | `ProviderModelCatalog` |
| PUT | `/projects/{id}/llm-config` | `ProjectLlmConfigRequest` | `Project` |

### 7.4 Issues / GitHub-флоу

| Метод | Шлях | Body | Відповідь |
|---|---|---|---|
| GET | `/projects/{id}/issues` | – | `GithubIssue[]` |
| POST | `/projects/{id}/sync-issues` | – | `{status: "syncing"}` (фон) |
| POST | `/projects/{id}/audit` | – | `{status: "auditing"}` (фон) |
| POST | `/projects/{id}/assign-copilot` | – | `{assigned, failed}` (sync) |
| POST | `/projects/{id}/issues/{issue_number}/assign-copilot` | – | `{assigned, issue_number}` |
| POST | `/projects/{id}/review-pr` | `{issue_number, pr_number}` | `{status: "reviewing"}` (фон) |
| POST | `/projects/{id}/figma-file` | `multipart/form-data: file` | `{status, chars, filename}` |

### 7.5 Логи / трасування / preview / deploy

| Метод | Шлях | Відповідь |
|---|---|---|
| GET | `/projects/{id}/logs` | `activity_logs` або (legacy) `task_logs` |
| GET | `/projects/{id}/sessions` | `Session[]` |
| GET | `/projects/{id}/chat-history` | `ChatMessage[]` |
| GET | `/projects/{id}/preview-url` | `{preview_url}` |
| GET | `/projects/{id}/deploy-runs` | `DeployRun[]` |
| GET | `/projects/{id}/trace` | `TraceEvent[]` |
| PUT | `/projects/{id}/deploy-target` | `DeployTargetRequest` → `{status}` |

### 7.6 Webhook

| POST | `/webhooks/github` | GitHub PR payload, заголовки `X-Hub-Signature-256`, `X-GitHub-Event` |

### 7.7 Health

| GET | `/health` | `{status: "ok"}` (без `/api` префіксу) |

### 7.8 Pydantic-моделі запитів/відповідей

[`routes.py:54-184`](../orchestrator/orchestrator/api/routes.py#L54-L184):

- `RoleLlmConfig {provider, model}`
- `ProjectLlmConfigRequest {architect, builder, reviewer}`
- `CreateProjectRequest {name, repo_url, intake_text?, description?, source="manual", source_ref?, llm_config?, figma_urls=[]}`
- `ReviewPRRequest {issue_number, pr_number}`
- `UpdatePlanRequest {plan_md}`
- `ApproveRequest {feedback?}`
- `DeployTargetRequest {auth_mode: "ssh_key"|"password", host, port=22, user, deploy_path, auth_reference?, ssh_private_key?, ssh_password?, known_hosts?, env_content?, app_url?, health_path="/api/health"}`
- `ProjectResponse` — 35 полів, повне відображення `projects` + computed `deploy_target_summary`, `description = intake_text`, `preview_port = port`
- `PreflightCheckResponse {code, status, message, details}`
- `PreflightResponse {phase, ok, blocking, checks}`

---

## 8. WebSocket: події і кімнати

Сервер: [`socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")`](../orchestrator/orchestrator/api/socket_server.py#L7).

### 8.1 Connection

[`@sio.on("connect")`](../orchestrator/orchestrator/api/websocket.py#L20):
- Парсить query string `?projectId=X` (або `project_id`)
- Якщо є — автоматично додає sid у кімнату `project:{id}`

### 8.2 Клієнтські події (UI → Server)

| Event | Payload | Handler |
|---|---|---|
| `connect` | – | Auto-join з query |
| `disconnect` | – | Лог |
| `join` | `{project_id}` | `enter_room` |
| `leave` | `{project_id}` | `leave_room` |
| `chat:message` | `{project_id, message}` | Зберігає в `chat_messages`, трасує `chat.message.received`, повертає echo `architect:message` (заглушка) |

### 8.3 Серверні події (Server → UI)

Усі емітяться в кімнату `project:{project_id}` через emit-helpers у [`websocket.py`](../orchestrator/orchestrator/api/websocket.py):

| Event | Payload | Тригер |
|---|---|---|
| `architect:message` | `{project_id, content, is_streaming}` | `emit_architect_message` (final), `emit_architect_chunk` (token) |
| `builder:log` | `{project_id, task_index, task_text, output, status}` | `emit_builder_log` — викликається `_log` у GitHubOrchestrator |
| `status:update` | `{project_id, status, stage, progress, preview_url?}` | `emit_status_update` |
| `human:required` | `{project_id, reason, stage?}` | `emit_human_required` |
| `run:error` | `{project_id, message, stage}` | `emit_error` (при exceptions) |
| `plan:updated` | `{project_id, plan_md}` | `emit_plan_updated` |
| `issues:updated` | `{project_id, issues}` | Після `apply_plan`, `sync_issues`, webhook PR |
| `pr:review_ready` | `{project_id, issue_number, pr_number, passed, summary}` | Після `review_pr` |

### 8.4 Клієнтський listener-набір

[`useWebSocket.ts`](../web-ui/src/hooks/useWebSocket.ts) підписується на всі вище — стрімінг chunks ігнорується (`is_streaming==true`). Reconnection: `attempts=10`, `delay=1s..10s`.

---

## 9. Дані та схема SQLite

База: `./data/automatron.db`, режим `journal_mode=WAL` (set on init).

### 9.1 Таблиця `projects`

Створюється в [`init_db`](../orchestrator/orchestrator/models/project.py#L183), розширюється `_ensure_columns` (лінива міграція через `ALTER TABLE IF NOT EXISTS`).

Базові колонки:

| Колонка | Тип | Default | Призначення |
|---|---|---|---|
| `id` | TEXT PK | – | UUID |
| `name` | TEXT NOT NULL | – | |
| `status` | TEXT NOT NULL | `'pending'` | ProjectStatus |
| `project_stage` | TEXT NOT NULL | `'intake'` | ProjectStage |
| `intake_text` | TEXT NOT NULL | `''` | Repo URL/опис |
| `intake_source` | TEXT NOT NULL | `'manual'` | manual/integration |
| `source_ref` | TEXT | NULL | |
| `plan_md` | TEXT | NULL | Markdown plan |
| `stack_config_json` | TEXT | `'{}'` | Stack metadata |
| `llm_config_json` | TEXT | NULL | Per-role LLM config |
| `execution_contract_json` | TEXT | `'{}'` | (legacy) |
| `decision_log_json` | TEXT | `'[]'` | (legacy) |
| `plan_delta_history_json` | TEXT | `'[]'` | (legacy) |
| `task_validation_result_json` | TEXT | `'{}'` | (legacy) |
| `last_escalation_json` | TEXT | `'{}'` | (legacy) |
| `builder_report_json` | TEXT | `'{}'` | (legacy) |
| `repo_name` | TEXT | NULL | Generated repo name |
| `repo_url` | TEXT | NULL | https URL |
| `repo_clone_url` | TEXT | NULL | clone URL |
| `default_branch` | TEXT | NULL | `'main'` |
| `develop_branch` | TEXT | NULL | `'develop'` |
| `feature_branch` | TEXT | NULL | `feature/N-slug` |
| `repo_ready` | INT BOOL | 0 | |
| `contract_version` | INT | 0 | |
| `active_task_id` | TEXT | NULL | |
| `task_attempt_count` | INT | 0 | |
| `container_id` | TEXT | NULL | (legacy) |
| `port` | INT | NULL | Allocated preview port |
| `preview_url` | TEXT | NULL | http://localhost:port |
| `preview_status` | TEXT | `'pending'` | pending/ready/failed |
| `preview_checked_at` | TEXT | NULL | ISO timestamp |
| `preview_metadata_json` | TEXT | `'{}'` | |
| `ci_status` | TEXT | `'not_configured'` | |
| `ci_run_id`, `ci_run_url` | TEXT | NULL | |
| `deploy_status` | TEXT | `'not_configured'` | |
| `deploy_run_url`, `deploy_commit_sha` | TEXT | NULL | |
| `deploy_target_json` | TEXT | NULL | Кодований DeployTargetRequest |
| `github_environment_name` | TEXT | `'production'` | |
| `last_workflow_sync_at` | TEXT | NULL | |
| `plan_approved`, `preview_approved` | INT BOOL | 0 | |
| `plan_approved_at`, `preview_approved_at` | TEXT | NULL | |
| `approval_history_json` | TEXT | `'[]'` | List of {type, approved, feedback, timestamp} |
| `last_deploy_at`, `last_deploy_run_id` | TEXT | NULL | |
| `github_repo_owner`, `github_repo_name` | TEXT | NULL | Окремо від repo_name (для existing repos) |
| `issue_plan_json` | TEXT | `'{}'` | Architect's plan |
| `figma_urls_json` | TEXT NOT NULL | `'[]'` | |
| `figma_file_context` | TEXT NOT NULL | `''` | Summary з .fig |
| `created_at`, `updated_at` | TEXT NOT NULL | – | ISO UTC |

**JSON-поля** автоматично deserializуються в `_serialize_project_row` ([project.py:127](../orchestrator/orchestrator/models/project.py#L127)). **BOOL_FIELDS** = `{repo_ready, plan_approved, preview_approved}`.

`update_project(**kwargs)` має alias-нормалізацію: `stack_config` → `stack_config_json`, `llm_config` → нормалізований через `normalize_llm_config`, тощо.

### 9.2 Таблиця `sessions` (legacy)

```sql
id TEXT PK, project_id TEXT FK, thread_id TEXT, phase TEXT,
started_at TEXT NOT NULL, ended_at TEXT
```
CRUD у [`models/session.py`](../orchestrator/orchestrator/models/session.py). Не наповнюється у GitHub-режимі.

### 9.3 Таблиця `task_logs` (legacy)

```sql
id, session_id FK, task_index, task_text, status, cline_output, duration_s, created_at
```
Залишилася для сумісності зі старим UI.

### 9.4 Таблиця `chat_messages`

```sql
id TEXT PK, project_id TEXT FK, role TEXT, content TEXT, created_at TEXT
```
`role`: `user | architect | system`.

### 9.5 Таблиця `deploy_runs`

```sql
id TEXT PK, project_id FK, status TEXT, branch TEXT, output TEXT,
summary_json TEXT, created_at TEXT, deployed_at TEXT
```
`upsert_deploy_run` зберігає `created_at` при INSERT OR REPLACE.

### 9.6 Таблиця `trace_events`

```sql
id TEXT PK, project_id FK, session_id TEXT, actor TEXT, event_type TEXT,
stage TEXT, payload_json TEXT NOT NULL, created_at TEXT
```
Event types зустрічаються:
- `chat.message.received`, `architect.run.started`, `architect.run.completed`
- `apply_plan.started`, `apply_plan.completed`
- `pr.review.completed`
- `llm.call.started/completed/failed`
- `llm.stream.started/completed/failed`

`payload_json` тримиться через [`_trim`](../orchestrator/orchestrator/observability.py#L14) (рекурсивно, ліміт `12000` символів на string).

### 9.7 Таблиця `activity_logs`

```sql
id, project_id FK, seq INT, task_text, output, status DEFAULT 'INFO', created_at
```
Лінійний журнал з номером — те, що бачить користувач у вкладці Activity. Status: `INFO | RUNNING | SUCCESS | AMBIGUITY | BLOCKER | ERROR`.

### 9.8 Таблиця `github_issues`

```sql
id TEXT PK, project_id FK, issue_number INT NOT NULL, title TEXT,
epic TEXT, story TEXT, status TEXT DEFAULT 'open',
pr_number INT, pr_url TEXT, pr_review_json TEXT,
copilot_workspace_url TEXT, created_at, updated_at
```
`status`: `open | pr_open | pr_reviewed | merged | closed`.

`find_github_issue_by_repo(owner, repo, issue_number)` — JOIN з `projects` через `github_repo_owner/name` для веб-хука.

---

## 10. GitHub-інтеграція (Issues, PR, Webhooks, Actions)

### 10.1 [`GitHubClient`](../orchestrator/orchestrator/github/issues.py) — REST + GraphQL

Headers: `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Authorization: Bearer {token}` (якщо є).

Методи:

#### Files
- `read_file(o, r, path)` — `GET /repos/o/r/contents/path`, base64-decode; повертає `None` на 404 чи directory
- `push_file(o, r, path, content, msg, branch="main")` — `PUT /repos/o/r/contents/path`. Спершу `GET` для отримання `sha` (якщо існує), потім PUT із base64-encoded content. Raises на статус !=200/201

#### Milestones
- `create_milestone(o, r, title, desc)` — `POST /repos/o/r/milestones`. На 422 (existed) — `list_milestones` + filter by title
- `list_milestones(o, r)` — `GET ?state=all&per_page=100`

#### Labels
- `ensure_label(o, r, name, color="ededed")` — `POST /repos/o/r/labels`. Допускає 201 (created) або 422 (exists)

#### Issues
- `create_issue(o, r, title, body, milestone_number?, labels?, assignees?)` — POST з опціональними полями
- `trigger_copilot_agent(o, r, issue_number)` — POST на `/repos/o/r/issues/N/assignees` з `assignees=["copilot-swe-agent[bot]"]` та `agent_assignment={target_repo, base_branch:"main"}`
- `list_issues(o, r, milestone?, state="all")` — фільтрує PR (мають поле `pull_request`)
- `get_issue(o, r, n)` — single

#### Pull Requests
- `list_prs(o, r, state="open")` — `GET /repos/o/r/pulls?state=&per_page=100`
- `get_pr_diff(o, r, n)` — окремий клієнт з `Accept: application/vnd.github.diff`, повертає raw text
- `post_pr_comment(o, r, n, body)` — `POST /repos/o/r/issues/N/comments` (PR коментарі це той самий ендпоінт)
- `find_pr_for_issue(o, r, issue_number)` — двопрохідний пошук:
  1. `GET /repos/o/r/issues/N/timeline` з `Accept: application/vnd.github.mockingbird-preview+json` — шукає `event=cross-referenced` з PR-source
  2. Fallback: `list_prs(open)` + `list_prs(closed)` → пошук по тілу `closes #N`/`fixes #N`/`resolves #N`/`#N`

#### Webhooks
- `register_webhook(o, r)` — idempotent. Перевіряє існуючі hooks за URL, створює з `events=["pull_request"]`. Повертає `"registered"|"already_exists"|"skipped"|"error: ..."`. `skipped` при відсутності `AUTOMATRON_PUBLIC_URL`

#### Deployments (legacy auto-detection)
- `get_preview_url_from_deployments(o, r)` — повертає environment_url першого SUCCESS з пріоритетом `production > preview/staging > any`. Не використовується в активному коді (заміненe local preview)

### 10.2 GraphQL: closing-issues lookup

[`webhook_github._linked_issue_numbers`](../orchestrator/orchestrator/api/webhook_github.py#L48) використовує:

```graphql
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      closingIssuesReferences(first: 25) {
        nodes { number }
      }
    }
  }
}
```

на `https://api.github.com/graphql`. Це працює навіть коли Copilot не пише `Closes #N`.

### 10.3 [`GitHubActionsManager`](../orchestrator/orchestrator/github_actions/manager.py) (для legacy CI/CD generated repos)

- `ensure_environment(repo, env)` — `PUT /repos/{owner}/{repo}/environments/{env}` з порожнім body
- `get_environment_public_key(repo, env)` — `GET .../environments/{env}/secrets/public-key`
- `upsert_environment_secrets(repo, deploy_target, env)` — шифрує через `nacl.public.SealedBox` з base64 public_key, потім `PUT .../secrets/{name}`
- `build_environment_secrets(deploy_target)` — мапить `host→AUTOMATRON_DEPLOY_HOST`, `port→...PORT`, `user→...USER`, `deploy_path→..._PATH`, `ssh_private_key→...SSH_PRIVATE_KEY`, `ssh_password→...SSH_PASSWORD`, `known_hosts→...KNOWN_HOSTS`, `env_content→...ENV_FILE`, `app_url→...APP_URL`, `health_path→...HEALTH_PATH`
- `sync_repository(repo, feature, develop, default)` — `GET /repos/.../actions/runs?per_page=50`, селектить per-workflow per-branch run, мапить `status+conclusion` → `pending|queued|running|succeeded|deployed|failed`
- `workflow_files()` — повертає dict `{".github/workflows/ci.yml": "...", "deploy.yml": "..."}` з готовим bash-скриптом для setup-node@v4, npm/yarn detection, pytest detection, SSH/sshpass deploy через `tar+scp+docker compose`, health check через curl з retry

### 10.4 [`RepositoryManager`](../orchestrator/orchestrator/repository/manager.py) (legacy шлях створення репозиторію)

- `create_remote_repository(project_id, name)` — `POST /user/repos` (або `/orgs/{owner}/repos` якщо `owner_type=org`), payload `{name, private, auto_init=false, description}`. На 422 (exists) — fallback до `_get_repository`
- `initialize_workspace_repository(project_id, project_name, metadata)` — `git init && config user/email && branch -M main && remote add origin URL && add -A && commit --allow-empty && push -u origin main && checkout -B develop && push -u && checkout -B feature/N-slug && push -u`
- `commit_workspace_changes(project_id, message, branch?)` — `git add -A && git commit && git push origin {branch}`, повертає SHA
- `merge_branch(project_id, source, target, message)` — `git fetch && checkout target && pull && merge --no-ff source && push`
- `ensure_deploy_supporting_docs(project_id, project_name)` — створює `.env.example`, `deploy/docker-compose.yml`, `DEPLOY.md`, `.github/workflows/{ci,deploy}.yml` з шаблонів `actions_manager.workflow_files()`
- `_git(cwd, args, authenticated=False, check=True)` — обгортка над `subprocess.run`, з `http.extraHeader=Authorization: Basic base64(x-access-token:TOKEN)` для autenthenticated requests
- `_slugify(value)` — lowercase, non-alphanumeric → `-`, strip

### 10.5 [`DeploymentManager`](../orchestrator/orchestrator/deployment/manager.py) (legacy)

`deploy(repo_clone_url, target, branch="main")`:

```bash
mkdir -p {path} && \
if [ ! -d {path}/.git ]; then git clone --branch {branch} {url} {path}; fi && \
cd {path} && git fetch origin && git checkout {branch} && git pull origin {branch} && \
docker compose -f deploy/docker-compose.yml up -d --build
```

Виконується через `ssh -p PORT -o StrictHostKeyChecking=no [-i KEY] user@host '...'`. Опціонально `_health_check` робить GET на `app_url + health_path`.

---

## 11. LLM-шар: провайдери, ролі, каталог, промпти

### 11.1 [`configuration.py`](../orchestrator/orchestrator/llm/configuration.py)

**Підтримувані провайдери**: `openai | anthropic | google` ([SUPPORTED_PROVIDERS](../orchestrator/orchestrator/llm/configuration.py#L12)).

**Ролі**: `architect | builder | reviewer`.

`infer_provider_from_model(model)`:
- `anthropic/...` або `claude` у назві → `anthropic`
- `gemini/...`, `google/...`, `gemini` → `google`
- `openai/...`, `gpt-...`, `o...` → `openai`
- Default → `openai`

`normalize_model_identifier(provider, model)`:
- Якщо вже `prefix/model` — повертає як є
- `anthropic` без префікса → `anthropic/{model}`
- `google` без префікса → `gemini/{model}`
- `openai` → залишає як є (litellm розуміє `gpt-*` напряму)

`provider_api_key(provider)` повертає відповідний key з settings.

`default_llm_config()` — `{role: {provider: inferred, model: settings.{role}_model}}`.

`normalize_llm_config(llm_config)` — заповнює дефолтами при відсутності поля, нормалізує provider+model.

### 11.2 [`catalog.py`](../orchestrator/orchestrator/llm/catalog.py) — динамічний каталог моделей

Кеш: in-memory `_catalog_cache: dict[provider, {models, fetched_at, fetched_at_epoch, error}]`, TTL = 300 сек.

Методи fetch:
- **OpenAI**: `GET https://api.openai.com/v1/models`, фільтр `_supports_text_generation_openai` — допускає `gpt-, chatgpt-, o1, o3, o4, codex-` префікси, виключає `audio, transcribe, tts, moderation, embedding, image, whisper, search, realtime, vision-preview, instruct`
- **Anthropic**: `GET https://api.anthropic.com/v1/models` з пагінацією через `after_id` (до 5 сторінок), header `anthropic-version: 2023-06-01`. ID нормалізується до `anthropic/{id}`. Label = `display_name`
- **Google**: `GET https://generativelanguage.googleapis.com/v1beta/models?key=...` з `pageToken`. Фільтр: `supportedActions/supported_generation_methods` повинен містити `generateContent`. ID → `gemini/{name_without_models_prefix}`

`get_provider_model_catalog(provider, force_refresh=False)`:
- На відсутність ключа: `{configured: false, models: [], error: "Provider API key is not configured"}`
- На fetch error: повертає cached models якщо є, інакше порожній з `error`

### 11.3 [`provider.py`](../orchestrator/orchestrator/llm/provider.py) — litellm wrapper

```python
litellm.set_verbose = False
```

`_cap_max_tokens(model, max_tokens)` — обмежує `max_tokens` для GPT-4 моделей (4096 cap).

`_completion_kwargs(model, temperature, max_tokens, stream)` — спеціальна гілка для `gpt-5*` моделей: вимушено `temperature=1` (вони відхиляють інші значення).

`_messages_to_dicts(messages)` — конвертує `langchain_core.messages.{System,Human,AI}Message` → `[{role, content}]`.

`call_llm(messages, model?, temperature=0.3, max_tokens=16384, trace_context?)`:
1. Емітує `llm.call.started` в `trace_events` з повним messages payload
2. `await litellm.acompletion(messages, **kwargs)` (без stream)
3. Логує usage (prompt_tokens, completion_tokens)
4. Емітує `llm.call.completed` з повною response та usage
5. На exception — `llm.call.failed`

`call_llm_streaming(...)` — те саме, але `stream=True`, yields chunks. Емітує `llm.stream.{started,completed,failed}`.

### 11.4 Промпти — [`orchestrator/prompts/`](../orchestrator/prompts/)

Існує 4 файли:

#### `architect_github_v1.txt` (active, для GitHub-issues mode)

Інструктує architect видати **3 блоки** в одній відповіді:

1. ```` ```json:issue_plan ```` — `{epics: [{title, description, stories: [{title, tasks: [{title, file?, component?, description, implementation_notes[≤3], acceptance_criteria[≤3], validation}]}]}]}`
2. ```` ```markdown:architecture ```` — System purpose, stack, components, data model, folder, integrations
3. ```` ```markdown:stories ```` — `## Epic: ...\n### Story: ...\n- [ ] Task: ...`

**Правила**:
- Tasks атомарні (30-90 хв)
- Залежність-впорядковані
- Кожен task має `validation` (npm run build, pytest, etc)
- `implementation_notes` ≤ 3 елементів, конкретні шляхи/функції
- `acceptance_criteria` ≤ 3 testable
- БЕЗ infrastructure tasks (CI/CD, Dockerfile)
- БЕЗ fabricated requirements (тільки з README/PRD)
- Total JSON ≤ 20K токенів
- Block 1 (json:issue_plan) видається ПЕРШИМ

#### `architect_v1.txt` (legacy)

Описує старий graph-flow з PLAN.md+execution_contract.json+plan_delta.json. Включає правила для Prisma 5/7, Tailwind v4 zero-config. Активно використовувалось разом з Cline builder.

#### `builder_v1.txt` (legacy)

Інструктує coder agent для Cline CLI: атомарне виконання 1 task за раз, не змінювати архітектуру, бігти `validation_commands` перед completion.

#### `reviewer_v1.txt` (legacy)

Класифікатор статусу для Cline output: `SUCCESS | BLOCKER | AMBIGUITY | SILENT_DECISION` з детальною матрицею правил. У GitHub-mode не використовується — там reviewer prompt хардкоджений у `review_pr` методі.

### 11.5 Як LLM використовується в активному коді

| Місце | Промпт | Модель |
|---|---|---|
| `analyze_and_plan` | `architect_github_v1.txt` | `llm_config.architect.model` (streaming, 32768 max) |
| `audit_codebase` reviewer | inline (`"You are an expert code reviewer..."`) | `llm_config.reviewer.model` (4096 max) |
| `audit_codebase` architect | `architect_github_v1.txt` + custom user-msg | `llm_config.architect.model` (16384 max, streaming) |
| `review_pr` | inline (`"You are a code reviewer..."`) | `llm_config.reviewer.model` (2048 max) |

---

## 12. Local Preview Engine

[`preview.py`](../orchestrator/orchestrator/preview.py).

### 12.1 Алгоритм

```python
async def run_preview_locally(project_id, owner, repo) -> str | None:
    workspace = settings.workspace_base_dir / project_id
    repo_dir = workspace / "repo"

    # 1. Clone or pull
    clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    if (repo_dir / ".git").exists():
        git pull --ff-only
    else:
        git clone {clone_url} {repo_dir}

    # 2. Detect type from package.json
    project_type = _detect_project_type(repo_dir)
    if project_type == "unknown": return None

    # 3. Generate Dockerfile if missing (per-stack template)
    _ensure_dockerfile(repo_dir, project_type)

    # 4. Allocate port
    port = _find_free_port()  # scans 7000..7999

    # 5. Build & run
    docker rm -f preview-{project_id}
    docker build -t automatron-preview-{project_id} .
    internal_port = _detect_internal_port(repo_dir)  # parses EXPOSE line
    docker run -d --name preview-{project_id} \
        -p {port}:{internal_port} \
        --restart unless-stopped \
        automatron-preview-{project_id}

    # 6. Health-poll up to 60s (20×3s)
    preview_url = f"http://localhost:{port}"
    for i in range(20):
        await asyncio.sleep(3)
        if (await httpx.get(preview_url)).status_code < 500:
            return preview_url
    return preview_url  # повертаємо anyway
```

### 12.2 Згенеровані Dockerfile-шаблони

**nextjs** — multi-stage не використовується, простий:
```Dockerfile
FROM node:22-alpine
WORKDIR /app
COPY . .
RUN npm install
RUN npm run build
EXPOSE 3000
CMD ["npm", "start"]
```

**vite**:
```Dockerfile
FROM node:22-alpine
WORKDIR /app
COPY . .
RUN npm install
RUN npm run build
RUN npm install -g serve
EXPOSE 3000
CMD ["serve", "-s", "dist", "-l", "3000"]
```

**node** — без `npm run build`.

**python**:
```Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install --no-cache-dir -e . 2>/dev/null || true
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 12.3 Detection logic

`_detect_project_type` — JSON-парсинг `package.json` deps:
- `next` ∈ deps → `nextjs`
- `vite` ∈ deps або `@vitejs/plugin-react` → `vite`
- Інакше — `node` (якщо є package.json)
- `pyproject.toml` або `requirements.txt` — `python`
- Else — `unknown`

`_detect_internal_port` — парсить `EXPOSE` директиву з Dockerfile (default 3000).

`_find_free_port` — `socket.connect_ex` test на діапазоні (settings.port_range_start..end).

---

## 13. Frontend: Next.js Web UI

### 13.1 Стек

- Next.js 15 App Router (`output: "standalone"` у [next.config.js](../web-ui/next.config.js))
- React 19
- TypeScript 5.7
- Tailwind CSS 3.4 + `@tailwindcss/typography`
- Zustand 5 (state management)
- socket.io-client 4.8
- `lucide-react` (icons), `react-markdown` (rendering plans), `date-fns`, `clsx`, `tailwind-merge`

### 13.2 Сторінки

- `/` ([app/page.tsx](../web-ui/src/app/page.tsx)) — Dashboard з grid карток `ProjectCard` + `NewProjectDialog`. Викликає `useWebSocket()` без projectId — підписується на дашбордні події (нічого не приходить, бо емісії per-room)
- `/project/[id]` ([app/project/[id]/page.tsx](../web-ui/src/app/project/[id]/page.tsx)) — головна сторінка. ~1150 LOC, 6 вкладок:
  - **chat** — `ChatPanel` з історією + `sendMessage`
  - **plan** — `PlanEditor` (markdown textarea + react-markdown preview)
  - **issues** — `IssuesBoard` (групування по epic, кнопки sync/audit/review/assign)
  - **preview** — iframe з `preview_url` або плейсхолдер
  - **activity** — `LogStream` зі статусними бейджами
  - **deploy** — форма `DeployTargetRequest` + history `deployRuns`

Layout: `AppLayout` з `Header` + `Sidebar`.

### 13.3 [`projectStore`](../web-ui/src/stores/projectStore.ts)

Велике Zustand-сховище зі станом:
- `projects, currentProject, chatMessages, builderLogs, deployRuns, issues, planMd`
- `isConnected, isLoading, error`
- `humanRequired, humanReason, humanStage` (для AlertPanel)
- `progress: {total, completed, percentage}`

Дії:
- `fetchProjects, fetchProject, fetchChatHistory, fetchLogs, fetchDeployRuns, fetchPlan, fetchIssues`
- `createProject, startProject, stopProject`
- `approvePlan, approvePreview, deployProject, syncCicd, restartPreview`
- `updatePlan, updateProjectLlmConfig, updateDeployTarget`
- `syncIssues, auditProject, assignToCopilot, assignIssueToCopilot, triggerPRReview`
- `addBuilderLog, addChatMessage, patchProject, setIssues, updateIssue, ...`

### 13.4 [`useWebSocket`](../web-ui/src/hooks/useWebSocket.ts)

Хук, що:
1. Викликає `connectSocket()` (singleton)
2. На `connect` — `joinProjectRoom(projectId)`
3. Підписується на 9 серверних подій з `if (data.project_id !== projectId) return`
4. На `architect:message` з `is_streaming==true` — ігнорується (TODO: реалізувати token-by-token)
5. На unmount — `socket.off(...)` для всіх + `disconnectSocket()`
6. Експортує `sendMessage(text)` що емітить `chat:message` + optimistic local message

### 13.5 [`socket.ts`](../web-ui/src/lib/socket.ts)

Singleton з `transports: ["websocket", "polling"]`, `reconnectionAttempts: 10`, `reconnectionDelay: 1000..10000`.

### 13.6 [`api.ts`](../web-ui/src/lib/api.ts)

Тонкий fetch-wrapper. Базовий URL з `process.env.NEXT_PUBLIC_API_URL`.
26 функцій: getProjects, getProject, createProject, deleteProject, getProviderModels, runProjectPreflight, syncProjectCicd, startProject, stopProject, approvePlan, approvePreview, deployProject, restartProjectPreview, getProjectPlan, updateProjectPlan, updateProjectLlmConfig, getChatHistory, getProjectLogs, getProjectSessions, getPreviewUrl, rollbackProject, updateDeployTarget, getDeployRuns, getIssues, syncIssues, auditProject, uploadFigmaFile, assignToCopilot, assignIssueToCopilot, reviewPR.

⚠️ Нотатка: деякі функції (`approvePreview`, `deployProject`, `restartProjectPreview`, `syncProjectCicd`, `rollbackProject`) кличуть ендпоінти, яких **немає** в `routes.py` (`/approve-preview`, `/deploy`, `/preview/restart`, `/sync-cicd`, `/rollback`) — це залишки від попередньої архітектури. Виклик впаде з 404, але UI це обробляє через try/catch у store.

### 13.7 [`types.ts`](../web-ui/src/lib/types.ts)

Повний набір TypeScript-типів, дзеркало Pydantic-моделей з backend.

---

## 14. Docker, Docker Compose, мережі

### 14.1 [`docker-compose.yml`](../docker-compose.yml) (base)

```yaml
services:
  orchestrator:
    build: docker/orchestrator/Dockerfile
    volumes:
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock          # ← preview engine
      - /var/automatron/workspaces:/var/automatron/workspaces
      - ./orchestrator/orchestrator:/app/orchestrator      # hot-reload (dev)
      - ./orchestrator/prompts:/app/prompts
    secrets:
      - openai_api_key
      - anthropic_api_key
      - google_api_key
    environment:
      SQLITE_DB_PATH=/app/data/automatron.db
      CHECKPOINT_DB_PATH=/app/data/checkpoints.db
      WORKSPACE_BASE_PATH=/var/automatron/workspaces
      GOLDEN_IMAGE=automatron/golden:latest
      DEBUG=true
    healthcheck: curl -f http://localhost:8000/health (30s/10s/3)

  web-ui:
    build:
      args:
        NEXT_PUBLIC_API_URL: ${API_URL:-http://localhost:8000}
        NEXT_PUBLIC_WS_URL:  ${WS_URL:-ws://localhost:8000}
    depends_on:
      orchestrator: {condition: service_healthy}

  nginx:
    image: nginx:alpine
    ports: ["80:80"]
    volumes: ./docker/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
    profiles: [production]    # ← активується тільки --profile production

secrets:
  openai_api_key:    {file: ./secrets/openai_api_key.txt}
  anthropic_api_key: {file: ./secrets/anthropic_api_key.txt}
  google_api_key:    {file: ./secrets/google_api_key.txt}
```

### 14.2 [`docker-compose.dev.yml`](../docker-compose.dev.yml) (overlay)

Прокидає порти:
```yaml
services:
  orchestrator:
    ports: ["8000:8000"]
  web-ui:
    ports: ["3000:3000"]
```

Запуск: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up`.

### 14.3 [`docker-compose.prod.yml`](../docker-compose.prod.yml) (overlay)

Підключає Traefik labels:
- `automatron-api` router: `Host(automatron.quitcode.com) && (PathPrefix(/api) || Path(/health) || PathPrefix(/socket.io))`, `priority=10`, port 8000
- `automatron-http` (HTTP→HTTPS redirect)
- `automatron` router: `Host(automatron.quitcode.com)`, `priority=1`, port 3000
- TLS: `tls.certresolver=le` (Let's Encrypt)
- Web-UI має `HOSTNAME=0.0.0.0` (для Next standalone)
- `nginx: profiles: [disabled]` — деактивує nginx у проді
- Network: external `proxy`

Запуск: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.

### 14.4 [`docker/orchestrator/Dockerfile`](../docker/orchestrator/Dockerfile)

```Dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN apt-get install ca-certificates curl git openssh-client
RUN pip install uv
COPY orchestrator/pyproject.toml orchestrator/README.md* ./
COPY orchestrator/orchestrator/ ./orchestrator/
COPY orchestrator/prompts/ ./prompts/
COPY orchestrator/scripts/ ./scripts/
RUN uv pip install --system -e ".[dev]"
RUN mkdir -p /app/data /var/automatron/workspaces
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 14.5 [`docker/web-ui/Dockerfile`](../docker/web-ui/Dockerfile) (multi-stage)

Builder: `node:22-alpine` → `npm install` → `npm run build` (Next standalone output).
Runner: `node:22-alpine` → копіює `.next/standalone`, `.next/static`, `public` → `EXPOSE 3000` → `CMD node server.js`.

### 14.6 [`docker/golden-image/Dockerfile`](../docker/golden-image/Dockerfile) (legacy)

`Ubuntu 24.04` + Node 22 + Python 3.12 + pnpm + `cline@2.7.0` + non-root `developer:1000`. Не використовується в активному GitHub-mode флоу, але залишився для legacy шляху.

### 14.7 [`docker/nginx/default.conf`](../docker/nginx/default.conf)

WebSocket upgrade map. Маршрутизація:
- `/api/` → `orchestrator:8000` (з 300s timeouts)
- `/health` → `orchestrator:8000`
- `/socket.io/` → `orchestrator:8000` з upgrade headers, `proxy_buffering off`, `read_timeout 86400s`
- `/` → `web-ui:3000`
- `/_next/webpack-hmr` → `web-ui:3000` з upgrade headers (dev HMR)

Security headers: X-Frame-Options SAMEORIGIN, X-Content-Type-Options nosniff, X-XSS-Protection, Referrer-Policy.

---

## 15. CI/CD самого Automatron

### 15.1 [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)

Тригер: `push` на `main` або manual `workflow_dispatch`.

Концурренсі: `automatron-production-deploy` group, `cancel-in-progress: true`.

Хардкоджені env:
- `DEPLOY_HOST=91.98.68.42`
- `DEPLOY_USER=root`
- `DEPLOY_PATH=/root/app/automatron-vmark`
- `REPO_SSH_URL=git@github.com:Quitcode-Dev/project-automatron-vmark.git`

Кроки:
1. Validate `AUTOMATRON_DEPLOY_KEY` secret
2. Configure SSH (`~/.ssh/id_ed25519`, `ssh-keyscan` для known_hosts)
3. SSH heredoc на VPS:
   - Перевіряє `/root/.ssh/automatron_deploy` server-side key
   - `git clone` (якщо немає `.git`, з backup `.env`) або `fetch+reset --hard origin/main`
   - `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --remove-orphans`
4. Опціональний webhook notify: `AUTOMATRON_WEBHOOK_URL` секрет, POST `{status: $JOB_STATUS}` JSON

### 15.2 GitHub Secrets (для самого Automatron repo)

| Secret | Призначення |
|---|---|
| `AUTOMATRON_DEPLOY_KEY` | Private SSH key, який кладеться на runner у `~/.ssh/id_ed25519` для зв'язку з VPS |
| `AUTOMATRON_WEBHOOK_URL` | (optional) Куди постити статус деплою |

На VPS повинен бути `/root/.ssh/automatron_deploy` ключ для git-clone з GitHub.

---

## 16. Спостережуваність та трасування

### 16.1 [`observability.py`](../orchestrator/orchestrator/observability.py)

Один публічний хелпер `trace_event(project_id, actor, event_type, payload, session_id?, stage?)` — записує в `trace_events` таблицю через `save_trace_event` з UUID id.

Перед записом — рекурсивний `_trim` (limit=12000) обрізає string payload-и. Виклики НЕ кидають exceptions (catch-and-log).

### 16.2 Що трасується

| Actor | Event type | Payload |
|---|---|---|
| `operator` | `chat.message.received` | `{text}` |
| `architect` | `architect.run.started` | `{owner, repo}` |
| `architect` | `architect.run.completed` | `{epics: int}` |
| `orchestrator` | `apply_plan.started/completed` | `{issues_created, total_tasks}` |
| `reviewer` | `pr.review.completed` | `{issue_number, pr_number, passed}` |
| `llm` | `llm.call.started` | `{model, temperature, max_tokens, messages, message_count, prompt_name}` |
| `llm` | `llm.call.completed` | `{model, response, usage}` |
| `llm` | `llm.call.failed` | `{model, error}` |
| `llm` | `llm.stream.started/completed/failed` | те саме + `chunk_count` |

### 16.3 Activity logs

Окремо від trace — це user-facing лог через `_log(task_text, output, status)` в `GitHubOrchestrator`:
- Записує в `activity_logs` таблицю
- Емітує `builder:log` Socket.IO подію в `project:{id}` кімнату
- `_log_seq` лічильник інкрементиться

Status-словник на UI: `INFO | RUNNING | SUCCESS | AMBIGUITY | BLOCKER | ERROR`.

### 16.4 Логування

`logging.getLogger(__name__)` скрізь. Default — без custom config (uvicorn handle). litellm `set_verbose = False`.

---

## 17. Безпека та секрети

### 17.1 Поточна модель

- **Без аутентифікації** — UI/API доступні всім, хто має URL. Очікується VPN/private network.
- CORS `allow_origins=["*"]` — прийнятно лише в приватному середовищі.
- Webhook signature: HMAC-SHA256 через `GITHUB_WEBHOOK_SECRET` — **opt-in**. Якщо не задано — запити з GitHub приймаються без перевірки. Це небезпечно при відкритому `AUTOMATRON_PUBLIC_URL`.

### 17.2 Secrets

#### Docker Secrets (база)
- `secrets/openai_api_key.txt`
- `secrets/anthropic_api_key.txt`
- `secrets/google_api_key.txt`

Монтуються в `/run/secrets/{name}` всередині контейнера. Формально — Settings читає з env, тому в docker-compose має бути або env-bind, або orchestrator повинен читати з `/run/secrets/...`. Поточний код **використовує тільки env-змінні** — Docker secrets file-based є рудиментом. Треба завантажувати їх в env через entrypoint якщо хочете їх реально використати.

#### GitHub PAT
Зберігається в `GITHUB_TOKEN` env. Використовується в Authorization headers + clone URL: `https://x-access-token:{token}@github.com/...`.

#### LLM keys
Передаються litellm через відповідні env (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`).

#### Deploy target SSH keys
Зберігаються в `projects.deploy_target_json` — clear text у БД (write-only з UI). Шифруються через nacl.SealedBox при upserting в GitHub Environment Secrets.

### 17.3 .gitignored

`secrets/`, `.env`, `.env.local`, `.env.production`, `data/`, `*.db`, `*.sqlite`, `node_modules/`, `__pycache__/`, etc.

### 17.4 Що варто посилити

- Додати JWT/session-based auth → middleware
- `GITHUB_WEBHOOK_SECRET` зробити обов'язковим коли `AUTOMATRON_PUBLIC_URL` задано
- CORS обмежити списком хостів
- Rate limiting на API
- Шифрування `deploy_target_json` at-rest

---

## 18. Сумісність з застарілими модулями (рудименти)

### 18.1 LangGraph

`pyproject.toml` тягне `langgraph>=1.0.9` та `langgraph-checkpoint-sqlite>=3.0.0`, але **жоден активний модуль не імпортує langgraph**. Можна видалити.

### 18.2 Cline CLI

Dockerfile `golden-image` встановлює `cline@2.7.0`. Не запускається з активного коду (`builder` ноду немає). Залишилася лише як base image для legacy сценаріїв.

### 18.3 PLAN.md parser/writer

Модуль [`plan_parser/`](../orchestrator/orchestrator/plan_parser/) — парсер frontmatter+чекбоксів та writer для `mark_task_complete`. У GitHub-mode не викликається — план зберігається як готовий markdown без структурованого парсингу.

### 18.4 Execution contract

[`execution_contract.py`](../orchestrator/orchestrator/execution_contract.py) — будує machine-readable JSON-контракт із PLAN.md+stack_config. У GitHub-mode його роль виконує `issue_plan_json`. Не викликається з активних шляхів.

### 18.5 RepositoryManager + DeploymentManager

Активно тільки `register_webhook` (хоча в коді є дублікат у `GitHubClient.register_webhook`) та `actions_manager.ensure_environment` для preflight `deploy`. Усі git-операції (`initialize_workspace_repository`, `commit_workspace_changes`, `merge_branch`) і SSH-deploy не викликаються.

### 18.6 Tests

[`tests/test_graph.py`, `test_graph_checkpoint.py`, `test_runner.py`, `test_validator_node.py`, `test_architect.py`](../orchestrator/tests/) — посилаються на `orchestrator.graph` модуль, якого немає. Імпорти зламаються при запуску. Активні тести: `test_routes_contract.py`, `test_preflight.py`, `test_llm_catalog.py`, `test_llm_configuration.py`, `test_repository_manager.py`, `test_github_actions_manager.py`, `test_deployment_manager.py`, `test_plan_parser.py`, `test_execution_contract.py`.

### 18.7 Старі API-маршрути на UI

[`api.ts`](../web-ui/src/lib/api.ts) має функції `approvePreview`, `deployProject`, `restartProjectPreview`, `syncProjectCicd`, `rollbackProject` — звертаються до неіснуючих ендпоінтів. Відповідні кнопки на UI зроблять 404, але через `try/catch` помилка тільки відобразиться в червоному errorBar.

---

## 19. Тести

[`orchestrator/tests/`](../orchestrator/tests/), pytest-asyncio mode auto, testpaths=["tests"].

| Файл | Покриття |
|---|---|
| `test_routes_contract.py` | REST API contract |
| `test_preflight.py` | PreflightService logic |
| `test_llm_catalog.py` | Кешування каталогу + filter rules |
| `test_llm_configuration.py` | normalize_llm_config |
| `test_repository_manager.py` | git+repo provisioning (з mocked subprocess) |
| `test_github_actions_manager.py` | nacl encryption, secrets, workflow render |
| `test_deployment_manager.py` | SSH command building |
| `test_plan_parser.py` | PLAN.md regex |
| `test_execution_contract.py` | extract_json_blocks, build_execution_contract |
| ⚠️ `test_graph*.py, test_runner.py, test_validator_node.py, test_architect.py` | Зламані імпорти (`orchestrator.graph` не існує) |
| `test_docker_engine.py` | (Docker SDK тести) |
| `test_validation_workspace.py` | runtime spec resolver |

Запуск:
```bash
make test          # cd orchestrator && python -m pytest tests/ -v
make test-cov      # + coverage
```

Перед запуском треба видалити/виправити legacy-тести, інакше pytest fail на collect.

---

## 20. Покрокова інструкція з відбудови з нуля

### 20.1 Що потрібно мати

| Інструмент | Версія |
|---|---|
| Linux VPS | Ubuntu 22.04+/Debian 12+ |
| Docker | 24+ з Docker Compose v2 |
| Python | 3.12 (для local dev) |
| Node.js | 22 LTS (для local dev) |
| Git | 2.34+ |
| pnpm | 9+ (опціонально) |
| GitHub PAT | scopes: `repo, admin:repo_hook, workflow, admin:org` (якщо org-mode) |
| LLM API keys | OpenAI/Anthropic/Google (≥ один) |
| Доменне ім'я + DNS | для production HTTPS (за бажанням) |

### 20.2 Крок 1: Каркас репозиторію

```bash
mkdir project-automatron && cd project-automatron
git init
mkdir -p docker/{golden-image,orchestrator,web-ui,nginx} \
         orchestrator/{orchestrator/{api,deployment,github,github_actions,llm,models,plan_parser,repository,validation},prompts,scripts,tests} \
         web-ui/src/{app/project/[id],app/projects,components/{layout,project,ui},hooks,lib,stores,public} \
         data secrets workspaces docs .github/workflows
```

### 20.3 Крок 2: Створити каркасні конфіги

#### `.gitignore` (з повним списком — див. розділ 4)
#### `.env.example` — копія з [`.env.example`](../.env.example)
#### `Makefile` — копія з [`Makefile`](../Makefile)
#### `pyproject.toml` для `orchestrator/` — копія з [`pyproject.toml`](../orchestrator/pyproject.toml)
#### `package.json` для `web-ui/` — копія з [`web-ui/package.json`](../web-ui/package.json)

### 20.4 Крок 3: Backend код (по модулях)

Виконуйте у порядку залежностей:

1. [`orchestrator/orchestrator/config.py`](../orchestrator/orchestrator/config.py) — Pydantic Settings
2. [`orchestrator/orchestrator/observability.py`](../orchestrator/orchestrator/observability.py) — trace_event
3. [`orchestrator/orchestrator/models/project.py`](../orchestrator/orchestrator/models/project.py) — SQLite модель + CRUD
4. [`orchestrator/orchestrator/models/session.py`](../orchestrator/orchestrator/models/session.py) — Sessions CRUD
5. [`orchestrator/orchestrator/llm/configuration.py`](../orchestrator/orchestrator/llm/configuration.py) — provider/model normalization
6. [`orchestrator/orchestrator/llm/catalog.py`](../orchestrator/orchestrator/llm/catalog.py) — Provider model catalog з кешем
7. [`orchestrator/orchestrator/llm/provider.py`](../orchestrator/orchestrator/llm/provider.py) — call_llm, call_llm_streaming
8. [`orchestrator/orchestrator/github/issues.py`](../orchestrator/orchestrator/github/issues.py) — GitHubClient
9. [`orchestrator/orchestrator/github_actions/manager.py`](../orchestrator/orchestrator/github_actions/manager.py) — GitHubActionsManager
10. [`orchestrator/orchestrator/repository/manager.py`](../orchestrator/orchestrator/repository/manager.py) — RepositoryManager
11. [`orchestrator/orchestrator/deployment/manager.py`](../orchestrator/orchestrator/deployment/manager.py) — DeploymentManager (legacy)
12. [`orchestrator/orchestrator/validation/preflight.py`](../orchestrator/orchestrator/validation/preflight.py) — PreflightService
13. [`orchestrator/orchestrator/validation/runtime.py`](../orchestrator/orchestrator/validation/runtime.py) — PreviewRuntimeSpec (legacy, але імпортовано)
14. [`orchestrator/orchestrator/preview.py`](../orchestrator/orchestrator/preview.py) — Local preview engine
15. [`orchestrator/orchestrator/orchestrator.py`](../orchestrator/orchestrator/orchestrator.py) — **GitHubOrchestrator (ядро)**
16. [`orchestrator/orchestrator/api/socket_server.py`](../orchestrator/orchestrator/api/socket_server.py) — sio singleton
17. [`orchestrator/orchestrator/api/websocket.py`](../orchestrator/orchestrator/api/websocket.py) — handlers + emit helpers
18. [`orchestrator/orchestrator/api/webhook_github.py`](../orchestrator/orchestrator/api/webhook_github.py) — PR webhook
19. [`orchestrator/orchestrator/api/routes.py`](../orchestrator/orchestrator/api/routes.py) — REST endpoints
20. [`orchestrator/orchestrator/main.py`](../orchestrator/orchestrator/main.py) — FastAPI factory + ASGIApp wrapper

#### Залежності модулів (граф імпортів):

```
main.py → routes.py, webhook_github.py, websocket.py, socket_server.py, models/project.py, config.py
routes.py → orchestrator.py, validation/preflight.py, repository/manager.py, models/{project,session}.py, llm/{catalog,configuration}.py, config.py
orchestrator.py → api/websocket.py, github/issues.py, llm/{configuration,provider}.py, models/project.py, observability.py, config.py, preview.py
github/issues.py → config.py
preview.py → config.py
preflight.py → config.py, llm/{catalog,configuration}.py, repository/manager.py
repository/manager.py → config.py, github_actions/manager.py
github_actions/manager.py → config.py
llm/provider.py → config.py, observability.py
llm/catalog.py → config.py, llm/configuration.py
llm/configuration.py → config.py
models/project.py → config.py, llm/configuration.py
observability.py → models/project.py
webhook_github.py → orchestrator.py, models/project.py, config.py, api/websocket.py
websocket.py → api/socket_server.py, models/project.py, observability.py
```

### 20.5 Крок 4: Промпти

Створіть [`orchestrator/prompts/architect_github_v1.txt`](../orchestrator/prompts/architect_github_v1.txt). Це **критичний файл** — без нього `analyze_and_plan` не працюватиме.

Опціонально: `architect_v1.txt`, `builder_v1.txt`, `reviewer_v1.txt` — для legacy режимів.

### 20.6 Крок 5: Scripts

Скопіюйте [`orchestrator/scripts/init-*.sh`](../orchestrator/scripts/) — використовуються legacy шляхом scaffold (можна пропустити для GitHub-mode).

### 20.7 Крок 6: Frontend

1. [`web-ui/next.config.js`](../web-ui/next.config.js) — `output: standalone`, env API/WS
2. [`web-ui/tsconfig.json`](../web-ui/tsconfig.json), [`tailwind.config.js`](../web-ui/tailwind.config.js), [`postcss.config.js`](../web-ui/postcss.config.js)
3. [`src/lib/types.ts`](../web-ui/src/lib/types.ts) — TS-типи
4. [`src/lib/socket.ts`](../web-ui/src/lib/socket.ts) — socket.io singleton
5. [`src/lib/api.ts`](../web-ui/src/lib/api.ts) — fetch-обгортки
6. [`src/lib/llmOptions.ts`](../web-ui/src/lib/llmOptions.ts), `utils.ts`
7. [`src/stores/projectStore.ts`](../web-ui/src/stores/projectStore.ts) — Zustand
8. [`src/hooks/useWebSocket.ts`](../web-ui/src/hooks/useWebSocket.ts)
9. UI компоненти: `components/layout/{AppLayout,Header,Sidebar}.tsx`, `components/ui/{AlertPanel,LogStream,ProgressBar,StatusBadge}.tsx`, `components/project/*.tsx`
10. Сторінки: `app/layout.tsx`, `app/globals.css`, `app/page.tsx`, `app/project/[id]/page.tsx`, `app/projects/page.tsx`

### 20.8 Крок 7: Docker файли

1. [`docker/orchestrator/Dockerfile`](../docker/orchestrator/Dockerfile)
2. [`docker/web-ui/Dockerfile`](../docker/web-ui/Dockerfile)
3. [`docker/nginx/default.conf`](../docker/nginx/default.conf)
4. [`docker-compose.yml`](../docker-compose.yml), [`docker-compose.dev.yml`](../docker-compose.dev.yml), [`docker-compose.prod.yml`](../docker-compose.prod.yml)
5. (опціонально) [`docker/golden-image/Dockerfile`](../docker/golden-image/Dockerfile)

### 20.9 Крок 8: CI/CD

[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) — підставте свої `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`, `REPO_SSH_URL`. Додайте GitHub secret `AUTOMATRON_DEPLOY_KEY`.

### 20.10 Крок 9: Local Dev

```bash
# 1. Configure
cp .env.example .env
nano .env                                    # GITHUB_TOKEN, GITHUB_OWNER, *_API_KEY

# 2. Backend
cd orchestrator
python -m venv .venv
source .venv/bin/activate                    # або .venv\Scripts\activate на Windows
pip install -e ".[dev]"
mkdir -p ../data
uvicorn orchestrator.main:app --reload --host 0.0.0.0 --port 8000

# 3. Frontend (інший термінал)
cd web-ui
pnpm install                                 # або npm install
pnpm dev                                     # http://localhost:3000

# 4. Створити проект
# Браузер → http://localhost:3000 → New Project
# - Name: Test
# - Repo URL: https://github.com/owner/repo (потрібен реальний з README.md)
# - LLM: openai / gpt-4o (наприклад)
# Після створення натиснути Start Build
```

### 20.11 Крок 10: Production deploy

```bash
# На VPS:
git clone git@github.com:Quitcode-Dev/your-fork.git /root/app/automatron
cd /root/app/automatron

# Configure
cp .env.example .env
nano .env

# Build & start
make secrets                                 # створює placeholder secrets
nano secrets/openai_api_key.txt              # підставити реальний
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# Перевірити
curl https://automatron.your-domain.com/health
```

Traefik мусить вже бути запущений на VPS з мережею `proxy` (external).

### 20.12 Крок 11: Налаштування webhook

Якщо `AUTOMATRON_PUBLIC_URL` задано — webhook реєструється автоматично при старті проекту. Інакше додайте вручну на GitHub:
- Payload URL: `https://your-automatron.com/api/webhooks/github`
- Content type: `application/json`
- Secret: значення `GITHUB_WEBHOOK_SECRET`
- Events: `Pull requests` only

---

## 21. Чек-лист готовності та смоук-тести

### 21.1 Конфігурація готова
- [ ] `.env` має валідні `GITHUB_TOKEN`, `GITHUB_OWNER`
- [ ] Принаймні один з `OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_API_KEY`
- [ ] (prod) `AUTOMATRON_PUBLIC_URL` задано
- [ ] (prod) `GITHUB_WEBHOOK_SECRET` задано
- [ ] (prod) DNS A-запис вказує на VPS
- [ ] (prod) Traefik або Nginx запущено

### 21.2 Backend health
```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 21.3 LLM каталог
```bash
curl http://localhost:8000/api/llm/providers/openai/models
# {"provider":"openai","configured":true,"models":[...],...}
```

### 21.4 SQLite ініціалізовано
```bash
sqlite3 data/automatron.db ".tables"
# activity_logs chat_messages deploy_runs github_issues projects sessions task_logs trace_events
```

### 21.5 Створення проекту через CLI
```bash
curl -X POST http://localhost:8000/api/projects \
  -H "Content-Type: application/json" \
  -d '{"name":"Test","repo_url":"https://github.com/owner/repo","source":"manual"}'
# {"id":"...","name":"Test",...}
```

### 21.6 Preflight
```bash
curl -X POST http://localhost:8000/api/projects/{id}/preflight \
  -H "Content-Type: application/json" -d '{"phase":"start"}'
# {"phase":"start","ok":true,"blocking":false,"checks":[...]}
```

### 21.7 Webhook signature тест
```bash
PAYLOAD='{"action":"opened","pull_request":{"number":1,"body":"closes #1"},"repository":{"name":"r","owner":{"login":"o"}}}'
SIG="sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | cut -d' ' -f2)"
curl -X POST http://localhost:8000/api/webhooks/github \
  -H "X-Hub-Signature-256: $SIG" \
  -H "X-GitHub-Event: pull_request" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD"
```

### 21.8 Frontend health
- [ ] `http://localhost:3000` відкриває dashboard
- [ ] Кнопка "New Project" відкриває діалог
- [ ] DevTools Network → видно `/api/llm/providers/openai/models` 200
- [ ] DevTools Network → є WebSocket з'єднання `ws://localhost:8000/socket.io/...`

### 21.9 E2E тест

1. Створити публічний GitHub репо з `README.md` що описує MVP (наприклад "Build a TODO app with Next.js+SQLite")
2. У UI створити проект з цим repo_url
3. Start Build → у Activity з'являються логи
4. Через 30-60s з'являється план у Chat tab + сторінка переходить у `awaiting_plan_approval`
5. Approve Plan → у Issues tab з'являються issues
6. Click "Open Issues on GitHub" → перевірити що milestones+issues+labels створено
7. Assign Copilot → issues отримують assignee
8. (Чекати) Copilot створює PRs → у Activity з'являється PR review log
9. Issues → "Review" кнопка → запит до /review-pr
10. Sync Issues → статуси оновлюються
11. Коли всі merged → preview автозапускається на порту 7000+
12. Preview tab → iframe з робочим додатком

---

## 22. Відомі проблеми та технічний борг

### 22.1 Документація розходиться з реалізацією

[`README.md`](../README.md), [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md), [`IMPLEMENTATION_PLAN.md`](../IMPLEMENTATION_PLAN.md) описують LangGraph 8-нодну стейт-машину з Cline CLI builder. Реалізація — лінійний `GitHubOrchestrator` без LangGraph runtime, з Copilot як builder. **Цей документ (`PROJECT_BLUEPRINT.md`) — actual source of truth**.

### 22.2 Мертвий код / залежності

- `langgraph`, `langgraph-checkpoint-sqlite` у `pyproject.toml` — не імпортуються
- `docker` Python SDK — не імпортується (preview використовує `subprocess`)
- Тести `test_graph*.py, test_runner.py, test_validator_node.py, test_architect.py` мають broken imports
- `plan_parser/`, `execution_contract.py`, `prompts/architect_v1.txt`, `builder_v1.txt`, `reviewer_v1.txt` — використовувались у LangGraph-режимі
- Більшість методів `RepositoryManager` та `DeploymentManager` не викликаються
- API-функції `approvePreview`, `deployProject`, `restartProjectPreview`, `syncProjectCicd`, `rollbackProject` у [`api.ts`](../web-ui/src/lib/api.ts) — звертаються до неіснуючих ендпоінтів

### 22.3 Великі моноліти

- [`orchestrator.py`](../orchestrator/orchestrator/orchestrator.py) — 1035 LOC, 5 неспоріднених стейджів. Кандидат на розбиття: `architect_runner.py`, `issue_creator.py`, `pr_reviewer.py`, `auditor.py`, `preview_starter.py`
- [`models/project.py`](../orchestrator/orchestrator/models/project.py) — 1075 LOC, `projects` таблиця з 50+ колонками, 17 update-функцій. Бажано — окремі модулі для активних доменів (preview, deploy, github_issues, trace)
- [`web-ui/src/app/project/[id]/page.tsx`](../web-ui/src/app/project/[id]/page.tsx) — 1150 LOC. Розбити на: `<ProjectHeader>`, `<DeliveryFlowCard>`, `<LlmRolesPanel>`, `<RepoPanel>`, `<DeployTargetForm>`, `<TabsNavigation>`

### 22.4 Безпека

- `CORS *` ([main.py:42](../orchestrator/orchestrator/main.py#L42))
- Webhook signature optional коли немає secret
- Hardcoded VPS IP `91.98.68.42` та repo URL у [deploy.yml](../.github/workflows/deploy.yml)
- Default моделі `gpt-5.3-codex` ([config.py:35](../orchestrator/orchestrator/config.py#L35)) — модель не існує, треба замінити на актуальну
- `deploy_target_json` — clear text у БД
- Без auth/RBAC

### 22.5 UX/функціональні діри

- `chat:message` handler ([websocket.py:64](../orchestrator/orchestrator/api/websocket.py#L64)) — заглушка-echo
- Streaming chunks ігноруються в UI ([useWebSocket.ts:57](../web-ui/src/hooks/useWebSocket.ts#L57)) — користувач бачить план тільки після завершення стрімінгу
- `audit_codebase` створює дублікати issues (немає dedup logic)
- На Windows `workspace_base_path` ігнорується якщо POSIX-стиль — це silent fallback на `cwd/workspaces`

### 22.6 Незакриті пункти з [E2E_REMEDIATION_BACKLOG.md](../docs/E2E_REMEDIATION_BACKLOG.md)

- B-001 Preflight enforcement (частково)
- B-003 Resume semantics (lock-down проти повторної генерації плану)
- B-004 Hard validation gates на deploy artifacts
- та інші

---

## 23. Глосарій

| Термін | Значення |
|---|---|
| **Architect** | LLM-роль для генерації плану з README/PRD |
| **Builder** | LLM-роль для написання коду. У GitHub-mode — це GitHub Copilot Coding Agent |
| **Reviewer** | LLM-роль для оцінки PR diff проти acceptance criteria |
| **Issue plan** | JSON структура `{epics: [{stories: [{tasks: []}]}]}` що Architect генерує |
| **Activity log** | Послідовний user-facing журнал для UI (через `_log` метод) |
| **Trace event** | Структурована low-level телеметрія (LLM calls, stage transitions) |
| **Stage** | Фаза життєвого циклу проекту (`intake` → ... → `deployed`) |
| **Status** | Грубіший статус для UI (`pending|building|deployed|...`) |
| **Approval** | Human-in-the-loop gate (план, preview) |
| **Webhook** | GitHub PR-event endpoint у Automatron |
| **Preview** | Local Docker container що показує робочий додаток на http://localhost:7XXX |
| **Golden Image** | Legacy Ubuntu 24.04 image з усіма runtimes — не використовується активно |
| **Cline CLI** | Legacy headless coding agent — замінений Copilot |
| **GitHub-native** | Поточний режим роботи: Issues+Copilot+PRs замість локального builder |
| **litellm** | Адаптер до OpenAI/Anthropic/Google єдиного API |
| **Per-project room** | Socket.IO кімната `project:{uuid}` для ізоляції realtime-подій |

---

## Додаток A: Швидкий перелік усіх ендпоінтів та подій

### REST (від [routes.py](../orchestrator/orchestrator/api/routes.py))

```
GET    /health
GET    /api/projects
POST   /api/projects
GET    /api/projects/{id}
DELETE /api/projects/{id}
POST   /api/projects/{id}/start
POST   /api/projects/{id}/stop
POST   /api/projects/{id}/approve-plan
POST   /api/projects/{id}/approve
POST   /api/projects/{id}/preflight
GET    /api/projects/{id}/plan
PUT    /api/projects/{id}/plan
PUT    /api/projects/{id}/llm-config
GET    /api/projects/{id}/issues
POST   /api/projects/{id}/sync-issues
POST   /api/projects/{id}/audit
POST   /api/projects/{id}/assign-copilot
POST   /api/projects/{id}/issues/{issue_number}/assign-copilot
POST   /api/projects/{id}/review-pr
POST   /api/projects/{id}/figma-file (multipart)
GET    /api/projects/{id}/logs
GET    /api/projects/{id}/sessions
GET    /api/projects/{id}/chat-history
GET    /api/projects/{id}/preview-url
GET    /api/projects/{id}/deploy-runs
GET    /api/projects/{id}/trace
PUT    /api/projects/{id}/deploy-target
GET    /api/llm/providers
GET    /api/llm/providers/{provider}/models
POST   /api/webhooks/github
```

### Socket.IO events

**Client → Server:** `connect, disconnect, join, leave, chat:message`

**Server → Client:** `architect:message, builder:log, status:update, human:required, run:error, plan:updated, issues:updated, pr:review_ready`

---

## Додаток B: Команди обслуговування

### Бекап даних
```bash
# SQLite WAL-safe copy
cp data/automatron.db data/automatron.db.bak.$(date +%Y%m%d_%H%M)
```

### Скинути всі preview-контейнери
```bash
docker ps --format '{{.Names}}' | grep '^preview-' | xargs -r docker rm -f
```

### Очистити robust workspaces
```bash
rm -rf orchestrator/workspaces/*
```

### Скинути базу (НЕЗВОРОТНО)
```bash
rm data/*.db
docker compose restart orchestrator
```

### Подивитися trace для проекту
```bash
sqlite3 data/automatron.db "SELECT actor, event_type, created_at FROM trace_events WHERE project_id='UUID' ORDER BY created_at DESC LIMIT 50"
```

### Перевірити, що webhook реєстровано
```bash
curl -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/repos/{owner}/{repo}/hooks
```

---

*Кінець документа. За змін у кодовій базі — оновлюйте відповідні розділи й нумеровані рядкові посилання.*
