# Shares over OCS

Nextcloud's own APIs do not speak WebDAV. They speak OCS: a JSON envelope
whose `meta.statuscode` is the real result, wrapped in an HTTP status that may
or may not agree with it. `ncl.ocs` owns that envelope so no caller has to.

Two details make OCS a silent-failure surface rather than ordinary HTTP.
Without the `OCS-APIRequest` header Nextcloud treats the call as a browser
navigation and answers a login page, which parses as neither success nor a
recognizable refusal. Without `format=json` the answer is XML. Both are sent
once, centrally. An answer whose media type is not JSON is reported as the
endpoint not having treated the call as an API request, rather than as a parse
error, because that names the actual cause.

The envelope is verified rather than trusted: a response with no `meta`, no
integer `statuscode`, or no `data` is malformed. An empty result and an
unparsed answer differ by everything the caller does next.

## What a share is, here

A share is the only mutation in this tool that changes who *else* can reach a
resource. It is not observable from the resource it exposes — reading the file
afterwards looks identical whether or not the world can also read it — so the
reach a share grants is named in the plan preview rather than discovered
afterwards.

Shares are addressed by DAV href, checked against the profile's files
allowlist, exactly like every other resource. The OCS API speaks
account-relative paths, so the two are converted in one place; a share of a
path this profile may not reach is refused before any request is made, because
once a request is built there is no origin left in the path to check.

A resource under another account's files is refused even when the allowlist
admits the href: this API shares from the authenticated account's own tree.

## Reading

`ncl share list` reports every share this account has made. `ncl share list
<href>` scopes that to one resource, and `--subfiles` reports the shares inside
a collection rather than on it.

An unscoped listing deliberately reports shares over paths outside the
allowlist. The allowlist bounds what this tool may *reach*; a listing that hid
an existing public link because its path was unconfigured would answer "who can
see my files" with a reassuring lie. For the same reason a share type this tool
cannot create is still listed, and one it has no name for is labelled
`unsupported:<n>` rather than dropped.

Permissions are reported as names decoded from the bitmask OCS packs them into:
`read`, `update`, `create`, `delete`, `share`.

## Creating and revoking

`ncl share create <href>` takes exactly one of `--public`, `--user <uid>`, or
`--group <gid>`, and grants `read` unless `--permissions` says otherwise. It
freezes a plan and sends nothing. `ncl share delete <id>` freezes the
revocation of a share it has read first, so the preview says what is being
taken away.

A link password is read from a file with `--password-from`, never from an
argument: a password in `argv` is visible to every process on the host and
lands in shell history. It is frozen into the plan's private payload, and a
step whose payload is itself a secret withholds its length from every
caller-visible view as well — for a password the size is the one property worth
guessing from.

A server may grant more than it was asked for. Nextcloud adds the `share` bit
to every public link, so a plan that promised `read` produces a share with
`read, share`. Applying reports the difference as `granted_beyond_plan` rather
than passing over it: refusing it would refuse every public link this server
makes, and staying silent would leave the approval describing something that
did not happen.

Revoking a share that is already gone is the outcome the step wanted, and is
reported as such, so a resumed plan stays finishable.

Reconciling a creation compares the shares on the path against the ones that
existed when the plan was frozen. Exactly one new share settles it. Two cannot
be told apart, so reconciliation refuses to guess and says to revoke by id
after listing.

## Not modelled

Changing an existing share — its permissions, expiry, password, or note — is
not implemented; revoke and recreate. Shares received from other accounts are
not listed, only shares this account made. Federated, circle, email, and
conversation shares are read but never created.
