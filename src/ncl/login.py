"""Login Flow v2, local credential lifecycle, and logout."""

from __future__ import annotations

import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from typing import Any

from . import exits, identity, secrets
from .session import (
    Response,
    Session,
    SessionError,
    Transport,
    UrllibTransport,
    absolute_url,
    parse_server_url,
)


class LoginError(RuntimeError):
    """A Login Flow v2 operation ended with a caller-actionable result."""

    def __init__(self, message: str, code: int) -> None:
        self.code = code
        super().__init__(message)


def _json_response(response: Response) -> dict[str, Any]:
    if response.status != 200:
        raise LoginError("the server did not start Login Flow v2", exits.SERVER_ERROR)
    try:
        value = response.json()
    except SessionError as exc:
        raise LoginError(str(exc), exc.code) from exc
    if not isinstance(value, dict):
        raise LoginError("the Login Flow v2 response was malformed", exits.MALFORMED_RESPONSE)
    return value


def _same_origin_url(profile: Any, value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LoginError(f"the Login Flow v2 {label} URL was malformed", exits.MALFORMED_RESPONSE)
    try:
        return absolute_url(profile, value)
    except SessionError as exc:
        raise LoginError(f"the Login Flow v2 {label} URL was refused", exc.code) from exc


def _poll_url(endpoint: str, token: str) -> str:
    try:
        parsed = parse_server_url(endpoint)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("token", token))
        encoded_query = urllib.parse.urlencode(query)
    except SessionError as exc:
        raise LoginError("the Login Flow v2 poll URL was malformed", exc.code) from exc
    except (TypeError, UnicodeError, ValueError) as exc:
        raise LoginError(
            "the Login Flow v2 poll URL was malformed", exits.MALFORMED_RESPONSE
        ) from exc
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, encoded_query, parsed.fragment)
    )


def _request(
    transport: Transport,
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> Response:
    try:
        return transport.request(method, url, headers=headers, data=data, timeout=timeout)
    except KeyboardInterrupt:
        raise
    except LoginError:
        raise
    except Exception as exc:
        raise LoginError(
            "the configured origin became unreachable during login", exits.UNREACHABLE
        ) from exc


def _store(profile: Any, payload: dict[str, Any]) -> None:
    login_name = payload.get("loginName")
    app_password = payload.get("appPassword")
    if not isinstance(login_name, str) or not login_name:
        raise LoginError("the Login Flow v2 response omitted loginName", exits.MALFORMED_RESPONSE)
    if not isinstance(app_password, str) or not app_password:
        raise LoginError("the Login Flow v2 response omitted appPassword", exits.MALFORMED_RESPONSE)
    try:
        secrets.store_credentials(profile, login_name, app_password)
    except secrets.SecretError as exc:
        raise _orphaned_credential("the credential store failed") from exc


def _orphaned_credential(reason: str) -> LoginError:
    return LoginError(
        "consent succeeded; an application password was issued but could not be stored "
        f"because {reason}. The orphaned application password must be revoked from the "
        "account Security settings",
        exits.CREDENTIAL_STORE_FAILED,
    )


def authenticate(
    profile: Any,
    *,
    force: bool = False,
    timeout: float = 1200,
    poll_interval: float = 1,
    max_poll_interval: float = 5,
    transport: Transport | None = None,
    browser_open: Callable[[str], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    output: Callable[[str], Any] = print,
) -> identity.Identity:
    """Complete Login Flow v2 and prove the stored credential with DAV."""
    browser_open = browser_open or webbrowser.open
    if not secrets.probe(profile):
        raise LoginError(
            "the configured secret backend cannot round-trip a value",
            exits.PRECONDITION_FAILED,
        )

    transport = transport or UrllibTransport()
    if secrets.has_credentials(profile):
        if not force:
            raise LoginError(
                "a credential is already stored; run `ncl logout` first", exits.CONFLICT
            )
        # Refusing after failed revocation prevents --force from hiding a live orphaned token.
        try:
            logout(profile, transport=transport)
        except LoginError as exc:
            raise LoginError(
                "the existing credential could not be revoked; refusing --force login; "
                "revoke it from the account Security settings",
                exits.REVOCATION_FAILED,
            ) from exc
        output(
            "The existing credential was revoked before browser consent; cancelling "
            "now will leave the profile without a credential."
        )

    start_url = absolute_url(profile, "/index.php/login/v2")
    response = _request(
        transport,
        "POST",
        start_url,
        headers={"Accept": "application/json"},
    )
    payload = _json_response(response)
    login_url = _same_origin_url(profile, payload.get("login"), "login")
    poll = payload.get("poll")
    if not isinstance(poll, dict):
        raise LoginError(
            "the Login Flow v2 response omitted poll details", exits.MALFORMED_RESPONSE
        )
    endpoint = _same_origin_url(profile, poll.get("endpoint"), "poll")
    token = poll.get("token")
    if not isinstance(token, str) or not token:
        raise LoginError(
            "the Login Flow v2 response omitted the poll token", exits.MALFORMED_RESPONSE
        )

    output(f"Open this URL to authorize ncl: {login_url}")
    output("The terminal is waiting for browser consent.")
    try:
        if not browser_open(login_url):
            output("The browser did not open; use the printed URL while the terminal waits.")
    except OSError:
        output("The browser could not be opened; use the printed URL while the terminal waits.")

    started = clock()
    deadline = min(timeout, 1200)
    issued = False
    interval = poll_interval
    try:
        while True:
            elapsed = clock() - started
            if elapsed >= 1200:
                raise LoginError("the Login Flow v2 poll token expired", exits.TOKEN_EXPIRED)
            if elapsed >= deadline:
                raise LoginError(
                    "browser consent timed out while the user was still deciding",
                    exits.LOGIN_TIMEOUT,
                )
            try:
                response = _request(
                    transport,
                    "GET",
                    _poll_url(endpoint, token),
                    headers={"Accept": "application/json"},
                )
            except LoginError as exc:
                if exc.code == exits.UNREACHABLE:
                    raise LoginError(
                        "the configured origin became unreachable during login",
                        exits.UNREACHABLE,
                    ) from exc
                raise
            if response.status == 404:
                sleep(min(interval, max(0, deadline - elapsed)))
                interval = min(max_poll_interval, interval * 2)
                continue
            if response.status in {401, 403}:
                raise LoginError("the browser consent was denied", exits.CONSENT_DENIED)
            if response.status in {408, 410}:
                raise LoginError("the Login Flow v2 poll token expired", exits.TOKEN_EXPIRED)
            if response.status != 200:
                raise LoginError(
                    "the Login Flow v2 poll response was unexpected", exits.SERVER_ERROR
                )
            issued = response.status == 200
            try:
                payload = _json_response(response)
                server = payload.get("server")
                if not isinstance(server, str) or not server:
                    if issued:
                        raise _orphaned_credential("the Login Flow v2 response omitted server")
                    raise LoginError(
                        "the Login Flow v2 response omitted server", exits.MALFORMED_RESPONSE
                    )
                try:
                    absolute_url(profile, server)
                except SessionError as exc:
                    if issued:
                        raise _orphaned_credential(
                            "the Login Flow v2 server URL was refused"
                        ) from exc
                    raise LoginError(
                        "the Login Flow v2 server URL was refused", exc.code
                    ) from exc
                try:
                    _store(profile, payload)
                except LoginError as exc:
                    if issued:
                        raise _orphaned_credential("the credential could not be stored") from exc
                    raise
            except KeyboardInterrupt:
                raise
            except LoginError as exc:
                if issued and exc.code != exits.CREDENTIAL_STORE_FAILED:
                    raise _orphaned_credential("the credential could not be stored") from exc
                raise
            except Exception as exc:
                if issued:
                    raise _orphaned_credential("the credential could not be stored") from exc
                raise LoginError(
                    "the Login Flow v2 response could not be validated",
                    exits.MALFORMED_RESPONSE,
                ) from exc
            break
    except KeyboardInterrupt as exc:
        if issued:
            raise _orphaned_credential("the login operation was interrupted") from exc
        raise LoginError(
            "interrupted before browser consent was granted", exits.LOGIN_TIMEOUT
        ) from exc

    authenticated = Session(profile, transport=transport)
    try:
        result = identity.discover(profile, session=authenticated)
    except (SessionError, identity.IdentityError) as exc:
        raise LoginError(
            "the stored credential could not be proved with principal discovery",
            getattr(exc, "code", exits.MALFORMED_RESPONSE),
        ) from exc
    output(
        f"Authenticated as {result.account_name} ({result.display_name}); "
        f"principal {result.principal_url}; calendar home {result.calendar_home}."
    )
    return result


def logout(
    profile: Any,
    *,
    transport: Transport | None = None,
) -> bool:
    """Attempt server revocation, then remove the local credential regardless."""
    transport = transport or UrllibTransport()
    revoked = False
    try:
        session = Session(profile, transport=transport)
        response = session.request(
            "DELETE",
            "/ocs/v2.php/core/apppassword",
            headers={"Accept": "application/json", "OCS-APIRequest": "true"},
        )
        if response.status == 200:
            try:
                value = response.json()
            except SessionError:
                value = None
            if isinstance(value, dict):
                ocs = value.get("ocs")
                meta = ocs.get("meta") if isinstance(ocs, dict) else None
                statuscode = meta.get("statuscode") if isinstance(meta, dict) else None
                revoked = statuscode == 100
    except (SessionError, OSError, ValueError):
        revoked = False

    try:
        secrets.clear_credentials(profile)
    except secrets.SecretError as exc:
        if not revoked:
            raise LoginError(
                "revocation failed and the local credential could not be removed; "
                "revoke it from the account Security settings",
                exits.REVOCATION_FAILED,
            ) from exc
        raise LoginError(
            "the server credential was revoked but local removal failed", exits.ERROR
        ) from exc

    if not revoked:
        raise LoginError(
            "the application password could not be revoked; it was removed locally, "
            "so revoke it from the account Security settings",
            exits.REVOCATION_FAILED,
        )
    return True
