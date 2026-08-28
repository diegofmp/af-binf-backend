# Authentication design

This service authenticates browser users via company SSO using OIDC
Authorization Code + PKCE, and issues a **server-side session** rather than
handing the browser any token. This document explains why, so it's ready to
hand to security review.

## The pattern

1. `GET /auth/login` redirects the browser to the IdP's authorization
   endpoint. Before redirecting, it generates a `state` value and a PKCE
   `code_verifier`/`code_challenge` pair, and stores `{state -> code_verifier,
   post_login_redirect}` in Redis with a 10-minute TTL. Nothing sensitive is
   exposed to the browser at this step beyond the `state` param the IdP
   round-trips back.
2. `GET /auth/callback` receives `code` + `state` from the IdP. It looks up
   and deletes the pending-login entry keyed by `state` (single use — this
   also prevents replay of a stale authorization code exchange), exchanges
   `code` for tokens directly server-to-server (never via the browser),
   validates the ID token's signature/issuer/audience against the IdP's
   published JWKS, and upserts a `User` row keyed on the OIDC `sub` claim.
3. A new **opaque, random session ID** (`secrets.token_urlsafe(32)`, not a
   JWT, not derived from anything guessable) is minted and stored in Redis as
   `session:<id> -> {user_id, created_at, expires_at}`. Only this random ID
   is set on the response, as an `HttpOnly` cookie.
4. Every subsequent request runs through the `get_current_user` FastAPI
   dependency, which reads the cookie, looks the session up in Redis, loads
   the `User`, 401s if the session is missing/expired, and slides the TTL
   forward on activity.
5. `POST /auth/logout` deletes the Redis entry and clears the cookie —
   revocation is immediate and server-side, not "wait for the token to
   expire."

## Why not a JWT in the browser?

The alternative — issuing a signed JWT (access token) that the frontend
stores in `localStorage`/`sessionStorage` and attaches as a bearer header —
was deliberately avoided:

- **XSS blast radius.** Anything readable by JavaScript (`localStorage`, a
  non-`HttpOnly` cookie) is readable by an XSS payload. A stolen bearer token
  is a self-contained credential an attacker can replay from anywhere until
  it expires, and a SPA typically can't outright revoke individual JWTs
  without an extra revocation list (which reintroduces server-side state
  anyway — at which point you might as well use a session). An `HttpOnly`
  cookie is never exposed to `document.cookie` or any JS the page runs,
  including injected script.
- **No client-side revocation.** With an opaque session ID, "log this user
  out everywhere" or "an admin needs to kill a compromised session" is a
  single Redis `DEL`. A self-contained JWT keeps working until it expires
  unless you build a denylist — which is just a worse-shaped session store.
- **Nothing to leak from the token itself.** The session cookie's value is
  random and carries no claims. Even if it leaked in a log line or a proxy
  header dump, it reveals no identity, roles, or provider tokens — those
  live server-side in Redis, looked up by the opaque ID.
- **Session fixation resistance.** A fresh, unpredictable session ID is
  minted only after the IdP round-trip completes and the ID token has been
  validated — never accepted from the client beforehand — so an attacker
  can't pre-set a known session ID for a victim to authenticate into.

The tradeoff is that this requires Redis (or another shared store) to be
available to every API process, and each authenticated request costs one
Redis lookup. Both are cheap.

## Cookie flags

- `HttpOnly=True` — inaccessible to JavaScript, the core XSS mitigation
  above.
- `Secure=True` — never sent over plain HTTP; required in every non-local
  deployment.
- `SameSite=Lax` — chosen over `Strict` because the OIDC redirect flow is a
  top-level browser navigation initiated by the IdP (`/auth/callback` is hit
  via a 302 redirect from a third-party origin). `Strict` cookies are not
  sent on a top-level cross-site navigation from a link/redirect on first
  arrival, which would mean the session cookie set at `/auth/callback` works,
  but any pre-existing cookie context needed *during* the redirect chain
  could behave inconsistently across browsers. `Lax` still blocks the cookie
  from being attached to cross-site subrequests (images, iframes, fetch calls
  from other origins) and cross-site POSTs, which is what actually matters
  for CSRF — it only relaxes top-level GET navigations, which is exactly the
  IdP redirect this flow depends on. If your IdP's flow doesn't require this
  (e.g. you front it with a same-site proxy), `Strict` is a strictly tighter
  option and can be swapped via `SESSION_COOKIE_SAMESITE`.
- No `Max-Age` beyond `SESSION_TTL_SECONDS`, refreshed on activity — an idle
  session expires; an active one doesn't force a re-login mid-task.

## CSRF

Because the session travels as a cookie the browser attaches automatically,
`SameSite=Lax` alone doesn't fully close CSRF for state-changing requests
(some legacy browsers, and cross-site simple `<form>` POSTs from *some*
contexts, are still a concern). This service adds a **double-submit cookie**:
a second, JS-readable cookie (`af_csrf`, not `HttpOnly`) is issued on first
response, and every `POST`/`PATCH`/`DELETE` must echo that value back in the
`X-CSRF-Token` header. A cross-site attacker can trigger the browser to send
the cookie, but cannot read it (cross-origin) to also set the matching
header — so a forged request fails the comparison in `app/auth/csrf.py`.

## What's still on you

- Point `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`/`OIDC_REDIRECT_URI`
  at your actual IdP (see `.env.example`); nothing here is hardcoded to a
  specific provider.
- Redis must be run with persistence/backup appropriate to how disruptive a
  mass session loss would be (worst case: everyone has to log in again — not
  a data-loss event).
- `APP_SECRET_KEY` and `OIDC_CLIENT_SECRET` must come from a secret manager in
  any real deployment, not the checked-in `.env.example` defaults.
