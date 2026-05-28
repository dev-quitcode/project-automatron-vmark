import { auth } from "@/auth";
import { NextResponse } from "next/server";

// Paths that should bypass auth. Order matters — first match wins.
const PUBLIC_PATHS = [
  "/login",
  "/api/auth", // Auth.js endpoints (callback, signin, csrf, etc.)
  "/_next",
  "/favicon.ico",
  "/notification.mp3",
];

function isPublic(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p + "/"));
}

export default auth((req) => {
  const { pathname } = req.nextUrl;

  if (isPublic(pathname)) {
    return NextResponse.next();
  }

  if (!req.auth) {
    // For API requests, return 401 JSON. For UI requests, redirect to /login.
    if (pathname.startsWith("/api/")) {
      return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
    }
    const loginUrl = new URL("/login", req.nextUrl.origin);
    loginUrl.searchParams.set("callbackUrl", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
});

export const config = {
  // Run on every path EXCEPT static assets that don't go through Next.js.
  // /api/webhooks/* flows through Traefik straight to the orchestrator, NOT
  // through Next.js, so we don't need to exempt it here.
  matcher: ["/((?!_next/static|_next/image|.*\\..*).*)"],
};
