---
title: Encryption at rest and in transit
section: backend
order: 54
phase: "Phase 7 · Security"
summary: TLS 1.3, certificates, mTLS and HSTS for data in transit; disk, database and field-level encryption, envelope encryption with KMS and key rotation at rest.
---

Encryption decides who can read data when something else has already gone wrong: a network tap, a stolen disk, a leaked backup, an over-privileged DBA, an SQL injection. Each layer defends against a specific threat and does nothing for the others, so the design question is never "is it encrypted?" but "encrypted where, with which key, and who can use that key?". This note covers data in transit (TLS, mTLS, HSTS) and at rest (disk, database, field-level, envelope encryption and key rotation). Key storage itself is covered in [API keys and secrets management]({{ '/backend/api-keys-and-secrets/' | relative_url }}).

## Hashing vs encryption vs encoding

These three get confused in code reviews and interviews alike.

| | Encoding | Hashing | Encryption |
|---|---|---|---|
| Purpose | Represent bytes safely for transport | Fingerprint, integrity, password verification | Confidentiality |
| Reversible? | Yes, by anyone | No, one-way | Yes, with the key |
| Key? | None | None (HMAC adds one for authenticity) | Required |
| Examples | Base64, hex, URL encoding | SHA-256, HMAC-SHA256, Argon2id | AES-256-GCM, ChaCha20-Poly1305, RSA-OAEP |
| Classic mistake | Treating base64 as protection (Kubernetes Secrets, JWT payloads) | A fast hash for passwords | "Encrypting" passwords; encryption without integrity |

Modern encryption should be **authenticated** (AEAD): it guarantees confidentiality and detects tampering in one primitive. AES-GCM and ChaCha20-Poly1305 are AEADs; AES-CBC without a MAC is not, and has produced padding-oracle attacks for decades. Passwords are hashed, not encrypted (see [Password hashing]({{ '/backend/password-hashing/' | relative_url }})).

## Encryption in transit

### TLS 1.3

TLS (RFC 8446 for 1.3) provides confidentiality, integrity and server authentication for TCP connections; QUIC builds the same handshake into HTTP/3.

```text
 Client                                          Server
   | ClientHello + key_share (X25519), ciphers     |
   |---------------------------------------------->|
   |       ServerHello + key_share                 |
   |       [EncryptedExtensions, Certificate,      |
   |        CertificateVerify, Finished]           |
   |<----------------------------------------------|
   | [Finished] + first HTTP request               |
   |---------------------------------------------->|
   |  one round trip before application data       |
   |  [ ] = encrypted with handshake keys          |
```

What changed from TLS 1.2, and why it matters:

- **1-RTT handshake** instead of 2, because the client guesses the key exchange group and sends its key share immediately. Resumption can use **0-RTT** early data, which an attacker can replay, so only allow it for idempotent requests (or disable it).
- **Forward secrecy always.** Only ephemeral (EC)DHE key exchange is allowed; RSA key transport is gone. Stealing the server's private key later doesn't decrypt recorded traffic.
- **AEAD-only cipher suites** (`TLS_AES_128_GCM_SHA256`, `TLS_AES_256_GCM_SHA384`, `TLS_CHACHA20_POLY1305_SHA256` in practice). CBC, RC4, SHA-1, compression and renegotiation are removed.
- **More of the handshake is encrypted,** including the server certificate.
- **Post-quantum transition.** Browsers and large CDNs have started deploying hybrid key exchange (X25519 combined with ML-KEM) as of 2025, to protect against traffic recorded now and decrypted later.

Where TLS ends matters as much as the version. Most systems terminate at a load balancer or ingress. From there, traffic to the backend is either re-encrypted, carried over mesh mTLS, or sent in plaintext inside a private network. Plaintext inside the VPC is a deliberate risk decision, and some compliance regimes don't allow it. Database connections need TLS too: PostgreSQL's `sslmode=require` encrypts but doesn't verify the server certificate, so only `verify-full` stops a man in the middle.

### Certificates

A server proves its identity with an X.509 certificate: a public key, the names it is valid for (Subject Alternative Names), a validity period, and a signature from a certificate authority (CA). Clients build a chain from the leaf through intermediates to a root in their trust store, then check the hostname against the SANs.

- **Automate issuance and renewal.** ACME (Let's Encrypt, cert-manager in Kubernetes) makes 90-day certificates routine. The CA/Browser Forum voted in 2025 to shrink maximum public certificate lifetimes in steps, down to 47 days by 2029, so manual renewal stops being viable.
- **Monitor expiry anyway.** An expired certificate is a full outage; alert at 30, 14 and 7 days.
- **Revocation is weak.** CRLs and OCSP are slow or soft-fail, which is the main reason lifetimes keep shrinking.
- **Internal services** use a private CA (Vault PKI, a cloud private CA, SPIRE, the service mesh's CA). Don't make clients skip verification for internal endpoints; trust the private CA instead.
- **Certificate pinning** in web clients causes more outages than it prevents; reserve it for mobile apps with a pin rotation plan.

### mTLS

In mutual TLS the client also presents a certificate, and the server verifies it against a CA it trusts. Both sides get a cryptographic identity from the connection itself.

- **Service-to-service authentication** in zero-trust networks: a service mesh (Istio, Linkerd) issues each workload a short-lived certificate with a SPIFFE ID like `spiffe://prod/ns/billing/sa/api` and enforces mTLS transparently (see [Service discovery and mesh]({{ '/backend/service-discovery-and-mesh/' | relative_url }})).
- **B2B and IoT clients** where each partner or device gets a certificate.
- **Sender-constrained OAuth tokens** (RFC 8705) bind a token to the client certificate.

The cost is certificate lifecycle at scale: issuance, rotation every hours to days, and revocation. That is why meshes automate it; hand-managed mTLS rarely survives contact with production.

### HSTS

HTTP Strict Transport Security (RFC 6797) is a response header telling browsers to use HTTPS only for this host, for `max-age` seconds:

```http
Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
```

Without it, a user typing `example.com` makes a plain HTTP request first, and an attacker on public Wi-Fi can strip the redirect to HTTPS and proxy the session. With HSTS the browser upgrades internally after the first visit; the `preload` list built into browsers covers even the first visit. Roll out gradually (a short `max-age`, then a week, then a year), because `includeSubDomains` breaks any subdomain still serving plain HTTP, and getting off the preload list takes months.

## Encryption at rest

| Layer | Protects against | Does not protect against | Examples |
|---|---|---|---|
| Disk / volume | Stolen disks, discarded hardware, raw snapshot access | Anyone with OS or database access, SQL injection | LUKS, EBS/Persistent Disk encryption |
| Database (TDE, storage) | Stolen data files and backups | Database users, SQL injection, app bugs | SQL Server/Oracle TDE, MySQL InnoDB encryption, RDS storage encryption |
| Field-level (application) | DBAs, replicas, backups, logs, SQL injection reading the column | A compromised app server holding the keys | AES-GCM in the app, AWS Encryption SDK, Google Tink |
| End-to-end (client-side) | The service operator | Compromised endpoints | Messaging apps, password managers |

### Disk and database encryption

Cloud volumes and managed databases encrypt storage with a KMS key, usually with one checkbox. It is necessary for compliance and nearly free, but it only covers threats below the database: anyone who can run `SELECT` sees plaintext. Community PostgreSQL does not ship transparent data encryption as of recent releases; teams rely on encrypted volumes (managed services do this for you) or on vendor distributions that add TDE. Remember backups, WAL archives, read replicas and exported dumps: they need the same protection.

### Field-level encryption

Encrypt specific sensitive columns in the application before they reach the database: national IDs, bank details, health data, OAuth refresh tokens you hold for third-party integrations.

- **Cost:** the database can't index, sort, range-query or full-text search the column.
- **Equality search** with a **blind index**: store `HMAC(index_key, normalize(value))` in a separate indexed column and query on that. It leaks which rows share a value, so avoid it for low-cardinality fields like gender or country.
- **Bind ciphertext to its row** by passing `tenant_id:row_id` as associated data, so an attacker with write access can't copy ciphertext from one row to another.
- **Per-tenant keys** enable crypto-shredding (delete the tenant's key and all its data is gone, including in backups) and bring-your-own-key for enterprise customers. See [Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }}).
- `pgcrypto` works but sends keys to the database in SQL, where they can land in logs and `pg_stat_statements`; encrypt in the application instead.

### Envelope encryption with KMS

You can't send every record to a KMS: it adds latency, costs money per call, and payloads are size-limited (4 KB for AWS KMS `Encrypt`). Envelope encryption uses two levels of keys:

```text
   +--------------------- KMS / HSM ---------------------+
   |  KEK v1, KEK v2 ... (never leave; every use logged) |
   +-----------------------------------------------------+
        ^  GenerateDataKey(context) / Decrypt(wrapped DEK)
        |
   app -+--> DEK in memory only (cache briefly)
   |              |
   |    AES-GCM(DEK, plaintext, aad = tenant:row)
   v              v
 row: kek_version | wrapped_dek | nonce | ciphertext+tag
```

1. Ask KMS to `GenerateDataKey`: it returns a random data encryption key (DEK) twice, once in plaintext and once encrypted ("wrapped") under the key encryption key (KEK) that never leaves KMS.
2. Encrypt the data locally with the plaintext DEK (AES-GCM), store the wrapped DEK next to the ciphertext, and discard the plaintext DEK.
3. To decrypt, send the wrapped DEK to KMS `Decrypt`, then decrypt locally.

Benefits: one KMS call per DEK rather than per record (cache plaintext DEKs for minutes to bound cost and latency), KMS audit logs show every decrypt, IAM policy on the KEK controls who can read data at all, and rotating the KEK doesn't touch the data. AWS KMS **encryption context** (key-value pairs bound as associated data and recorded in audit logs) ties a wrapped DEK to, say, a tenant ID.

### Key rotation

| What rotates | How | Data re-encrypted? |
|---|---|---|
| KEK (scheduled) | KMS creates a new key version; old versions stay available for decrypt | No. Optionally re-wrap DEKs, which is cheap |
| DEK (scheduled) | New DEK for new writes; old data re-encrypted lazily on write or by a background job | Yes, gradually |
| After compromise | New KEK and DEKs, re-encrypt everything, then disable old keys | Yes, urgently |

Store a key version with every ciphertext so several generations can coexist during a migration. With AES-GCM and random 96-bit nonces, NIST guidance limits one key to about 2^32 encryptions, another reason to use many DEKs rather than one key for everything.

## In practice

### Envelope encryption, rotation and blind index, runnable

The standard library has no AES, so this demo uses a **toy** AEAD built from HMAC-SHA256 (a keystream plus encrypt-then-MAC) to show the structure. Use AES-GCM from a vetted library in real code, as in the next snippet.

```python
import base64
import hashlib
import hmac
import json
import secrets


def _keystream(key, nonce, n):  # toy: HMAC-SHA256 in counter mode
    blocks = (hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256)
              .digest() for i in range((n + 31) // 32))
    return b"".join(blocks)[:n]


def _subkeys(key):
    return (hmac.new(key, b"enc", hashlib.sha256).digest(),
            hmac.new(key, b"mac", hashlib.sha256).digest())


def seal(key, plaintext, aad=b""):
    enc, mac = _subkeys(key)
    nonce = secrets.token_bytes(16)
    ks = _keystream(enc, nonce, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, ks))
    tag = hmac.new(mac, len(aad).to_bytes(8, "big") + aad + nonce + ct,
                   hashlib.sha256).digest()
    return nonce + ct + tag


def unseal(key, blob, aad=b""):
    enc, mac = _subkeys(key)
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    good = hmac.new(mac, len(aad).to_bytes(8, "big") + aad + nonce + ct,
                    hashlib.sha256).digest()
    if not hmac.compare_digest(tag, good):
        raise ValueError("tampered, wrong key or wrong context")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc, nonce, len(ct))))


class FakeKMS:
    """KEKs never leave this object; old versions stay for decryption."""

    def __init__(self):
        self._keks, self.current = {1: secrets.token_bytes(32)}, 1

    def rotate(self):
        self.current += 1
        self._keks[self.current] = secrets.token_bytes(32)

    def _aad(self, ctx):
        return json.dumps(ctx, sort_keys=True).encode()

    def generate_data_key(self, ctx):
        dek = secrets.token_bytes(32)
        return dek, (self.current, seal(self._keks[self.current], dek,
                                        self._aad(ctx)))

    def decrypt(self, wrapped, ctx):
        version, blob = wrapped
        return unseal(self._keks[version], blob, self._aad(ctx))

    def reencrypt(self, wrapped, ctx):
        dek = self.decrypt(wrapped, ctx)
        return self.current, seal(self._keks[self.current], dek, self._aad(ctx))


class FieldCipher:
    def __init__(self, kms):
        self.kms, self.wrapped = kms, {}  # tenant -> wrapped DEK (a table)

    def _dek(self, tenant):
        ctx = {"tenant": tenant}
        if tenant not in self.wrapped:
            dek, self.wrapped[tenant] = self.kms.generate_data_key(ctx)
            return dek
        return self.kms.decrypt(self.wrapped[tenant], ctx)  # cache in prod

    def encrypt(self, tenant, row_id, value):
        aad = f"{tenant}:{row_id}".encode()
        return base64.b64encode(seal(self._dek(tenant), value.encode(), aad))

    def decrypt(self, tenant, row_id, token):
        aad = f"{tenant}:{row_id}".encode()
        blob = base64.b64decode(token)
        return unseal(self._dek(tenant), blob, aad).decode()

    def rewrap_all(self):  # after KEK rotation; data untouched
        for tenant, w in self.wrapped.items():
            self.wrapped[tenant] = self.kms.reencrypt(w, {"tenant": tenant})

    def shred(self, tenant):  # crypto-shredding
        del self.wrapped[tenant]


INDEX_KEY = secrets.token_bytes(32)  # separate from encryption keys


def blind_index(email):
    norm = email.strip().lower().encode()
    return hmac.new(INDEX_KEY, norm, hashlib.sha256).hexdigest()[:32]


if __name__ == "__main__":
    # encoding is not protection
    assert base64.b64decode(base64.b64encode(b"s3cret")) == b"s3cret"

    kms = FakeKMS()
    fc = FieldCipher(kms)
    c1 = fc.encrypt("acme", 7, "DE89 3704 0044 0532 0130 00")
    c2 = fc.encrypt("acme", 7, "DE89 3704 0044 0532 0130 00")
    assert c1 != c2  # random nonce per encryption
    assert fc.decrypt("acme", 7, c1) == "DE89 3704 0044 0532 0130 00"

    raw = bytearray(base64.b64decode(c1))
    raw[20] ^= 1  # flip one ciphertext bit
    tampered = base64.b64encode(bytes(raw))
    for bad in (lambda: fc.decrypt("acme", 8, c1),        # moved to another row
                lambda: fc.decrypt("acme", 7, tampered)):  # modified in place
        try:
            bad()
            raise AssertionError("should fail")
        except ValueError:
            pass

    kms.rotate()
    assert fc.wrapped["acme"][0] == 1             # still wrapped by v1
    assert fc.decrypt("acme", 7, c1)              # old versions still work
    fc.rewrap_all()
    assert fc.wrapped["acme"][0] == 2             # re-wrapped, data unchanged
    assert fc.decrypt("acme", 7, c1).startswith("DE89")

    fc.encrypt("globex", 1, "secret")
    fc.shred("globex")
    assert "globex" not in fc.wrapped  # its ciphertexts are now unreadable

    assert blind_index(" Ana@Example.com") == blind_index("ana@example.com")
    assert blind_index("ana@example.com") != blind_index("bob@example.com")
    print("ok")
```

### Envelope encryption with AWS KMS and AES-GCM

Illustrative, using `boto3` and the `cryptography` package:

```python
import os
import boto3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

kms = boto3.client("kms")

def encrypt_record(tenant_id: str, row_id: str, plaintext: bytes) -> dict:
    dk = kms.generate_data_key(KeyId="alias/app-data", KeySpec="AES_256",
                               EncryptionContext={"tenant_id": tenant_id})
    nonce = os.urandom(12)  # 96-bit nonce, never reused with the same key
    aad = f"{tenant_id}:{row_id}".encode()
    ct = AESGCM(dk["Plaintext"]).encrypt(nonce, plaintext, aad)
    return {"wrapped_dek": dk["CiphertextBlob"], "nonce": nonce, "ct": ct}

def decrypt_record(tenant_id: str, row_id: str, rec: dict) -> bytes:
    dek = kms.decrypt(CiphertextBlob=rec["wrapped_dek"],
                      EncryptionContext={"tenant_id": tenant_id})["Plaintext"]
    aad = f"{tenant_id}:{row_id}".encode()
    return AESGCM(dek).decrypt(rec["nonce"], rec["ct"], aad)
```

The AWS Encryption SDK and Google Tink package this pattern, including DEK caching and a versioned ciphertext format; prefer them over hand-assembly.

### TLS and HSTS configuration

Illustrative nginx and Django settings:

```nginx
server {
    listen 443 ssl;
    http2 on;                                  # nginx 1.25.1+
    server_name api.example.com;
    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    # mTLS for a partner endpoint:
    # ssl_client_certificate /etc/nginx/partner-ca.pem;
    # ssl_verify_client on;
}
server {
    listen 80;
    server_name api.example.com;
    return 301 https://$host$request_uri;
}
```

```python
# settings.py
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")  # only behind a trusted proxy
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
DATABASES["default"]["OPTIONS"] = {
    "sslmode": "verify-full",
    "sslrootcert": "/etc/ssl/certs/db-ca.pem",
}
```

## Pitfalls

- **AES-GCM nonce reuse.** Two messages under the same key and nonce leak the XOR of their plaintexts and let attackers forge tags. Use random 96-bit nonces with a per-key message limit, a counter, or a misuse-resistant mode (AES-GCM-SIV).
- **Hand-rolled crypto.** ECB mode, CBC without a MAC, MAC-then-encrypt, home-made key derivation. Use `cryptography`, libsodium/PyNaCl, Tink or the AWS Encryption SDK.
- **Keys stored with the data.** An encryption key in the same database, or in `settings.py` in the same repo, turns encryption into obfuscation.
- **`verify=False` and `sslmode=require`.** Both encrypt to whoever answers. Symptom: nothing, until someone sits in the middle.
- **Decrypted data in logs.** The field is encrypted in Postgres, but the serializer logs it and Sentry stores it. Encrypting a column is pointless if it's logged in plaintext.
- **Unencrypted backups and exports.** The production database is encrypted; the nightly `pg_dump` in an S3 bucket isn't.
- **Losing keys.** Deleting a KMS key destroys every piece of data under it. AWS enforces a 7 to 30 day waiting period; alert on scheduled deletions.
- **Certificate expiry outages.** Automate renewal, monitor expiry, and remember certificates embedded in clients, webhooks and partner configs.

## Interview questions

**What's the difference between hashing, encryption and encoding?**
Encoding changes representation and is reversible by anyone. Hashing is one-way and used for integrity and password verification. Encryption is reversible only with a key and provides confidentiality; use authenticated encryption so tampering is detected.

**What does TLS 1.3 improve over TLS 1.2?**
A 1-RTT handshake (0-RTT resumption with replay caveats), mandatory forward secrecy via ephemeral key exchange, AEAD-only cipher suites, removal of legacy algorithms and renegotiation, and encryption of more of the handshake including the certificate.

**What is envelope encryption and why use it?**
Data is encrypted locally with a data key; the data key is encrypted by a master key held in KMS. It avoids sending data to KMS, keeps the master key in an HSM with audit logs and IAM, allows per-tenant keys and crypto-shredding, and makes master key rotation a cheap re-wrap.

**How do you rotate encryption keys over terabytes of data?**
Rotate the KEK in KMS and re-wrap DEKs, which touches only small key blobs. For DEK rotation, version every ciphertext, write new data with the new key and re-encrypt old data lazily on write or with a throttled background job.

**Disk encryption is on. Is the data safe from SQL injection?**
No. Disk and storage encryption protect against stolen media and raw snapshot access; the database decrypts transparently for any authorized query. Field-level encryption in the application protects specific columns from database-level access.

**How can you search an encrypted email column?**
Add a blind index: an HMAC of the normalized email under a separate key, stored in an indexed column, used for equality lookups. Accept that it reveals equal values; range and prefix search aren't possible.

**When would you use mTLS?**
For service-to-service authentication in zero-trust networks (usually via a mesh that automates short-lived certificates), for B2B partners or devices with issued certificates, and to bind OAuth tokens to a client. The main cost is certificate lifecycle management.

## Further reading

- RFC 8446, *The Transport Layer Security (TLS) Protocol Version 1.3*
- RFC 6797, *HTTP Strict Transport Security (HSTS)*
- NIST SP 800-38D, *Recommendation for Block Cipher Modes of Operation: Galois/Counter Mode (GCM) and GMAC*
- AWS Key Management Service Developer Guide, sections on envelope encryption and encryption context
- Jean-Philippe Aumasson, *Serious Cryptography* (No Starch Press)
