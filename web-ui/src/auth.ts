import NextAuth from "next-auth";
import Google from "next-auth/providers/google";

// Comma-separated allowlist from env. If empty, ANY Google account can sign in
// (dev convenience — should always be set in production).
const allowed = (process.env.AUTOMATRON_ALLOWED_EMAILS ?? "")
  .split(",")
  .map((e) => e.trim().toLowerCase())
  .filter(Boolean);

export const { handlers, auth, signIn, signOut } = NextAuth({
  // Use JWE (default) so the FastAPI orchestrator can decrypt the same cookie
  // using AUTH_SECRET via HKDF-SHA256. See orchestrator/orchestrator/auth.py.
  session: { strategy: "jwt" },
  providers: [Google],
  callbacks: {
    signIn({ profile }) {
      const email = (profile?.email ?? "").toLowerCase();
      if (allowed.length === 0) return true; // open mode
      return allowed.includes(email);
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
