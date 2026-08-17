# Authenticating against Nextcloud

Every fact here is traceable to the server's own source. Nextcloud's client
documentation is thin on the parts that decide a client's shape, and the parts
it omits are the ones that break implementations quietly.

## Bearer tokens reach the DAV endpoints

`apps/dav/lib/Server.php` builds the server behind `/remote.php/dav` and
registers a `BearerAuth` backend on the auth plugin *before* the basic-auth
backend. `BearerAuth::validateBearerToken` delegates to
`IUserSession::tryTokenLogin`, which resolves any token in the authentication
token store — the same store the OAuth2 app writes its access tokens into.

So an OAuth2 access token authenticates CalDAV and WebDAV, not merely the OCS
APIs. Reports of `Bearer` failing against `remote.php/dav` describe either
older servers or malformed `Authorization` headers, and should not be taken as
a reason to reach for basic auth.

The ordering matters in one direction only: a request carrying a bearer token
that does not resolve falls through to the next backend, and the eventual
refusal is phrased in terms of basic auth. A 401 mentioning `Authorization:
Basic` is therefore not evidence that the server ignored the bearer header — it
is evidence that the token was rejected.

## The authorization-code flow, as this server implements it

Two grant types exist and no others: `authorization_code` and `refresh_token`.
Anything else is refused before the request is examined.

- Authorize: `/index.php/apps/oauth2/authorize`, taking `client_id`,
  `state`, `response_type`, and `redirect_uri`.
- Token: `/index.php/apps/oauth2/api/v1/token`.

**There is no PKCE.** The authorize endpoint accepts no `code_challenge`, so
the code cannot be bound to the requesting client by proof of possession. The
server supports confidential clients only, which means a client secret is
mandatory and has to live wherever the tool keeps secrets. A native CLI holding
a client secret is a known compromise of the OAuth model; here it is not
optional, so the secret is treated exactly like the tokens it obtains.

**Access tokens live 3600 seconds.** The value is not negotiable and not
advertised per client; the token controller hardcodes both the stored expiry
and the `expires_in` it returns.

**Refresh tokens rotate, and replay is punished.** Redeeming a refresh token
invalidates it and issues a new one in the same response. Presenting a spent
token is not merely refused: the controller throttles it under
`refresh_token_already_redeemed`, which is brute-force protection and slows
subsequent attempts from the same source.

Two consequences the implementation must honour, because the failure is silent
and self-inflicted:

- The new refresh token has to be durably stored *before* the access token is
  used for anything, and a crash between redeeming and storing costs the
  credential permanently — recovery means another browser consent.
- Refresh has to be serialized across processes. A tool a harness may invoke
  several times concurrently will otherwise have two invocations redeem the
  same refresh token, and the loser both fails and trips the throttle. A
  cross-process lock around the refresh is a correctness requirement, not a
  tuning detail.

## Scope, and what a token is actually worth

Nextcloud's OAuth2 has no scopes. The admin manual states it plainly: every
token has full access to the complete account, including read and write access
to stored files. An access token, a refresh token, and an application password
therefore all carry identical authority, and the only real differences are
lifetime and how each is revoked.

This is why the tool's own allowlist is a security boundary rather than a
convenience. The server will not decline a request for a collection outside the
configured scope; only the client will.

## Application passwords, and why the code still supports them

Login Flow v2 obtains a per-device application password through the same
browser consent the OAuth flow uses: `POST /index.php/login/v2` returns a login
URL and a poll token, the user grants in the browser, and a single successful
poll returns the password exactly once. The credential does not expire, needs
no client registration, and is revocable individually.

It remains supported because it survives conditions the OAuth flow does not: a
server whose OAuth2 client registration has been removed, an unattended run
whose refresh token was invalidated, and any host where storing a client secret
is not acceptable. It is a fallback rather than the default.
