import NextAuth from "next-auth";
import Google from "next-auth/providers/google";

// AUTOMATRON_ALLOWED_EMAILS rules: comma-separated. Each rule is either a
// full email (exact match) or a domain pattern starting with `@` (e.g.
// `@quitcode.com` allows everyone at that domain). Empty = delegate to Google.
const allowRules = (process.env.AUTOMATRON_ALLOWED_EMAILS ?? "")
  .split(",")
  .map((e) => e.trim().toLowerCase())
  .filter(Boolean);

function emailAllowed(email: string): boolean {
  if (allowRules.length === 0) return true;
  const lower = email.toLowerCase();
  return allowRules.some((rule) =>
    rule.startsWith("@") ? lower.endsWith(rule) : lower === rule,
  );
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  // Use JWE (default) so the FastAPI orchestrator can decrypt the same cookie
  // using AUTH_SECRET via HKDF-SHA256. See orchestrator/orchestrator/auth.py.
  session: { strategy: "jwt" },
  providers: [Google],
  callbacks: {
    signIn({ profile }) {
      return emailAllowed(profile?.email ?? "");
    },
    async jwt({ token, profile }) {
      // First sign-in: copy email + name from the Google profile so it sticks
      // in the JWT for downstream consumers (orchestrator's require_auth).
      if (profile) {
        token.email = profile.email ?? token.email;
        token.name = profile.name ?? token.name;
        token.picture = profile.picture ?? token.picture;
      }
      return token;
    },
    async session({ session, token }) {
      if (session.user) {
        session.user.email = (token.email as string) ?? session.user.email;
        session.user.name = (token.name as string) ?? session.user.name;
        session.user.image = (token.picture as string) ?? session.user.image;
      }
      return session;
    },
  },
  pages: {
    signIn: "/login",
  },
  trustHost: true, // we're behind Traefik
});
