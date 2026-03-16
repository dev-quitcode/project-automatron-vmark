# E2E Remediation Backlog

## Purpose

This backlog converts the findings from
`docs/E2E_TEST_RUN_POSTMORTEM.md` into an execution plan.

The goal is not just to fix bugs, but to eliminate manual rescue work in the
next full test run.

## How to Use This Backlog

- `Critical Now`: must be done before the next serious autonomous E2E run
- `Before Next E2E`: should be done to make the next run reliable and
  repeatable
- `Before Production`: required before treating Automatron as a production
  delivery system

Suggested owner tags:

- `Platform`: orchestrator, Docker, graph runtime, repository/deploy pipeline
- `Prompting`: architect/builder/reviewer prompts and policies
- `Validation`: tests, quality gates, artifact checks, CI assertions
- `UI/API`: operator UX, DTOs, run visibility, contract stability

## Critical Now

### B-001 Preflight checks before `start`

- Area: `Platform`
- Problem:
  - runs can fail late because prerequisites are missing or invalid
- Work:
  - add a preflight step before project start
  - verify:
    - golden image exists
    - Docker daemon is reachable
    - workspace path is valid for host OS
    - GitHub token is configured
    - GitHub owner is configured
    - required LLM key exists for selected provider(s)
- Done when:
  - project start fails early with a precise structured error if any
    prerequisite is missing
  - happy-path preflight is covered by automated tests

### B-002 Make graph compile and checkpoint restore a tested invariant

- Area: `Platform`
- Problem:
  - graph/checkpoint issues previously blocked the run before architect/builder
    execution
- Work:
  - add an integration test that compiles the graph and performs:
    - initial invoke
    - checkpoint persistence
    - state history read
    - resume invoke
- Done when:
  - regression test fails on bad checkpointer wiring
  - no manual verification is needed to trust resume mechanics

### B-003 Lock down resume semantics

- Area: `Platform`
- Problem:
  - `start` previously regenerated planning instead of resuming
- Work:
  - add state transition tests for:
    - `pending -> planning`
    - `awaiting_plan_approval -> resume`
    - `building -> resume`
    - `paused/error -> resume without plan reset`
  - explicitly disallow starting a new planning cycle when a valid plan/checkpoint
    already exists
- Done when:
  - approved plans are stable across pause/error/start cycles
  - no plan regeneration happens unless explicitly requested

### B-004 Turn required deploy artifacts into hard validation gates

- Area: `Validation`
- Problem:
  - the generated repo became feature-complete before it became deployable
- Work:
  - enforce required artifacts before preview-ready or release-ready state:
    - `Dockerfile`
    - `.env.example`
    - `deploy/docker-compose.yml`
    - `DEPLOY.md`
    - `.github/workflows/ci.yml`
    - `.github/workflows/deploy.yml`
  - validate existence and minimal content shape, not only file presence
- Done when:
  - missing or malformed deploy artifacts block progression
  - reviewer/build flow cannot mark the project ready without them

### B-005 Strengthen reviewer from log-parser to artifact validator

- Area: `Validation`
- Problem:
  - reviewer was too permissive and accepted incomplete output
- Work:
  - keep current Next.js metadata/health checks
  - add stack-specific validator hooks
  - for `nextjs-prisma-sqlite-tailwind`, validate:
    - `/api/health` exists
    - metadata is not default scaffold text
    - preview command is runnable
    - Prisma client can import
    - build passes
- Done when:
  - reviewer can fail a builder task even if logs look clean but artifacts are
    invalid

### B-006 Standardize generated app health contract

- Area: `Platform`
- Problem:
  - generated apps did not consistently expose a health endpoint
- Work:
  - define one standard contract for generated web apps:
    - route path default: `/api/health`
    - JSON response with `status`, `service`, `timestamp`
    - optional dependency probe, such as DB check
  - bake this into scaffold defaults, builder contract, reviewer checks, and
    deploy defaults
- Done when:
  - every generated web app includes a valid health route without manual edits

### B-007 Eliminate stale preview process behavior

- Area: `Platform`
- Problem:
  - preview process could continue serving stale state after file changes
- Work:
  - make preview start/restart deterministic
  - track preview PID and restart reason
  - optionally clear framework caches before restart for known stacks
  - add explicit preview restart API/action
- Done when:
  - preview always reflects the latest workspace contents after a restart
  - operators do not need to restart containers manually for validation

### B-008 Add GitHub + deploy target preflight validation

- Area: `Platform`
- Problem:
  - GitHub repo/actions/environment/secrets readiness and deploy target
    correctness were proven only in live debugging
- Work:
  - add preflight validation for:
    - repo creation permission
    - environment creation permission
    - environment secret write permission
    - deploy target shape
    - auth mode validity
    - health URL defaults
- Done when:
  - deploy configuration can be validated before first production push

### B-025 Fix run-state drift between trace, project summary, and task-status

- Area: `Platform`
- Problem:
  - the clean autonomous run progressed from planning into builder/validator
    retries, but the project DTO still reported:
    - `status=planning`
    - `project_stage=awaiting_plan_approval`
    - `active_task_id=task-001...`
  - `task-status` also remained stuck on task 1 while trace showed the run had
    already advanced to task 2
- Work:
  - make project summary fields derive from the same canonical execution state as
    trace/routing
  - update persisted state immediately after:
    - task completion
    - validator completion
    - reviewer decision
    - task selection
    - architect delta / resume
  - add regression tests that compare:
    - trace latest event
    - `GET /projects/{id}`
    - `GET /projects/{id}/task-status`
- Done when:
  - operator-facing state always reflects the current live task/stage
  - trace, task-status, and project summary cannot disagree after a run step

### B-026 Fix shell escaping in deterministic validation commands

- Area: `Validation`
- Problem:
  - the Prisma smoke validation command failed even after builder produced a
    working Prisma setup because the command string contained
    `client.$disconnect()`
  - when executed through the shell, `$disconnect` was expanded away, producing
    invalid JavaScript like `client.()`
- Work:
  - stop passing raw JS snippets with `$` through shell-sensitive string paths
  - run deterministic checks using one of:
    - argument arrays without shell interpolation
    - checked-in helper scripts
    - temporary files executed by node/python directly
  - add regression tests for commands containing `$`, quotes, and nested JSON
- Done when:
  - validator command execution is shell-safe and deterministic
  - Prisma smoke checks fail only on real Prisma issues, not quoting bugs

### B-027 Align task granularity with validation gates

- Area: `Validation`
- Problem:
  - task 1 (`Verify and adapt scaffold`) was initially marked blocked because
    validator demanded artifacts that belong to later tasks:
    - `Dockerfile`
    - `app/api/health/route.ts`
    - `prisma/schema.prisma`
  - this forces builder to solve future phases too early and distorts retry
    behavior
- Work:
  - split validators into:
    - task-local gates
    - phase gates
    - release/deploy gates
  - enforce deploy artifacts only at the correct phase boundary
  - ensure task contracts declare the exact artifacts expected for that task
- Done when:
  - scaffold/setup tasks are validated only against their own contract
  - release artifacts are enforced before preview/release readiness, not during
    unrelated early tasks

### B-028 Make Prisma validation and contract version-aware

- Area: `Validation`
- Problem:
  - current Next.js + Prisma 7 flow uses `@prisma/adapter-libsql` and
    `prisma.config.ts`, but validation still assumes an older generic
    `new PrismaClient()` smoke pattern
  - builder produced a plausible Prisma 7 setup, yet validator/reviewer still
    treated it as broken because the smoke check did not match the selected
    integration style
- Work:
  - add explicit Prisma-version-aware validation strategies
  - for Prisma 7 + libsql adapter:
    - validate `prisma.config.ts`
    - validate adapter dependency presence
    - run a stack-aware smoke check using the actual app Prisma wrapper
  - stop using a one-size-fits-all Prisma command across all stacks/versions
- Done when:
  - Prisma validation matches the selected stack contract and Prisma major
    version
  - valid adapter-based setups pass without manual intervention

### B-029 Improve session/run observability schema

- Area: `UI/API`
- Problem:
  - `sessions` currently stores only:
    - `id`
    - `project_id`
    - `thread_id`
    - `phase`
    - `started_at`
    - `ended_at`
  - this is too thin for diagnosing live autonomous runs and does not capture
    current task, terminal status, retry count, or failure summary
- Work:
  - extend session/run persistence with:
    - run status
    - active task id
    - last event type
    - last validation gate result
    - last failure summary
  - expose these fields in API/UI timeline views
- Done when:
  - operators can understand a stuck/failed run without querying trace and DB
    separately
  - session records are useful as a first-line debugging surface

### B-030 Fix plan artifact serialization/rendering in the operator UI

- Area: `UI/API`
- Problem:
  - generated planning artifacts are reaching the operator UI in a partially
    object-shaped form, and the plan renderer shows broken lines like:
    - `,[object Object],: Confirm pre-scaffolded app is usable...`
  - this makes the plan-approval gate unreliable because the operator cannot
    trust the visual rendering of task title, description, and context
  - the defect may sit in one or more boundaries:
    - architect artifact parsing
    - `PLAN.md` normalization
    - API DTO serialization
    - frontend plan parsing/rendering
- Work:
  - trace the full artifact path:
    - raw architect response
    - parsed `PLAN.md`
    - persisted `plan_md`
    - project API response
    - frontend rendered task list
  - ensure operator-facing plan rendering uses a deterministic typed shape
    rather than interpolating raw objects into strings
  - add regression coverage for:
    - markdown task lines with context blocks
    - mixed-language plan content
    - execution-contract-derived task metadata shown in the UI
  - add a safe fallback in the UI that shows raw `PLAN.md` if structured plan
    rendering fails
- Done when:
  - plan approval UI renders tasks and context without `object Object`
    artifacts
  - operator can reliably review either parsed plan data or raw markdown
    without information loss

### B-031 Handle empty LLM streaming responses deterministically

- Area: `Platform`
- Problem:
  - `gpt-5.3-codex` planning runs produced `llm.stream.completed` with:
    - `response=""`
    - `chunk_count=0`
  - the orchestrator treated that as a normal architect completion and the run
    fell into `graph.run.failed` before any planning artifacts were persisted
- Work:
  - treat empty streaming output as an invalid result, not a successful call
  - automatically fall back to a non-streaming architect call when the stream
    yields zero content
  - if fallback is also empty, raise a precise planning error instead of a
    generic graph failure
  - add regression tests for `stream success with zero chunks`
- Done when:
  - architect planning does not fail silently on empty streamed responses
  - planning either produces artifacts or a precise typed error

### B-032 Surface provider quota exhaustion as a typed preflight/runtime failure

- Area: `Platform`
- Problem:
  - after the empty-stream fallback was fixed, planning still failed because the
    fallback call hit:
    - `OpenAIException`
    - `code=insufficient_quota`
  - the run ended as a generic `graph.run.failed`, which is too coarse for the
    operator and too late in the lifecycle
- Work:
  - detect provider quota/billing failures and map them to a typed operator-facing
    error
  - optionally add a lightweight provider readiness probe before expensive runs
  - expose the failure in project summary/UI as `provider_quota_exhausted`
    instead of only in trace
- Done when:
  - quota exhaustion is immediately obvious from the project status surface
  - operators do not need trace inspection to distinguish quota issues from
    platform bugs

### B-033 Fix preview startup when PID file is missing

- Area: `Platform`
- Problem:
  - after all build tasks completed, `preview_check` crashed because
    `start_preview_process()` treated `/tmp/automatron-preview.pid` as mandatory
  - if the preview process starts but the PID file is not written, the graph
    fails even though readiness probe could still verify a healthy preview
- Work:
  - make PID file optional during preview startup
  - capture PID from process stdout when possible
  - fall back to readiness probe and preview log diagnostics if PID file is
    absent
  - return a structured preview startup error instead of generic graph failure
- Done when:
  - missing PID file no longer crashes preview startup by itself
  - preview readiness is determined by probe success, not only by PID file

### B-034 Fix Prisma client layout for preview on mounted workspaces

- Area: `Platform`
- Problem:
  - in the live Next.js + Prisma preview flow, `@prisma/client/default.js`
    resolved `.prisma/client/default` relative to `node_modules/@prisma/client`
    while the generated client existed only in `node_modules/.prisma/client`
  - this caused app routes like `/dashboard`, `/customers`, `/invoices`, and
    `/invoices/new` to return `500` even though `/api/health` could remain
    healthy
  - current validation allowed the project to reach preview without catching
    this runtime layout incompatibility
- Work:
  - make Prisma preview/runtime setup explicitly materialize the generated
    client where `@prisma/client` expects it
  - validate Prisma client import using the same installed layout as preview
  - add regression coverage for Next.js + Prisma preview startup on mounted
    workspaces
  - tighten final validation so broken Prisma client layout cannot pass preview
    readiness
- Done when:
  - Next.js + Prisma preview routes return `200` after a clean preview start on
    the mounted workspace
  - preview no longer requires manual `.prisma/client` copy/symlink repair
  - reviewer/validator catches broken Prisma runtime layout before preview
    approval

## Before Next E2E

### B-009 Introduce explicit `awaiting_clarification` stage

- Area: `Prompting`
- Problem:
  - architect sometimes asked questions instead of producing a usable plan
- Work:
  - add a dedicated stage for missing input
  - architect must choose exactly one:
    - generate plan with defaults
    - request clarification via explicit clarification stage
- Done when:
  - no project gets stuck in `awaiting_plan_approval` without an actual
    technical plan

### B-010 Make architect prompt version-aware and regression-tested

- Area: `Prompting`
- Problem:
  - stale ecosystem assumptions leaked into plans
- Work:
  - maintain version-aware prompt guidance for major stacks
  - add regression fixtures for current Next.js/Tailwind/Prisma assumptions
- Done when:
  - prompts stop recommending obsolete files or framework patterns

### B-011 Make builder output contract concise and machine-checkable

- Area: `Prompting`
- Problem:
  - builder prompt was directionally correct but not strict enough
- Work:
  - reduce contract to a short set of mandatory outcomes
  - align each required outcome with a validator
  - avoid requirements that are prose-only and not testable
- Done when:
  - every critical contract item has a corresponding validation check

### B-012 Add stack-specific validators

- Area: `Validation`
- Problem:
  - validation logic is still too generic
- Work:
  - create validators by stack family:
    - Next.js
    - Vite
    - Python web app
  - each validator checks:
    - runtime entrypoint
    - health endpoint
    - build/test command availability
    - deploy artifact set
- Done when:
  - reviewer quality gates differ by stack instead of using one generic policy

### B-013 Split build lifecycle into finer-grained stages

- Area: `Platform`
- Problem:
  - success states are too coarse
- Work:
  - expand lifecycle to separate:
    - `building`
    - `validating`
    - `preview_ready`
    - `release_ready`
    - `deploying`
    - `deployed`
- Done when:
  - operators can see whether a project is only feature-complete or actually
    release-ready

### B-014 Add frontend-backend contract tests

- Area: `UI/API`
- Problem:
  - REST and websocket drift caused runtime confusion earlier
- Work:
  - add contract tests for:
    - project DTO
    - deploy target summary
    - CI/CD sync response
    - websocket status update payload
    - chat/log payload shapes
- Done when:
  - backend and UI cannot drift silently on core DTOs/events

### B-015 Add run timeline and failure surface in UI

- Area: `UI/API`
- Problem:
  - operators had to inspect DB, logs, and APIs directly to understand failures
- Work:
  - add a timeline panel with:
    - current phase
    - current task index/title
    - last human gate
    - last error
    - last CI run
    - last deploy run
    - checkpoint/resume marker
- Done when:
  - one project page explains where the run stopped without external debugging

### B-016 Add Windows compatibility smoke tests

- Area: `Validation`
- Problem:
  - Windows host behavior was fragile
- Work:
  - validate:
    - workspace mount paths
    - Docker host path mapping
    - local startup commands
    - golden image build
  - at minimum, provide automated smoke scripts even if CI runs on Linux
- Done when:
  - Windows is either supported with tested behavior or explicitly documented as
    unsupported for certain flows

### B-017 Add automatic backend config-change notice

- Area: `Platform`
- Problem:
  - `.env` changes were easy to forget and stale processes kept old settings
- Work:
  - log loaded model/provider settings clearly at startup
  - optionally detect config file mtime drift in development and warn that
    restart is required
- Done when:
  - operator confusion around stale env/config drops significantly

## Before Production

### B-018 Replace ad hoc secret handling with a proper secret strategy

- Area: `Platform`
- Problem:
  - current secret usage is workable for testing, but not strong enough for
    production operation
- Work:
  - define a production secret strategy for:
    - LLM keys
    - GitHub token
    - deploy credentials
  - reduce secret exposure in local files and runtime logs
- Done when:
  - production secret flow is documented, scoped, auditable, and minimally
    exposed

### B-019 Add policy-driven deployment controls

- Area: `Platform`
- Problem:
  - successful deploy still depends heavily on operator discipline
- Work:
  - enforce deploy preconditions:
    - preview approved
    - validation passed
    - required artifacts present
    - deploy target configured
    - health contract configured
- Done when:
  - deploy endpoint cannot be called successfully if a release is not actually
    ready

### B-020 Add deeper generated-app validation in CI

- Area: `Validation`
- Problem:
  - current CI relies mostly on scripts available in the generated project
- Work:
  - add Automatron-owned validation jobs where possible:
    - artifact checks
    - health route check
    - Docker build check
    - minimal runtime smoke test
- Done when:
  - deployability is asserted by CI, not inferred only from repo contents

### B-021 Separate dev preview from production-like validation

- Area: `Platform`
- Problem:
  - dev preview and production validation are currently too easy to conflate
- Work:
  - keep preview for operator review
  - add a dedicated production-like smoke validation step using built artifacts
- Done when:
  - preview success and production-readiness are independently visible

### B-022 Add run audit trail and richer observability

- Area: `UI/API`
- Problem:
  - debugging still depends on ad hoc inspection
- Work:
  - retain structured events for:
    - phase changes
    - task results
    - approvals
    - deploy attempts
    - GitHub sync results
- Done when:
  - every run can be reconstructed without scraping console output

### B-023 Define support matrix and pin compatible toolchain versions

- Area: `Platform`
- Problem:
  - compatibility drift across Next.js, Prisma, Docker, LangGraph, and host OS
    can silently break the flow
- Work:
  - document and pin supported versions
  - add upgrade checklist for:
    - LangGraph
    - Prisma
    - Next.js
    - Docker base images
- Done when:
  - dependency upgrades are deliberate and regression-tested

### B-024 Remove manual generated-code rescue from the definition of success

- Area: `Validation`
- Problem:
  - first green run still required manual edits inside the generated repo
- Work:
  - make this a hard acceptance rule:
    - no manual code edits inside generated repo are allowed during the next
      official autonomous E2E test
- Done when:
  - the next accepted E2E report can honestly state that generated application
    code was not manually fixed

## Phase 2 Strategic Backlog

These items are not only bug-fixes. They are the next capability layer after
v1 is stabilized.

Recommended rule:

- start serious `Phase 2` implementation only after the `Critical Now` and
  `Before Next E2E` items have removed manual rescue from the main flow

### P2-001 Stabilize v1 as a platform

- Area: `Platform`
- Problem:
  - v1 proved viable, but still required manual intervention during the first
    green run
- Work:
  - repeatedly run the real happy path:
    - intake
    - plan approval
    - build
    - preview
    - preview approval
    - deploy
  - complete end-to-end and integration tests for:
    - graph lifecycle
    - repository creation
    - preview startup
    - deploy flow
  - replace temporary heuristics in:
    - preview start command selection
    - deploy artifact detection
    - branch merge handling
  - add proper observability:
    - structured logs
    - run timeline
    - phase-level errors
    - operator debug screen
- Done when:
  - the happy path can be rerun consistently without manual rescue
  - platform debugging no longer depends on direct DB/container inspection

### P2-002 Integrate Solomon as a first-class requirement source

- Area: `UI/API`
- Problem:
  - Automatron currently starts from raw intake text and does not preserve rich
    product specification structure
- Work:
  - introduce a distinct domain entity:
    - `Specification` or `IntakePackage`
  - allow Solomon to send structured data:
    - requirements
    - user stories
    - acceptance criteria
    - constraints
    - preferred stack
  - add intake/spec versioning and change sync between Solomon and Automatron
  - show business input and technical plan side by side in UI
- Done when:
  - raw text is no longer the only intake mode
  - specification evolution is stored, versioned, and visible

### P2-003 Upgrade Git workflow to production level

- Area: `Platform`
- Problem:
  - current repository flow is functional, but too direct for production-grade
    governance
- Work:
  - replace direct promotion with PR flow:
    - `feature -> develop`
    - `develop -> main`
  - integrate:
    - GitHub PR creation
    - commit statuses
    - branch protection awareness
  - add:
    - release tagging
    - changelog generation
    - build artifact references
  - persist in project/run state:
    - SHA
    - PR URL
    - release URL
    - commit history
- Done when:
  - repository promotion follows reviewable pull-request based governance
  - Automatron state reflects the full release lineage

### P2-004 Make deployment secure and scalable

- Area: `Platform`
- Problem:
  - current deployment flow is workable for testing, but not mature enough for
    multi-environment or higher-trust delivery
- Work:
  - introduce environments:
    - preview
    - staging
    - production
  - move secrets out of Automatron-local handling into a managed secret system:
    - Vault
    - 1Password Connect
    - AWS SSM
    - or at minimum an encrypted secret backend
  - add:
    - rollback deploy
    - smoke tests after deploy
    - health-check retries
    - deploy locks
  - support multiple deployment targets:
    - single VPS compose
    - Docker Swarm
    - Kubernetes
    - optional PaaS
- Done when:
  - deployment is environment-aware, rollback-capable, and no longer coupled to
    one target pattern

### P2-005 Expand and harden CI/CD automation

- Area: `Platform`
- Problem:
  - CI/CD exists, but needs to become configurable, policy-driven, and robust
- Work:
  - generate `.github/workflows` for:
    - test
    - build
    - deploy
  - provision GitHub Actions secrets and environments automatically
  - support two operating modes:
    - Automatron deploys directly
    - Automatron configures CI/CD and deploys through Actions
  - add policy checks before merge/deploy:
    - tests passed
    - preview approved
    - required files present
- Done when:
  - CI/CD is not just present, but a governed release path with explicit policy
    enforcement

### P2-006 Strengthen agent-environment security

- Area: `Platform`
- Problem:
  - builder isolation improved during v1, but capability and credential
    boundaries still need hardening
- Work:
  - strictly isolate builder credentials from repository/deploy credentials
  - define capability boundaries for the agent:
    - what it can read
    - where it can push
    - which commands it can execute
  - log all sensitive actions:
    - repo creation
    - secret access
    - deploy execution
  - add an approval policy engine, not only UI buttons
- Done when:
  - agent actions are constrained by enforceable policy, not just convention

### P2-007 Mature the operator product

- Area: `UI/API`
- Problem:
  - the operator UI is now functional, but not yet a full operational control
    surface
- Work:
  - add:
    - run history
    - diff between runs
    - replay/resume tooling
    - manual task injection without editing all of `PLAN.md`
    - side-by-side preview/logs/plan/deploy status
    - multi-project queue
    - retries
    - concurrency control
- Done when:
  - operators can supervise and steer multiple projects without dropping into
    raw APIs, DB queries, or container shells

## Suggested Execution Order

Recommended order for implementation:

1. `B-001` Preflight checks
2. `B-002` Graph compile/checkpoint tests
3. `B-003` Resume semantics
4. `B-004` Required deploy artifact gates
5. `B-005` Reviewer artifact validation
6. `B-006` Standard health contract
7. `B-007` Preview restart determinism
8. `B-008` GitHub/deploy preflight
9. `B-009` Clarification stage
10. `B-010` and `B-011` prompt hardening
11. `B-012` stack validators
12. `B-013` lifecycle refinement
13. `B-014` and `B-015` contract/timeline UX
14. `B-016` and `B-017` operator-environment stability
15. production hardening items `B-018` to `B-024`
16. `P2-001` stabilize v1 as a platform
17. `P2-002` integrate Solomon as structured intake
18. `P2-003` production-grade Git workflow
19. `P2-004` secure and scalable deployment
20. `P2-005` expanded CI/CD automation
21. `P2-006` agent-environment security hardening
22. `P2-007` operator product maturation

## Exit Criteria for the Next Autonomous E2E Run

The next official autonomous E2E run should only be considered successful if:

- no manual code edits are made inside the generated project
- no manual orchestrator rescue patches are needed during the run
- plan generation is deterministic enough to proceed without ad hoc steering
- preview reflects latest code without manual process cleanup
- generated repo is deploy-ready before human preview approval
- deploy executes with a valid health check
- final report can describe the run as autonomous, not assisted
