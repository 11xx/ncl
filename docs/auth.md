# Authenticating against Nextcloud

`ncl` authenticates with an application password obtained through Login Flow v2.
That is the only mechanism it implements.

Every fact here is traceable to the server's own source. Nextcloud's client
documentation is thin on the parts that decide a client's shape, and the parts
it omits are the ones that break implementations quietly.

## Login Flow v2

The flow is designed for exactly this case: a native client that can open a
browser but cannot safely hold a long-term secret of its own.

1. `POST /index.php/login/v2` returns a `login` URL for the user to visit and a
   `poll` object holding a token and an endpoint. The token is valid for twenty
   minutes.
2. The client shows the login URL and opens it. The user authenticates in their
   own browser, with two-factor authentication if their account requires it,
   and grants access.
3. The client polls the endpoint with the token. **A 404 means consent has not
   happened yet**, not that anything is missing — polling continues.
4. A single 200 returns `{server, loginName, appPassword}`. It is returned
   exactly once and cannot be requested again.

Three consequences shape the implementation:

- **The credential arrives once, so ordering is load-bearing.** The secret
  store must be proven to round-trip *before* consent is requested. Discovering
  an unusable store afterwards costs the user a second trip through the browser
  and leaves a live application password on the server that nobody knows to
  revoke.
- **The URLs come from the server and must be checked.** A `login` or poll
  endpoint whose origin differs from the configured one is refused rather than
  followed.
- **`loginName` is not the account UID.** Nextcloud permits logging in with a
  UID, an email address, or other identifiers. A DAV path built from it
  addresses the wrong account or none, so the account is discovered from the
  authenticated principal instead.

The credential lifecycle is serialized per profile. `ncl login`, forced
replacement, and `ncl logout` hold an exclusive lock under
`$XDG_RUNTIME_DIR/ncl/` from credential preflight through storage or removal
and principal verification. The per-profile lock file persists, while the
kernel owns lock liveness through `flock`: process exit releases the lock
without PID recording, stale-file recovery, or PID-reuse assumptions.

Secret backends distinguish an absent entry from an unusable backend. `pass`
reports absence with its explicit “not in the password store” result, while
Secret Service reports it with an empty successful lookup. Decryption,
agent, service, and other backend failures remain failures, so `ncl doctor`
directs the caller to repair the backend rather than treating the credential
as absent.

The authenticated request boundary registers the application password, the
`user:password` pair, its base64 encoding, and its Basic-auth form. Structured
output is recursively redacted, and the CLI wraps its standard text streams so
`argparse`, direct `print` calls, and library text writers pass through the same
redaction. The wrapper matches within a write and across
write boundaries alike, so a value broken in two by a buffered writer is still
redacted; text that merely ends inside a credential prefix is held only until
the rest of the output arrives, and is released unchanged when the invocation
finishes. Replacement repeats until the text stops changing, because the
replacement marker contributes characters that could otherwise complete a
second registered value at the seam. Raw
file-descriptor and binary-buffer writes remain prohibited by project discipline
rather than intercepted by the stream wrapper.

The resulting credential does not expire, is revocable on its own from the
account's security settings with a visible last-used timestamp, and carries no
client secret.

## Bearer tokens do reach the DAV endpoints

Worth recording because it is widely believed otherwise, and because it is the
fact that makes the OAuth question a real decision rather than a foregone one.

`apps/dav/lib/Server.php` builds the server behind `/remote.php/dav` and
registers a `BearerAuth` backend on the auth plugin *before* the basic-auth
backend. `BearerAuth::validateBearerToken` delegates to
`IUserSession::tryTokenLogin`, which resolves any token in the authentication
token store — the same store the OAuth2 app writes its access tokens into.

The ordering misleads in one direction: a request whose bearer token does not
resolve falls through to the next backend, and the eventual refusal is phrased
in terms of basic auth. **A 401 mentioning `Authorization: Basic` is not
evidence that the server ignored the bearer header** — it is evidence that the
token was rejected. Reports that OAuth cannot reach WebDAV or CalDAV are that
misreading.

## Why OAuth2 is not used

It is feasible, as above. It was rejected because it buys nothing here and
costs several things. All of this is read from `apps/oauth2`.

- **No scopes.** The admin manual states it plainly: every token has full
  access to the complete account, read and write. An OAuth access token and an
  application password therefore carry identical authority, which removes the
  usual reason to prefer OAuth.
- **No PKCE, confidential clients only.** The authorize endpoint accepts no
  `code_challenge`, so a command-line client must store a client secret. For a
  native application that is a weakening of the OAuth model, not a
  strengthening of the credential.
- **The redirect URI is fixed.** The authorize endpoint ignores the
  `redirect_uri` a client sends and uses the one registered against the client.
  The `http://localhost:*` wildcard applies only when an administrator has
  enabled the non-default `oauth2.enable_oc_clients` setting. A loopback
  listener therefore cannot choose an ephemeral port.
- **Access tokens last 3600 seconds**, hardcoded in the token controller.
- **Refresh tokens rotate, and replay is throttled.** Redeeming one invalidates
  it and issues a replacement in the same response; presenting a spent token is
  throttled under `refresh_token_already_redeemed` as a brute-force signal.
  That combination has a permanent-lockout failure mode. A crash between
  redeeming and persisting the replacement costs the credential outright, and
  two concurrent invocations redeeming the same token lock the tool out of its
  own account unless every refresh is serialized by a cross-process lock.
- **It requires an administrator-registered client**, which is setup the user
  must perform and can later delete out from under the tool.

Revisit if Nextcloud ships genuinely scoped tokens, or if an administrator
requires a centrally registered client and disables Login Flow.

## Scope, and what a credential is actually worth

Because the server enforces no scopes, every credential it issues carries full
account access. The tool's own allowlist is therefore the only scope boundary
that exists in the system: the server will not decline a request for a
collection outside the configured scope, so only the client will.

That is why the allowlist is compared on canonical discovered hrefs — decoded
per path segment and normalized — rather than on display names or string
prefixes.
