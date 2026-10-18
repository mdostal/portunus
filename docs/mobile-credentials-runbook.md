# Mobile Signing & Push Credentials Runbook

Apple and Android app credentials are all **manual**. Apple has no public API that issues or
revokes any of them. An Android keystore is a file you generate yourself, so there's no provider
to call. Portunus has no rotation adapter for any of these, and none should be built
([provider-rotation-matrix.md](provider-rotation-matrix.md#apple-push-notification-service-apns-auth-key)).

| Credential | Vault references | Liveness | Rotate when |
|---|---|---|---|
| [APNs auth key](#apns-auth-key-p8) | `apns-authkey-B9FVCQ4GB4`, `apns-authkey-U22RLFH997`, `apns-authkey-unknown-jan9`, `shindig-apns-authkey-p8` | **yes**, `apns` probe | compromised, or `liveness` reads it `dead` |
| [App Store Connect API key](#app-store-connect-api-key) | `orchestration-prod-apple-asc-api-key-p8`, `shindig-prod-asc-{key-base64,key-id,issuer-id}` | no | compromised, or the person who created it leaves |
| [Apple code-signing certificate](#apple-code-signing-certificate-and-provisioning-profile) | `apple-developer-cert-cer`, `shindig-ios-cert-*-{cer,p12}`, `shindig-distribution-cert`, `shindig-appstore-profile` | no | before it expires (yearly), or compromised |
| [Android signing keystore](#android-signing-keystore) | `shindig-android-keystore-{base64,password}`, `shindig-dev-keystore-{base64,password}` | no | compromised only. Never on a schedule. |

Only the APNs key gets a liveness probe. It's the only one with a network protocol that can be
checked without side effects. Liveness checks for the other three were deliberately left out
(PANT-1127):

- **ASC API key**: App Store Connect does have an authenticated API, but it isn't probed here.
- **Certificates and profiles**: the real failure is expiry, and that's a date inside the file,
  not something a server tells you. Put the expiry date in a calendar when you install one.
- **Android keystore**: nothing on Google's side can say whether a stored upload key still
  matches Play without making a release. Checking it the wrong way could itself turn into a
  support ticket.

These references live in GCP Secret Manager (`ffe-cicd`). Portunus can't write a new value to a
GCP backend, so storing a new value is a human `gcloud` call.

## Shared procedure

1. Create the new credential (per type, below). Keep the old one working.
2. Write the new value to a file with mode 0600. Don't paste it into a shell argument, an agent
   chat or a PR. Binary files (`.p12`, `.jks`) are stored base64-encoded: `base64 -w0 in > out`.
3. Add it as a new Secret Manager version. The old version stays as a rollback:
   `gcloud secrets versions add <sm_name> --project=ffe-cicd --data-file=<file>`, then
   `shred -u <file>`.
4. Point the consumer at it: redeploy the push server, or re-run the CI signing job that reads
   it, and confirm it works (a push arrives, a build signs and uploads). This repo doesn't
   document which services read these secrets. Check the consuming repo's deploy and CI config.
5. Only then revoke the old credential with the provider, and disable the previous version:
   `gcloud secrets versions disable <old-version> --secret=<sm_name> --project=ffe-cicd`.

---

## APNs auth key (`.p8`)

What it is: an ES256 private key plus a 10-character key ID. A push server signs a short-lived
JWT with it (`kid` = key ID, `iss` = 10-character Team ID). The key doesn't expire. It stops
working only when someone revokes it in the developer portal, and then every push server
using it fails at once.

### Liveness

```sh
# once per reference: the probe needs the key ID and team ID, which the .p8 doesn't contain.
# retag --tags replaces the whole tags dict, so include any existing tags.
portunus retag apns-authkey-B9FVCQ4GB4 --tags liveness_provider=apns,apns_team_id=<TEAMID>
portunus retag shindig-apns-authkey-p8 \
    --tags liveness_provider=apns,apns_team_id=<TEAMID>,apns_key_id=<KEYID>,apns_topic=<bundle id>

portunus liveness check apns-authkey-B9FVCQ4GB4
```

The probe needs the optional `h2` package (`pip install 'pantheon-portunus[apns]'`), because
APNs only speaks HTTP/2. How it works and why it's safe: [liveness.md](liveness.md#apns-auth-keys).

- `live`: APNs accepted the key.
- `dead` (`403 InvalidProviderToken`): APNs doesn't accept the key for that key ID and team ID.
  Before re-issuing, check that `apns_team_id` and the key ID match the portal's *Keys* page.
  A wrong tag reads exactly like a revoked key.
- `unknown` with `BadEnvironmentKeyIdInToken`: the key is limited to the other environment. Set
  `apns_env=production` (or `sandbox`).

`apns-authkey-unknown-jan9` has no key ID in its name. Find it on the portal's *Keys* page (key
names and creation dates are listed there) and set `apns_key_id`. If no key there matches, the key
was revoked or belongs to another team. Confirm nothing reads it, then retire it with
`portunus state apns-authkey-unknown-jan9 revoked`.

### Re-issue

1. developer.apple.com > Certificates, Identifiers & Profiles > *Keys* > **+**. Enable *Apple Push
   Notifications service (APNs)*. Apple now asks for the environment (Sandbox or Production) and
   whether the key is team-scoped or topic-specific. Match the old key, or make one key per
   environment, which is Apple's recommendation. A team can have at most **two team-scoped
   keys per environment**. If you're at the limit, revoke an unused key first (check with
   liveness that it really is unused).
2. Download the `.p8`. **Apple only lets you download it once.** Note the key ID.
3. Store it (shared steps 2–3) under a reference named `apns-authkey-<KEYID>`, so the probe reads
   the key ID from the name. Tag it as above, and run `portunus liveness check` until it reads
   `live`.
4. Switch the push server to the new key ID and key, and send a real push to a test device.
5. Revoke the old key on the *Keys* page, and retire its reference.

Source: Apple, [Establishing a token-based connection to APNs](https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns).
Checked 2026-10-18.

---

## App Store Connect API key

What it is: a `.p8` private key, a key ID and the team's issuer ID. CI uses them to upload
builds and manage TestFlight (`shindig-prod-asc-*`, `orchestration-prod-apple-asc-api-key-p8`).
The issuer ID is the same for every key on the team and isn't secret. It doesn't change when the
key does.

1. App Store Connect > *Users and Access* > *Integrations* > *App Store Connect API* > *Team Keys* >
   **Generate API Key**. You need the Account Holder or Admin role. Give it the same access role as
   the old key. A key's name and role can't be edited afterwards.
2. Download the `.p8` (**only downloadable once**) and note the key ID.
3. Store the `.p8` (base64-encoded for `shindig-prod-asc-key-base64`) and the key ID together.
   They only work as a pair. Re-run the CI job that uploads a build.
4. Revoke the old key on the same page. Revocation takes effect immediately.

Team keys apply to every app in the team. If one of these was an *individual* key, its creator
leaving revokes it. Replace it with a team key.

Source: Apple, [Creating API keys for App Store Connect API](https://developer.apple.com/documentation/appstoreconnectapi/creating-api-keys-for-app-store-connect-api).
Checked 2026-10-18.

---

## Apple code-signing certificate and provisioning profile

What it is: an X.509 certificate Apple issues for a private key you generated (`.cer` = the
certificate alone, `.p12` = certificate plus private key, password-protected). An App Store
provisioning profile (`shindig-appstore-profile`) names the certificate it allows, so a new
certificate needs a new profile too.

These expire. Renew before the expiry date, not after a build breaks. To read the date from a
stored `.cer`, decode it in a scratch directory and run
`openssl x509 -inform der -noout -enddate -in cert.cer`.

1. Make a private key and CSR on a trusted machine (Keychain Access > Certificate Assistant >
   *Request a Certificate From a Certificate Authority*, or `openssl req -new -newkey rsa:2048`).
2. developer.apple.com > *Certificates* > **+** > *Apple Distribution* (or *Apple Development*).
   Upload the CSR and download the `.cer`. Apple caps how many distribution certificates a team
   can hold. If you're at the cap, revoke an expired one first.
3. Build the `.p12` from the private key and the `.cer`, with a new password.
4. *Profiles* > edit the App Store profile, select the new certificate, then save and download it.
5. Store the `.cer`, the `.p12`, the profile and the `.p12` password (shared steps 2–3), and make a
   signed build with CI.
6. Revoke the old certificate. Apps already on the App Store keep working. Profiles that named
   the old certificate stop working, which is why step 4 comes first.

Source: Apple, [Certificates](https://developer.apple.com/support/certificates/). Not verified
against a logged-in account: the exact distribution-certificate cap.

---

## Android signing keystore

What it is: a `.jks` file holding the key that signs the app, plus its store and key passwords.
On Play, it's either the **upload key** (Play App Signing is on: Google holds the real app
signing key and re-signs uploads) or the **app signing key** itself (legacy apps).

!!! danger "Don't rotate this on a schedule"
    The keystore never expires on its own, and replacing it isn't routine. If an upload key
    can't be replaced, no new release can be published, so only rotate it when it's lost or
    compromised.

- **Upload key (Play App Signing on)**, which is the normal case:
    1. Generate a new upload keystore on a trusted machine:
       `keytool -genkeypair -v -keystore upload.jks -alias upload -keyalg RSA -keysize 2048 -validity 10000`.
    2. Export its certificate: `keytool -export -rfc -keystore upload.jks -alias upload -file upload_certificate.pem`.
    3. Play Console > the app > *Play App Signing* settings > **Request upload key reset**, and
       submit the `.pem`. You need the account owner or Admin. The new key is accepted only after
       Google processes the request. Third-party reports range from hours to about a week, and
       there's no official figure. Until then, keep releasing with the old key if it's still
       usable.
    4. After Google confirms, store the keystore (base64) and the passwords (shared steps 2–3),
       then release a build.
- **App signing key (no Play App Signing)**: losing it means the app can't be updated. Don't
  generate a replacement. Turn on Play App Signing (uploading the existing key), or use Play's
  app signing key upgrade, which applies to new installs only. Both are one-off, attended
  operations. Follow Google's current *Play App Signing* help page rather than this runbook.
- **`shindig-dev-keystore-*`** (dev/internal builds): regenerate it freely. A device that has a
  build signed with the old key must uninstall it before installing one signed with the new key.

Source: Android Developers, [Sign your app](https://developer.android.com/studio/publish/app-signing).
Not verified (needs Play Console access): the current menu path for the upload-key reset, and
which mode `shindig` uses.
