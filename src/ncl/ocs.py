"""The OCS envelope, which every non-DAV Nextcloud API answers in.

Nextcloud's own APIs — sharing, provisioning, notifications, capabilities —
do not speak WebDAV. They speak OCS: a JSON or XML envelope carrying a `meta`
block whose `statuscode` is the real result, wrapped in an HTTP status that
may or may not agree with it.

Two details make this a silent-failure surface rather than ordinary HTTP.
Without the `OCS-APIRequest` header Nextcloud treats the call as a browser
navigation and answers a login page, which parses as neither success nor a
recognizable refusal. And without `format=json` the answer is XML, so a client
asking for JSON and not saying so reads a parse error instead of its data. Both
are sent here, once, so no caller can forget either.

The envelope is verified rather than trusted. A response whose `meta` is
missing, whose `statuscode` is not an integer, or whose `data` is absent is
malformed — not an empty result — because an empty result and an unparsed
answer differ by everything the caller would do next.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from . import exits
from .session import Session, SessionError

#: OCS v2 maps its own status codes onto HTTP ones, which v1 did not. Every
#: path here is v2 so that a transport-level refusal and an application-level
#: refusal do not have to be told apart by reading the body.
ROOT = "/ocs/v2.php"

#: Nextcloud answers a request without this header with a login page.
_REQUIRED_HEADERS = {
    "OCS-APIRequest": "true",
    "Accept": "application/json",
}

#: What each OCS status means in the caller's terms. The three-digit codes are
#: OCS's own legacy values, which Nextcloud still returns from some endpoints.
_STATUS_EXITS = {
    400: exits.USAGE,
    401: exits.CREDENTIAL_REJECTED,
    403: exits.SCOPE_DENIED,
    404: exits.TARGET_NOT_FOUND,
    405: exits.UNSUPPORTED_STRUCTURE,
    409: exits.CONFLICT,
    412: exits.CONFLICT,
    423: exits.LOCKED,
    429: exits.THROTTLED,
    996: exits.SERVER_ERROR,
    997: exits.CREDENTIAL_REJECTED,
    998: exits.TARGET_NOT_FOUND,
    999: exits.ERROR,
}

_SUCCESS = frozenset({100, 200})


class OcsError(RuntimeError):
    """An OCS call did not succeed, or did not answer in the OCS envelope."""

    def __init__(self, message: str, code: int = exits.MALFORMED_RESPONSE) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def path(*segments: str, query: dict[str, str] | None = None) -> str:
    """Build one OCS path, always asking for JSON.

    Segments are percent-encoded individually so a share id or an account name
    carrying a slash addresses one resource rather than escaping into the path.
    """
    encoded = "/".join(urllib.parse.quote(str(segment), safe="") for segment in segments)
    parameters = {"format": "json", **(query or {})}
    return f"{ROOT}/{encoded}?{urllib.parse.urlencode(parameters)}"


def _envelope(body: Any) -> tuple[int, str, Any]:
    if not isinstance(body, dict):
        raise OcsError("the OCS response was not an object")
    envelope = body.get("ocs")
    if not isinstance(envelope, dict):
        raise OcsError("the OCS response carried no ocs envelope")
    meta = envelope.get("meta")
    if not isinstance(meta, dict):
        raise OcsError("the OCS response carried no meta block")
    status = meta.get("statuscode")
    if not isinstance(status, int) or isinstance(status, bool):
        raise OcsError("the OCS response carried no numeric status code")
    message = meta.get("message")
    if "data" not in envelope:
        raise OcsError("the OCS response carried no data")
    return status, str(message or "").strip(), envelope["data"]


def request(
    profile: Any,
    *,
    session: Session,
    method: str,
    url: str,
    form: dict[str, Any] | None = None,
) -> Any:
    """Make one OCS call and return its `data`, or refuse with the server's reason.

    A form body is sent as `application/x-www-form-urlencoded`, which is what
    every OCS endpoint accepts and several accept exclusively.
    """
    headers = dict(_REQUIRED_HEADERS)
    body: bytes | None = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        body = urllib.parse.urlencode(
            {key: value for key, value in form.items() if value is not None}
        ).encode("utf-8")
    try:
        response = session.request(method, url, headers=headers, data=body)
    except SessionError as exc:
        raise OcsError(exc.message, exc.code) from exc

    content_type = (response.header("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type and content_type != "application/json":
        # The login page Nextcloud serves to a request without the header is
        # HTML, and reporting it as malformed JSON would send the caller
        # looking for a parse bug instead of a missing header.
        raise OcsError(
            f"the OCS endpoint answered {content_type} rather than JSON; it did not "
            "treat this as an API request"
        )
    try:
        parsed = response.json()
    except SessionError as exc:
        raise OcsError("the OCS response was not valid JSON", exc.code) from exc

    status, message, data = _envelope(parsed)
    if status in _SUCCESS:
        return data
    detail = f": {message}" if message else ""
    raise OcsError(
        f"the OCS endpoint refused with status {status}{detail}",
        _STATUS_EXITS.get(status, exits.MALFORMED_RESPONSE),
    )


def require_list(data: Any, *, label: str) -> list[Any]:
    if not isinstance(data, list):
        raise OcsError(f"the OCS endpoint did not return a list of {label}")
    return data


def require_object(data: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise OcsError(f"the OCS endpoint did not return one {label}")
    return data
