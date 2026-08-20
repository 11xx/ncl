from __future__ import annotations

import pytest

from ncl import caldav, exits
from ncl.config import Profile

PROFILE = Profile(
    "home",
    "https://cloud.example.invalid",
    "pass",
    ("/remote.php/dav/calendars/alice/work/",),
    ("/remote.php/dav/files/alice/",),
)

HOME = "https://cloud.example.invalid/remote.php/dav/calendars/alice/"

MULTISTATUS = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/work/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Work</d:displayname>
      <d:current-user-privilege-set>
        <d:privilege><d:read/></d:privilege><d:privilege><d:write/></d:privilege>
      </d:current-user-privilege-set>
      <c:supported-calendar-component-set>
        <c:comp name="VEVENT"/>
      </c:supported-calendar-component-set>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/shared/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Work</d:displayname>
      <d:current-user-privilege-set><d:privilege><d:read/></d:privilege></d:current-user-privilege-set>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/contacts/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/alice/hidden/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
      <d:displayname>Hidden</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
    <d:propstat><d:prop><d:current-user-privilege-set/></d:prop>
      <d:status>HTTP/1.1 403 Forbidden</d:status></d:propstat>
  </d:response>
</d:multistatus>
"""


class FakeSession:
    def __init__(self, status=207, body=MULTISTATUS):
        self.status = status
        self.body = body
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, data=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers, "data": data})

        class R:
            pass

        response = R()
        response.status = self.status
        response.body = self.body
        return response


def _listing(**kwargs):
    session = FakeSession(**kwargs)
    return caldav.list_calendars(PROFILE, session=session, calendar_home=HOME), session


def test_listing_requests_depth_one_propfind_on_the_home():
    _, session = _listing()
    assert session.requests[0]["method"] == "PROPFIND"
    assert session.requests[0]["url"] == HOME
    assert session.requests[0]["headers"]["Depth"] == "1"


def test_only_calendar_collections_are_returned_and_the_home_is_not():
    calendars, _ = _listing()
    hrefs = [calendar.href for calendar in calendars]
    assert HOME not in hrefs
    assert not any(href.endswith("/contacts/") for href in hrefs)
    assert len(calendars) == 3


def test_scope_reports_the_allowlist_decision_without_hiding_anything():
    calendars, _ = _listing()
    scoped = {calendar.href.rsplit("/", 2)[-2]: calendar.in_scope for calendar in calendars}
    assert scoped == {"work": True, "shared": False, "hidden": False}


def test_a_calendar_without_write_privilege_is_read_only():
    calendars, _ = _listing()
    by_name = {calendar.href.rsplit("/", 2)[-2]: calendar for calendar in calendars}
    assert by_name["work"].read_only is False
    assert by_name["shared"].read_only is True


def test_a_privilege_set_returned_inside_a_403_propstat_is_absent_not_empty():
    """A forbidden property must not read as 'no privileges listed, assume writable'.

    The response returns current-user-privilege-set inside a 403 propstat. Read
    without checking the propstat status it looks like an empty privilege set;
    the safe reading is that the server did not say, so writing is not allowed.
    """
    calendars, _ = _listing()
    by_name = {calendar.href.rsplit("/", 2)[-2]: calendar for calendar in calendars}
    assert by_name["hidden"].read_only is True


def test_components_are_read_from_the_supported_set():
    calendars, _ = _listing()
    by_name = {calendar.href.rsplit("/", 2)[-2]: calendar for calendar in calendars}
    assert by_name["work"].components == ("VEVENT",)


def test_resolve_by_href_suffix_and_by_unique_display_name():
    calendars, _ = _listing()
    assert caldav.resolve(calendars, "/remote.php/dav/calendars/alice/work/").href.endswith(
        "/work/"
    )
    assert caldav.resolve(calendars, "Hidden").href.endswith("/hidden/")


def test_resolve_short_name_uses_the_exact_final_path_segment():
    calendars = [
        caldav.Calendar(
            href=HOME + "work/",
            display_name="Work",
            components=("VEVENT",),
            read_only=False,
            in_scope=True,
        ),
        caldav.Calendar(
            href=HOME + "subwork/",
            display_name="Subwork",
            components=("VEVENT",),
            read_only=False,
            in_scope=True,
        ),
    ]

    assert caldav.resolve(calendars, "work").href.endswith("/work/")
    with pytest.raises(caldav.CalendarError) as error:
        caldav.resolve([calendars[1]], "work")
    assert error.value.code == exits.TARGET_NOT_FOUND


def test_resolve_refuses_an_ambiguous_display_name():
    """Two calendars named 'Work' must not resolve to whichever came first."""
    calendars, _ = _listing()
    with pytest.raises(caldav.CalendarError) as error:
        caldav.resolve(calendars, "Work")
    assert error.value.code == exits.AMBIGUOUS_TARGET
    assert "/work/" in str(error.value) and "/shared/" in str(error.value)


def test_resolve_reports_a_missing_target():
    calendars, _ = _listing()
    with pytest.raises(caldav.CalendarError) as error:
        caldav.resolve(calendars, "Nope")
    assert error.value.code == exits.TARGET_NOT_FOUND


def test_a_non_multistatus_answer_is_refused():
    with pytest.raises(caldav.CalendarError) as error:
        _listing(status=200)
    assert error.value.code == exits.MALFORMED_RESPONSE
