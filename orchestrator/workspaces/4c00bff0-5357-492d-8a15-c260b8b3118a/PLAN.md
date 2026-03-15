---
project_name: "InvoiceDashboard"
stack: "Next.js (App Router) + Prisma + SQLite + Tailwind CSS"
root_dir: "/workspace"
global_rules:
  - "STRICT: All required artifacts (Dockerfile, .env.example, deploy/docker-compose.yml, DEPLOY.md, .github/workflows/ci.yml, .github/workflows/deploy.yml) must be present and production-ready."
  - "STRICT: Health endpoint must be at /api/health and probe DB connectivity."
  - "STRICT: All pages and API routes must use Next.js App Router conventions."
  - "STRICT: Prisma schema and seed must provide demo customers and invoices."
  - "STRICT: All project-specific metadata must be present in scaffolded files."
---

# План Реалізації: InvoiceDashboard

## Фаза 1: Ініціалізація та Скелет Проєкту
- [x] **Verify Next.js App Router Scaffold**: Ensure Next.js App Router project is initialized and usable.
    - *Context*: Check for `/app` directory, `next.config.js`, and project metadata in `/package.json`. Use Next.js 14+. If `next` is not installed, run `npm install next@latest react@latest react-dom@latest` to ensure Next.js is present. Validate build with `npm run build`. Update `package.json` if necessary to ensure `next`, `react`, and `react-dom` are dependencies.
- [ ] **Install and Configure Tailwind CSS**: Set up Tailwind CSS for styling.
    - *Context*: Use Tailwind v4 zero-config if possible. Verify Tailwind is working by updating `/app/page.tsx` with Tailwind classes.

## Фаза 2: База Даних та Prisma
- [ ] **Initialize Prisma and SQLite**: Set up Prisma with SQLite as the database.
    - *Context*: Create `/prisma/schema.prisma` with `Customer` and `Invoice` models. Configure SQLite datasource.
- [ ] **Create Prisma Seed Script**: Provide demo data for customers and invoices.
    - *Context*: Implement `/prisma/seed.ts` to insert at least 2 customers and 2 invoices. Update `package.json` with `prisma db seed` script.
- [ ] **Run Initial Migration and Seed**: Apply schema and seed data locally.
    - *Context*: Use `npx prisma migrate dev` and `npx prisma db seed`. Ensure `dev.db` is created in `/prisma`.

## Фаза 3: API та Health Endpoint
- [ ] **Implement /api/health Endpoint**: Create health check API route.
    - *Context*: File: `/app/api/health/route.ts`. Returns JSON `{ status, service, timestamp }` and checks DB connectivity via Prisma.
- [ ] **Create /api/customers and /api/invoices Endpoints**: Expose customers and invoices data.
    - *Context*: Files: `/app/api/customers/route.ts`, `/app/api/invoices/route.ts`. Return all records from DB as JSON.

## Фаза 4: UI Сторінки
- [ ] **Build Customers Page**: Display list of customers.
    - *Context*: File: `/app/customers/page.tsx`. Fetches data from `/api/customers` or directly via Prisma on server.
- [ ] **Build Invoices Page**: Display list of invoices with customer names.
    - *Context*: File: `/app/invoices/page.tsx`. Fetches data from `/api/invoices` or directly via Prisma on server. Shows invoice amount, status, and customer.

## Фаза 5: DevOps та Деплоймент
- [ ] **Create Dockerfile (Multi-stage)**: Production-ready Dockerfile for Next.js app.
    - *Context*: File: `/Dockerfile`. Multi-stage: build and run. Uses `node:20-alpine`. Copies Prisma files, runs migrations and seed at build/startup.
- [ ] **Create .env.example**: Template for environment variables.
    - *Context*: File: `/.env.example`. Includes `DATABASE_URL` for SQLite and any required Next.js secrets.
- [ ] **Create deploy/docker-compose.yml**: Compose file for production deployment.
    - *Context*: File: `/deploy/docker-compose.yml`. Defines app service, mounts persistent volume for SQLite DB.
- [ ] **Write DEPLOY.md**: Step-by-step deployment instructions.
    - *Context*: File: `/DEPLOY.md`. Covers Docker build, compose up, environment setup, and seed instructions.
- [ ] **Create GitHub Actions CI Workflow**: Lint, build, and test on push.
    - *Context*: File: `/.github/workflows/ci.yml`. Runs `npm run lint`, `npm run build`, and checks `/api/health`.
- [ ] **Create GitHub Actions Deploy Workflow**: Deploy to VPS on main branch push.
    - *Context*: File: `/.github/workflows/deploy.yml`. SSH to VPS, pulls repo, builds Docker image, restarts compose stack.