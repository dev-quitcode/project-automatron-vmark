#!/bin/bash
set -e

echo "=== Automatron: Initializing Next.js project ==="
cd /workspace
TMP_DIR="/tmp/automatron-next-app"

rm -rf "$TMP_DIR"

# Initialize Next.js with TypeScript, Tailwind, ESLint, App Router, and npm.
# Keep the generated scaffold intact instead of forcing legacy Tailwind files.
npx --yes create-next-app@latest "$TMP_DIR" \
    --typescript \
    --tailwind \
    --eslint \
    --app \
    --no-src-dir \
    --import-alias "@/*" \
    --use-npm \
    --skip-install \
    --yes

# Merge the generated scaffold into /workspace while preserving existing
# Automatron repo/docs files such as .git, PLAN.md, DEPLOY.md, .github, and deploy/.
find "$TMP_DIR" -mindepth 1 -maxdepth 1 ! -name node_modules ! -name .next -exec mv -f {} /workspace/ \;
rm -rf "$TMP_DIR"

# Normalize package metadata for repeatable generated repos.
if [ -f package.json ]; then
  node - <<'EOF'
const fs = require("fs");
const path = "package.json";
const pkg = JSON.parse(fs.readFileSync(path, "utf8"));
if (!pkg.name || pkg.name === "workspace") {
  pkg.name = "automatron-app";
}
fs.writeFileSync(path, JSON.stringify(pkg, null, 2) + "\n");
EOF
fi

# Install runtime dependencies in the real workspace so validation/build
# commands can run immediately after scaffold.
if [ -f package.json ]; then
  npm install
fi

mkdir -p app/api/health
if [ ! -f app/api/health/route.ts ]; then
  cat > app/api/health/route.ts <<'EOF'
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  return NextResponse.json(
    {
      status: "ok",
      service: "automatron-app",
      timestamp: new Date().toISOString(),
    },
    { status: 200 },
  );
}
EOF
fi

echo "=== Next.js scaffold complete ==="
