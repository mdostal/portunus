# Lessons learned: the portunus-oauth-token-broker live proof

Written after Story 04's live proof against two real Google accounts (personal +
Firefly Events). Four real things happened during that proof that are worth recording in one
place, not scattered across commit messages and `docs/architecture.md` §20's own summary. None
of these involved committing a secret to the repo — confirmed directly by grepping the full
commit history for this epic; only test-fixture placeholder strings ("CLIENT-SECRET-do-not-leak"
etc.) ever appear.

## 1. `gcloud auth login` and `gcloud auth application-default login` are two different credential stores

Easy to conflate, genuinely different underneath:

- **`gcloud auth login <email>`** authenticates the `gcloud` CLI *itself*. The resulting tokens
  live in an internal SQLite database (`~/.config/gcloud/credentials.db`), read only by `gcloud`
  commands. This is what Portunus's existing `portunus auth login` already wraps
  (`GcloudBackend` shells out to `gcloud`, so it never needs the raw credential).
- **`gcloud auth application-default login`** authenticates **Application Default Credentials**
  — a separate, portable JSON file
  (`~/.config/gcloud/application_default_credentials.json`) containing a plain
  `client_id`/`client_secret`/`refresh_token`. Designed to be read directly by *any* code, not
  just `gcloud` — which is exactly why it's the right source for `portunus oauth store`: the
  broker does the OAuth refresh grant itself via a direct HTTPS call, generalized across
  providers, never shelling out to `gcloud`.

Running the first command when the second is needed produces no error — it just authenticates
the wrong thing, silently. First real confusion of the proof: an initial `gcloud auth login` was
run, which updated the CLI's own store but left the ADC file untouched (confirmed by checking
its mtime — still Feb 2024, unchanged). Documented in README.md's OAuth section so the next
person doesn't lose the same few minutes.

## 2. Don't trust a project-name/quota-project hint to infer which Google account authenticated

`gcloud auth application-default login`'s own success output only ever names a *quota project*
(from the CLI's active `gcloud config`, if one is set) — it does **not** print which Google
account was actually picked in the browser consent screen. During this proof, a credential was
initially labeled `"personal"` based on an assumption from the quota-project warning text
(`personalsites-487021`, a project name that sounds personal) — wrong. The account was actually
Firefly Events (`@ff.events`); the quota-project hint was unrelated to which account got
consent.

**The reliable fix**: mint an access token from the stored credential, then call Google's own
`tokeninfo` endpoint (`https://oauth2.googleapis.com/tokeninfo?access_token=...`) — its response
includes the actual `email` the token belongs to. This is the only way to *know*, not guess,
which account a bootstrapped ADC credential represents. Worth surfacing as a real, reusable
verification technique for anyone setting up multiple accounts under one Portunus vault — guess
the label, then verify it with `tokeninfo` before trusting it.

## 3. A stock python.org macOS install can't verify TLS certs out of the box

The very first mint attempt failed with `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify
failed: unable to get local issuer certificate`. Confirmed this was environmental, not a
Portunus/code bug, by testing `curl` against the same endpoint (worked fine — the system trust
store is intact) while Python's own `urllib` failed (its default `ssl.get_default_verify_paths()`
pointed at an empty/non-existent `cert.pem` under the Python.framework install). This is a
well-known python.org installer gap: the interpreter doesn't automatically inherit the macOS
system CA trust store, and its own `Install Certificates.command` (shipped alongside the
installer, under `/Applications/Python <version>/`) is needed once to populate it — or point
`SSL_CERT_FILE` at `certifi`'s bundle (`python3 -c "import certifi; print(certifi.where())"`) as
a lighter, install-scoped fix, which is what unblocked this proof. Documented as a README
troubleshooting note rather than worked around in Portunus's own code, since it's a prerequisite
for *any* Python HTTPS client on an affected install, not something specific to this feature.

## 4. A boolean-comparison bug briefly printed a live access token

The real mistake of this proof. A verification script meant to report *whether* two minted
tokens were different wrote:

```python
print("the two tokens are genuinely different values:", v1 != v2 and v1 and v2)
```

In Python, `and` returns the last truthy operand, not a boolean — `v1 != v2 and v1 and v2`
evaluates to `v2` (the raw token string) whenever the comparison is true, not `True`. The
intended boolean check silently became "print the token." It printed directly into this
session's own terminal output.

**What limited the damage**: it was a short-lived (~1 hour) OAuth *access* token for the
`cloud-platform` scope — not the refresh token, not the client secret, neither of which was ever
printed. It expired on its own well before this write-up.

**What happened next, deliberately**: caught immediately on re-reading the output; disclosed to
the user in the same turn, without minimizing it or waiting to be asked; the resolved tempfile
holding the value was located and deleted; the check was rewritten to compare `hashlib.sha256`
digests of the two values instead of ever comparing or printing them raw:

```python
def digest(path):
    with open(path) as f:
        return hashlib.sha256(f.read().encode()).hexdigest()[:12]

print("tokens are distinct:", bool(digest(p1) != digest(p2)))
```

**The general lesson**: any ad-hoc verification script that touches a real secret value —
even one written to prove a *security* mechanism works — needs the same "never print/return the
raw value" discipline as the production code it's testing. A quick diagnostic one-liner is not
exempt from the boundary-safety invariant just because it's disposable; if anything, it deserves
more scrutiny, since it's exactly the kind of code that gets written fast and reviewed less.
