"""Authenticated HTTP transport with origin and credential safety checks."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from . import exits, profiles, render, secrets


class SessionError(RuntimeError):
    """An authenticated request was refused or could not be completed."""

    def __init__(self, message: str, code: int) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionError(
                "the server returned malformed JSON", exits.MALFORMED_RESPONSE
            ) from exc


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
        timeout: float | None = None,
    ) -> Response: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_server_url(value: str) -> urllib.parse.SplitResult:
    """Parse a URL supplied by the server without exposing parser failures."""
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.username, parsed.password, parsed.hostname, parsed.port
        value.encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise SessionError("the server URL is malformed", exits.MALFORMED_RESPONSE) from exc
    return parsed


def _resolve_server_url(base: str, reference: str) -> tuple[str, urllib.parse.SplitResult]:
    try:
        resolved = urllib.parse.urljoin(base, reference)
    except (TypeError, ValueError) as exc:
        raise SessionError("the server URL is malformed", exits.MALFORMED_RESPONSE) from exc
    return resolved, parse_server_url(resolved)


class UrllibTransport:
    """A transport that returns redirects instead of following them."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
        timeout: float | None = None,
    ) -> Response:
        request = urllib.request.Request(
            url,
            data=data,
            headers=dict(headers or {}),
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return Response(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as response:
            try:
                body = response.read()
            finally:
                response.close()
            return Response(
                status=response.code,
                headers=dict(response.headers.items()),
                body=body,
                url=url,
            )
        except (OSError, urllib.error.URLError) as exc:
            raise SessionError("the configured origin was unreachable", exits.UNREACHABLE) from exc


def _origin_parts(url: str) -> tuple[str, str, int | None]:
    parse_server_url(url)
    try:
        return profiles.origin_parts(url)
    except (TypeError, ValueError) as exc:
        raise SessionError("the server URL is malformed", exits.MALFORMED_RESPONSE) from exc


def _retry_after_detail(value: str | None) -> str:
    if value is None:
        return "no retry delay was provided"
    if not isinstance(value, str):
        return "Retry-After was unparseable"
    candidate = value.strip()
    if candidate.isascii() and candidate.isdigit():
        # Six digits already allow an eleven-day delay; longer values are not useful hints.
        if len(candidate) > 6:
            return "Retry-After was unparseable"
        return f"retry delay: {int(candidate)} seconds"
    try:
        parsed = parsedate_to_datetime(candidate)
        if parsed is None:
            return "Retry-After was unparseable"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        return f"retry delay until {parsed.isoformat()}"
    except (AttributeError, TypeError, ValueError, OverflowError):
        return "Retry-After was unparseable"


def same_origin(origin: str, url: str) -> bool:
    """Return whether two URLs have the same scheme, host, and effective port."""
    try:
        return _origin_parts(origin) == _origin_parts(url)
    except SessionError:
        return False


def absolute_url(profile: Any, url_or_path: str) -> str:
    """Resolve a path and reject a URL outside the configured origin."""
    url, parsed = _resolve_server_url(profile.origin.rstrip("/") + "/", url_or_path)
    if parsed.username is not None or parsed.password is not None:
        raise SessionError("the request URL contains user information", exits.MALFORMED_RESPONSE)
    if not same_origin(profile.origin, url):
        raise SessionError(
            "the request URL is outside the configured origin", exits.MALFORMED_RESPONSE
        )
    return url


class Session:
    """Authenticated requests for one profile."""

    def __init__(
        self,
        profile: Any,
        *,
        transport: Transport | None = None,
        timeout: float = 30,
    ) -> None:
        self.profile = profile
        self.transport = transport or UrllibTransport()
        self.timeout = timeout

    def request(
        self,
        method: str,
        url_or_path: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | str | None = None,
        max_redirects: int = 5,
    ) -> Response:
        url = absolute_url(self.profile, url_or_path)
        try:
            login_name = secrets.get(self.profile, "login_name")
            app_password = secrets.get(self.profile, "app_password")
            render.register_secret(app_password)
        except secrets.SecretError as exc:
            raise SessionError(
                "the selected profile's secret backend failed", exits.PRECONDITION_FAILED
            ) from exc
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            raise SessionError(
                "the selected profile's secret backend failed", exits.PRECONDITION_FAILED
            ) from exc
        if not login_name or not app_password:
            raise SessionError(
                "the selected profile has no stored credential", exits.NO_CREDENTIAL
            )

        auth_value = base64.b64encode(f"{login_name}:{app_password}".encode()).decode("ascii")
        render.register_secret(f"Basic {auth_value}")
        request_headers = {**dict(headers or {}), "Authorization": f"Basic {auth_value}"}
        request_data = data.encode("utf-8") if isinstance(data, str) else data
        current_method = method.upper()
        for redirect_count in range(max_redirects + 1):
            try:
                response = self.transport.request(
                    current_method,
                    url,
                    headers=request_headers,
                    data=request_data,
                    timeout=self.timeout,
                )
            except SessionError:
                raise
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                raise SessionError("the authenticated request failed", exits.UNREACHABLE) from exc

            if (
                response.url
                and response.url != url
                and not same_origin(self.profile.origin, response.url)
            ):
                raise SessionError(
                    "cross-origin redirect refused; credentials were not resent",
                    exits.MALFORMED_RESPONSE,
                )
            location = response.header("Location")
            if response.status in {301, 302, 303, 307, 308} and location:
                target, _ = _resolve_server_url(url, location)
                if not same_origin(self.profile.origin, target):
                    raise SessionError(
                        "cross-origin redirect refused; credentials were not resent",
                        exits.MALFORMED_RESPONSE,
                    )
                if redirect_count == max_redirects:
                    raise SessionError(
                        "too many redirects from the server", exits.MALFORMED_RESPONSE
                    )
                url = target
                if response.status == 303 or (
                    response.status in {301, 302} and current_method not in {"GET", "HEAD"}
                ):
                    current_method = "GET"
                    request_data = None
                continue

            if response.status == 401:
                raise SessionError(
                    "the stored credential was rejected; run `ncl login`",
                    exits.CREDENTIAL_REJECTED,
                )
            retry_after = response.header("Retry-After")
            if response.status == 429:
                raise SessionError(
                    f"the server is rate-limiting; {_retry_after_detail(retry_after)}",
                    exits.THROTTLED,
                )
            if 500 <= response.status <= 599:
                detail = (
                    f"; {_retry_after_detail(retry_after)}"
                    if retry_after is not None
                    else ""
                )
                raise SessionError(
                    f"the server returned a server error{detail}", exits.SERVER_ERROR
                )
            return response

        raise SessionError("too many redirects from the server", exits.MALFORMED_RESPONSE)


def json_body(value: Any) -> bytes:
    """Encode a request body without ever including a credential."""
    return json.dumps(value, separators=(",", ":")).encode("utf-8")
