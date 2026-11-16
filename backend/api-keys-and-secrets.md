---
title: API keys and secrets management
section: backend
order: 53
phase: "Phase 7 · Security"
summary: Issue, hash, scope, rotate, revoke and detect leaked API keys, and keep your own secrets in vaults with short-lived credentials instead of env files.
---

Two related problems share this note. **API keys** are credentials you issue to customers so their servers can call your API: they must be unguessable, stored so a database leak doesn't expose them, scoped, rotatable and easy to detect when leaked. **Secrets management** is the reverse: the credentials your own services hold (database passwords, third-party API keys, signing keys, TLS private keys) and how they get to the process without ending up in Git, logs or a container image. Both fail the same way: a long-lived string copied into too many places. Passwords are a different problem with different rules (see [Password hashing]({{ '/backend/password-hashing/' | relative_url }})).

## API key management

API keys identify an application or integration, not an interactive user. They suit server-to-server calls from customer backends, CI jobs and scripts, where an OAuth flow would be overkill.

| | API key | OAuth access token | Session cookie |
|---|---|---|---|
| Represents | An integration or account | A client acting for a user (or itself) | A logged-in browser user |
| Lifetime | Months, until rotated | Minutes | Hours to days |
| Issued by | Your dashboard or API | Authorization server | Login |
| Best for | Simple server-to-server | Third-party apps, delegated access | First-party web apps |

If third parties act on behalf of your users, use OAuth (see [OAuth 2.0 and OpenID Connect]({{ '/backend/oauth2-and-oidc/' | relative_url }})). If a customer's own backend calls your API, API keys are fine.

### Generation

- Use a CSPRNG (`secrets` in Python), never `random`, timestamps, user IDs or `uuid1`.
- At least 128 bits of entropy; 30 base62 characters give about 178 bits.
- Show the full key once at creation. After that, the dashboard shows only a prefix and the last four characters.

### Prefixing and format

A structured format makes keys identifiable by humans and machines:

```text
  acme_live_4fT9kQ2mWb7Lx0PzRc5Nd8Ys1HvJ3e  M3k9Qz
  |    |    |                               |
  |    |    30 random base62 chars          6-char CRC32 checksum
  |    environment: live or test
  product prefix, so scanners can find it
```

- **Prefix.** Tells a support engineer, a log scrubber and a secret scanner what the string is. Stripe's `sk_live_` / `sk_test_` / `pk_live_` and GitHub's `ghp_` / `gho_` / `ghs_` are the well-known public examples.
- **Environment.** `test` keys never touch production data, and a `live` key in a test config is easy to spot.
- **Checksum.** GitHub publicly described adding a CRC32 checksum to its tokens in 2021. It lets you reject typos and random strings without a database lookup, and lets scanners validate candidates offline with near-zero false positives.
- **Characters.** Base62 with `_` separators is URL-safe and selects with a double-click.

### Hashing at rest

Store a hash of the key, never the key. If the `api_keys` table leaks, attackers get nothing usable.

- **A fast hash is correct here.** SHA-256 (or HMAC-SHA256 with a server-side key) is enough because a 178-bit random key can't be brute-forced at any hash speed. bcrypt or Argon2 would add tens of milliseconds to every API request for no benefit.
- **Lookup.** Either index the hash (`WHERE key_hash = sha256(presented)`) or split the key into a public `key_id` and a secret part: look up by `key_id`, then `hmac.compare_digest` the secret's hash. The split version lets logs carry the `key_id` safely.
- **Keep metadata, not secrets:** tenant, scopes, created by, created at, expires at, revoked at, last used at, display prefix.

### Scopes and restrictions

- **Scopes** per key (`orders:read`, `webhooks:write`), so a reporting script's key can't issue refunds. Stripe's restricted keys are a public example.
- **Tenant binding.** The key belongs to one tenant; the tenant comes from the key record, never from a request parameter.
- **Optional restrictions:** IP allowlists, expiry dates, per-key rate limits and quotas (see [Rate limiting]({{ '/backend/rate-limiting/' | relative_url }})).
- **Publishable vs secret keys.** If a key must ship in a browser or mobile app, it is public by definition. Give it a separate type that can only do harmless things (create a payment intent, not read customers).
- Accept keys only in a header (`Authorization: Bearer ...` or `X-API-Key`), never in the query string, where they end up in access logs and browser history.

### Key rotation

Rotation must not need downtime, which means allowing at least two active keys per integration:

1. Customer creates a new key.
2. They deploy it everywhere.
3. They watch `last_used_at` on the old key stop moving.
4. They revoke the old key, or it expires after a grace period.

Offer an explicit "roll key" action that issues a replacement and schedules the old key to expire (Stripe and others offer an expiry window for rolled keys). Track `last_used_at` (sampled or batched, not a write per request) so customers can find unused keys, and email before expiry dates.

### Revocation

Revocation should take effect within seconds. If you cache key lookups in Redis or in process memory to save a database hit, a revoked key keeps working for the cache TTL. Either keep the TTL short (30 to 60 seconds), or publish revocations on a channel that evicts cache entries. Log every create, roll and revoke in an audit log with the actor.

### Leak detection

Keys leak through public Git repositories, CI logs, Slack messages, mobile app bundles and support tickets.

- **Secret scanning.** GitHub scans public repositories for known token formats and, through its secret scanning partner program, notifies the issuing provider, which can revoke automatically. Push protection blocks commits containing secrets. If you're a provider, a distinctive prefix plus checksum is what makes your keys detectable.
- **Your own pipeline.** Run a scanner such as gitleaks or trufflehog in CI and pre-commit hooks, and scrub your own key pattern from logs and error reports.
- **Canary keys.** Plant fake keys in places attackers look (old repos, internal wikis) and alert on any use.
- **Response.** Revoke or roll the key, notify the customer, then use logs keyed by key ID to find what the key did since the leak.

## Secrets management

### Where secrets live and env var risks

| Option | Risk | Verdict |
|---|---|---|
| Hard-coded or committed `.env` | Lives in Git history forever | Never |
| Baked into a Docker image | Anyone who can pull the image has it | Never |
| Environment variables | Inherited by child processes, visible in `/proc/<pid>/environ`, `docker inspect`, debug pages, crash reports | Common and acceptable when injected at runtime; know the leaks |
| Mounted files (tmpfs) | Readable by the process user | Better; can be rotated in place |
| Fetched from a secrets manager at startup | Needs an identity to authenticate | Best, with workload identity |

The Twelve-Factor App popularized config in environment variables, which beats config in code, but env vars leak in ways files don't: a `printenv` in a CI step, Sentry capturing the environment, Django's debug page in a misconfigured staging deploy. Scrub them in error reporting, and prefer files or direct fetches for high-value secrets.

### Vaults and KMS

Two different products that are often confused:

| | Secrets manager / vault | KMS / HSM |
|---|---|---|
| Stores | Secret values you get back (passwords, API keys) | Keys that never leave the service |
| Operations | Get, put, version, rotate | Encrypt, decrypt, sign, generate data keys |
| Examples | HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault secrets | AWS KMS, Cloud KMS, Azure Key Vault keys, CloudHSM |

HashiCorp Vault adds features beyond storage: **dynamic secrets** (it creates a database user on request and drops it when the lease expires), leases and renewal, audit logging of every read, a transit engine for encryption as a service, and many auth methods (Kubernetes service accounts, cloud IAM, OIDC). KMS is the root of envelope encryption (see [Encryption at rest and in transit]({{ '/backend/encryption/' | relative_url }})).

Services authenticate to the vault with a platform identity (a Kubernetes service account token, an IAM role), so there is no "secret zero" to distribute.

### Secret rotation

Rotating a secret you consume follows the same two-phase rule as API keys:

1. Create the new credential while the old one still works (a second database user or a second password slot).
2. Update the secret store; consumers pick it up by re-reading on a timer or on authentication failure.
3. Verify nothing uses the old credential, then revoke it.

AWS Secrets Manager's managed rotation implements this for databases, including an alternating-users strategy that flips between two database users so one is always valid. Applications that read a secret once at boot and cache it forever turn every rotation into a restart, so design for reload.

### Short-lived credentials

The best secret is one that expires before anyone can misuse it.

- **Dynamic database credentials** from Vault, valid for hours, unique per pod, so the audit log shows which instance did what.
- **Cloud workload identity:** IAM roles for EC2/ECS, IRSA or EKS Pod Identity, GKE Workload Identity. The SDK fetches short-lived credentials automatically; no access keys exist.
- **CI federation with OIDC:** GitHub Actions, GitLab and others issue a signed OIDC token per job, which the cloud exchanges for a short-lived role session. No cloud keys stored in CI secrets.
- **SPIFFE/SPIRE** or a service mesh issuing short-lived X.509 certificates for service-to-service mTLS.

Illustrative GitHub Actions job assuming an AWS role via OIDC:

```yaml
permissions:
  id-token: write   # allow the job to request an OIDC token
  contents: read
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789012:role/deploy-billing
          aws-region: eu-west-1
      - run: aws s3 sync ./dist s3://billing-assets
```

The IAM role's trust policy restricts which repository and branch may assume it.

### Kubernetes secrets caveats

Kubernetes `Secret` objects are convenient and widely misunderstood (see [Kubernetes]({{ '/backend/kubernetes/' | relative_url }})):

- **Base64 is encoding, not encryption.** Anyone who can read the object reads the value.
- **etcd stores them in plaintext unless you configure encryption at rest** (an `EncryptionConfiguration` with a KMS provider). Managed services offer KMS envelope encryption; check that it is on.
- **RBAC is broader than it looks.** `list` or `watch` on secrets returns their values, and anyone who can create a Pod in a namespace can mount any secret in that namespace.
- **Env vars from secrets don't update.** Pods must restart to see a rotated value; mounted volumes update in place (eventually), but the app must re-read the file.
- **Don't commit Secret manifests.** Use the External Secrets Operator (syncs from a cloud secrets manager), the Secrets Store CSI Driver (mounts directly from the vault), or Sealed Secrets / SOPS (encrypted in Git).

Illustrative External Secrets Operator resource (API version varies by release):

```yaml
apiVersion: external-secrets.io/v1   # v1beta1 on older releases
kind: ExternalSecret
metadata:
  name: billing-db
  namespace: billing
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: aws-secrets-manager
  target:
    name: billing-db            # Kubernetes Secret it creates and updates
  data:
    - secretKey: DATABASE_PASSWORD
      remoteRef:
        key: prod/billing/db
        property: password
```

## In practice

### Issue, verify, rotate, revoke and scan, runnable

Prefixed keys with a CRC32 checksum, SHA-256 at rest, scopes, expiry, rotation with a grace period, revocation, and a leak scanner. Standard library only.

```python
import hashlib
import re
import secrets
import string
import time
import zlib
from dataclasses import dataclass

BASE62 = string.digits + string.ascii_letters
KEY_RE = re.compile(r"acme_(live|test)_([0-9A-Za-z]{30})([0-9A-Za-z]{6})")


def base62(n: int, width: int) -> str:
    out = ""
    for _ in range(width):
        n, rem = divmod(n, 62)
        out = BASE62[rem] + out
    return out


def checksum(body: str) -> str:
    return base62(zlib.crc32(body.encode()), 6)


def well_formed(key: str) -> bool:
    m = KEY_RE.fullmatch(key)
    return bool(m) and checksum(m.group(2)) == m.group(3)


@dataclass
class KeyRecord:
    tenant_id: int
    scopes: frozenset
    display: str
    expires_at: float | None = None
    revoked_at: float | None = None
    last_used_at: float | None = None


class KeyStore:
    def __init__(self):
        self.rows = {}  # sha256(key) -> KeyRecord; a table with a unique index

    @staticmethod
    def digest(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def issue(self, tenant_id, scopes, ttl=None, env="live", now=None):
        now = now or time.time()
        body = "".join(secrets.choice(BASE62) for _ in range(30))
        key = f"acme_{env}_{body}{checksum(body)}"
        self.rows[self.digest(key)] = KeyRecord(
            tenant_id, frozenset(scopes), key[:14] + "..." + key[-4:],
            expires_at=now + ttl if ttl else None)
        return self.digest(key), key  # (row id, plaintext shown once)

    def verify(self, key, scope, now=None):
        """Returns (status, record); status maps to 200 / 401 / 403."""
        now = now or time.time()
        if not well_formed(key):
            return 401, None  # cheap reject, no database hit
        rec = self.rows.get(self.digest(key))
        if rec is None or rec.revoked_at:
            return 401, None
        if rec.expires_at is not None and rec.expires_at <= now:
            return 401, None
        if scope not in rec.scopes:
            return 403, rec
        rec.last_used_at = now  # batch these writes in production
        return 200, rec

    def roll(self, row_id, grace=7 * 86400, now=None):
        now = now or time.time()
        old = self.rows[row_id]
        new_id, new_key = self.issue(old.tenant_id, old.scopes, now=now)
        old.expires_at = min(old.expires_at or float("inf"), now + grace)
        return new_id, new_key

    def revoke(self, row_id, now=None):
        self.rows[row_id].revoked_at = now or time.time()


def scan_for_leaks(text: str):
    return [m.group(0) for m in KEY_RE.finditer(text) if well_formed(m.group(0))]


if __name__ == "__main__":
    store = KeyStore()
    t0 = 1_700_000_000.0
    kid, key = store.issue(42, {"orders:read"}, now=t0)
    assert well_formed(key) and key.startswith("acme_live_") and len(key) == 46
    assert key not in repr(store.rows)  # only the hash and a display hint

    assert store.verify(key, "orders:read", now=t0)[0] == 200
    assert store.verify(key, "refunds:write", now=t0)[0] == 403
    flipped = key[:20] + ("A" if key[20] != "A" else "B") + key[21:]
    assert not well_formed(flipped)  # typo caught by checksum alone
    assert store.verify("acme_live_" + "x" * 36, "orders:read")[0] == 401

    _, short = store.issue(42, {"orders:read"}, ttl=3600, now=t0)
    assert store.verify(short, "orders:read", now=t0 + 3599)[0] == 200
    assert store.verify(short, "orders:read", now=t0 + 3600)[0] == 401

    new_id, new_key = store.roll(kid, grace=86400, now=t0)
    assert store.verify(key, "orders:read", now=t0 + 3600)[0] == 200  # grace
    assert store.verify(key, "orders:read", now=t0 + 86400)[0] == 401
    assert store.verify(new_key, "orders:read", now=t0 + 86400)[0] == 200

    log_line = f"POST /v1/orders auth=Bearer {new_key} ua=curl/8.5"
    noise = "acme_live_" + "a" * 36  # right shape, wrong checksum
    leaked = scan_for_leaks(log_line + " " + noise)
    assert leaked == [new_key]
    store.revoke(store.digest(leaked[0]), now=t0 + 90000)
    assert store.verify(new_key, "orders:read", now=t0 + 90001)[0] == 401
    print("ok")
```

### Vault dynamic database credentials

Illustrative commands; the role and paths are configured once by an operator.

```bash
# Each read creates a new Postgres user with a TTL; Vault drops it on expiry.
vault read database/creds/billing-readonly
# Key                Value
# lease_id           database/creds/billing-readonly/2f6a...
# lease_duration     1h
# username           v-k8s-billing-readonl-Xa81...
# password           <generated>

vault lease revoke -prefix database/creds/billing-readonly  # kill all, now
```

Combined with the Vault Kubernetes auth method or Vault Agent injector, a pod gets credentials tied to its service account and nobody ever sees a long-lived database password.

## Pitfalls

- **Storing API keys in plaintext "so we can show them again".** One SQL injection and every customer integration is compromised. Show once; offer roll instead of reveal.
- **Keys in query strings.** They end up in nginx logs, CDN logs, analytics and `Referer`. Reject `?api_key=` outright.
- **Secrets in Git history.** Deleting the file doesn't help; the commit is in every clone and fork. Rotate first, then rewrite history if you must.
- **One shared key for everything.** A single "master" key used by ten internal services can't be rotated without coordinating all ten and can't be attributed in logs. One credential per consumer.
- **Revocation hidden behind a long cache.** A key revoked after a leak keeps working for an hour because of a 1-hour lookup cache. Cap the TTL or evict on revoke.
- **`kubectl get secret -o yaml` in a debug session.** The output, now in someone's terminal scrollback or a ticket, contains every value. Grant `get` on secrets narrowly.
- **Rotation that requires a deploy.** If changing a database password means editing a manifest and redeploying 30 services, nobody rotates. Make consumers reload.

## Interview questions

**How would you design API keys for a public API?**
CSPRNG keys with at least 128 bits, a product and environment prefix plus checksum, SHA-256 hash stored with tenant, scopes, expiry and last-used metadata, shown once. Accept them only in a header, verify with an indexed hash lookup, cache briefly, support multiple active keys for rotation, and register the format with secret scanners.

**Why hash API keys with SHA-256 instead of bcrypt?**
Slow hashes protect low-entropy secrets from guessing. A random 178-bit key can't be guessed at any speed, so a fast hash gives the same protection without adding latency to every request.

**How do you rotate a credential without downtime?**
Overlap: create the new credential while the old one is valid, roll it out, confirm the old one is unused via last-used tracking, then revoke. Consumers must be able to reload secrets without a redeploy.

**Where should a Kubernetes service get its database password?**
Ideally it doesn't have a long-lived one: Vault dynamic credentials or cloud IAM database auth via workload identity. Otherwise sync from a cloud secrets manager with External Secrets or the CSI driver, with etcd encryption at rest, narrow RBAC, and reload on change.

**Are Kubernetes Secrets secure?**
Only with configuration. They're base64, plaintext in etcd by default, readable by anyone with `list` on secrets or the ability to create pods in the namespace. Enable KMS encryption at rest, restrict RBAC, keep manifests out of Git, and prefer external secret stores.

**A customer's key was pushed to a public GitHub repo. What happens?**
GitHub's secret scanning flags it and notifies you as the provider if you're in the partner program; your endpoint validates the checksum and hash, revokes or rolls the key, and notifies the customer. Then audit that key's activity from its usage logs.

## Further reading

- OWASP Secrets Management Cheat Sheet
- Kubernetes documentation, *Good practices for Kubernetes Secrets* and *Encrypting Confidential Data at Rest*
- HashiCorp Vault documentation, database secrets engine and Kubernetes auth method
- GitHub Docs, *About secret scanning* and the secret scanning partner program
- The Twelve-Factor App, *III. Config*
