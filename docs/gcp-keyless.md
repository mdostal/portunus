# Keyless GCP access (WIF + service account)

Portunus can write to and read from GCP Secret Manager with no interactive `gcloud` login and
no service-account key file. Two pieces make that work:

1. **One least-privilege service account per project**, holding the only Secret Manager grants.
2. **An identity that can act as that service account without a human session.** The target is
   Workload Identity Federation (WIF): a short-lived OIDC token from the harness is exchanged at
   Google's STS, then for the service account's own access token via the IAM Credentials API.
   The interim option is a stable `gcloud` account (one with no org reauth policy) that
   impersonates the service account.

Either way, `portunus auth status --strict` tells you before a write whether the credential is
usable. `portunus gcp add-version` checks it automatically before every write.

## 1. Service account (once per project)

```bash
PROJECT=ffe-cicd
ADMIN=<an account with IAM admin on $PROJECT>
SA=portunus-secrets@$PROJECT.iam.gserviceaccount.com

gcloud iam service-accounts create portunus-secrets \
  --project=$PROJECT --display-name="Portunus secret writer" --account=$ADMIN

# Grant per secret, never project-wide. Only the secrets Portunus manages.
for SECRET in social-engine-prod-resend-api-key; do
  for ROLE in roles/secretmanager.secretVersionAdder roles/secretmanager.secretAccessor; do
    gcloud secrets add-iam-policy-binding "$SECRET" --project=$PROJECT \
      --member="serviceAccount:$SA" --role="$ROLE" --account=$ADMIN
  done
done
```

`secretVersionAdder` covers `gcp add-version` and `secretAccessor` covers reads. A binding with
`sync_mode=cached` also reads version metadata (`versions describe`), and `secretAccessor` does
not include that, so grant `roles/secretmanager.viewer` on those secrets too.

## 2a. WIF: fully keyless (target)

This needs an OIDC issuer the harness can get tokens from, and those tokens must carry a stable
`sub` claim. If Google can't fetch the issuer's discovery document, upload its JWKS with
`--jwk-json-path`.

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)' --account=$ADMIN)

gcloud iam workload-identity-pools create portunus \
  --project=$PROJECT --location=global --account=$ADMIN
gcloud iam workload-identity-pools providers create-oidc hive \
  --project=$PROJECT --location=global --workload-identity-pool=portunus \
  --issuer-uri=<ISSUER_URL> \
  --attribute-mapping="google.subject=assertion.sub" \
  --attribute-condition="assertion.sub == '<HARNESS_SUBJECT>'" \
  --account=$ADMIN            # add --jwk-json-path=jwks.json for a private issuer

# Let exactly that subject act as the service account.
gcloud iam service-accounts add-iam-policy-binding $SA --project=$PROJECT \
  --role=roles/iam.workloadIdentityUser \
  --member="principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/portunus/subject/<HARNESS_SUBJECT>" \
  --account=$ADMIN

portunus bindings set $PROJECT \
  --wif-audience "//iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/portunus/providers/hive" \
  --impersonate-service-account $SA
```

The harness supplies the OIDC token through `PORTUNUS_OIDC_TOKEN_FILE` (or
`PORTUNUS_OIDC_TOKEN`), plus `PORTUNUS_OIDC_SUBJECT` and `PORTUNUS_OIDC_EXPIRES_AT` as metadata.
By default the token's `aud` must be the provider's full resource name with an `https:` prefix
(`https://iam.googleapis.com/projects/.../providers/hive`), unless you set
`--allowed-audiences` on the provider. Check it:

```bash
portunus auth gcp --project ffe-cicd    # gcp:wif ok identity=... service_account=portunus-secrets@... expires_at=...
portunus auth status --strict           # ffe-cicd: wif -> portunus-secrets@... -- ready (keyless)
```

With a WIF binding, `gcloud` only ever gets `--access-token-file`. No gcloud account is used,
and the active account never matters.

## 2b. Interim: stable account + impersonation

Use this until an OIDC issuer exists on a host. Choose an account that is not subject to a
Workspace reauth policy, and let it mint tokens for the service account:

```bash
gcloud iam service-accounts add-iam-policy-binding $SA --project=$PROJECT \
  --role=roles/iam.serviceAccountTokenCreator \
  --member="user:<stable-account>" --account=$ADMIN

portunus bindings set $PROJECT --account <stable-account> --impersonate-service-account $SA
```

This account needs no Secret Manager grants of its own. That is why an account that gets
`IAM_PERMISSION_DENIED` on a secret can still write it, as the service account.

## Per-command `--account`: never change the active account

Every Portunus GCP call for a bound project passes `--account=` (and
`--impersonate-service-account=`) explicitly, so `gcloud config set account` is never needed.
Other projects that rely on the current active account are unaffected. When a binding's session
expires, re-authenticate it without activating it:

```bash
gcloud auth login <account> --no-activate
```

`portunus auth status` prints that exact command next to any binding it reports as `EXPIRED`.

## Writing a version

```bash
printf '%s' "$NEW_VALUE" | portunus gcp add-version ffe-cicd social-engine-prod-resend-api-key --stdin
portunus gcp add-version ffe-cicd social-engine-prod-resend-api-key --value-file ./new-value.txt
```

The value goes to `gcloud secrets versions add --data-file=-` on stdin. It never appears in argv,
output, or the audit log. A failed preflight stops the write before anything is sent.
