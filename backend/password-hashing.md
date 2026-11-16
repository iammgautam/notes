---
title: Password hashing
section: backend
order: 52
phase: "Phase 7 · Security"
summary: Why fast hashes fail, how salts, peppers and work factors work, how to tune Argon2id, migrate legacy hashes and compare in constant time.
---

Assume your `users` table will leak: a SQL injection, a misconfigured backup bucket, a stolen laptop with a database dump. Password hashing decides what happens next. With a fast hash, attackers recover most passwords within hours and replay them against email, banking and your own SSO, because users reuse passwords. With a slow, salted, memory-hard hash, each guess costs real hardware time, and only the weakest passwords fall. This note covers storing passwords you verify. API keys are high-entropy random secrets and follow different rules (see [API keys and secrets management]({{ '/backend/api-keys-and-secrets/' | relative_url }})); session handling lives in [Authentication and sessions]({{ '/backend/authentication-and-sessions/' | relative_url }}).

## Why fast hashes fail

MD5, SHA-1 and SHA-256 are designed to be fast. That's right for checksums and signatures and wrong for passwords, because the attacker's job after a breach is an **offline** guessing attack: hash candidate passwords and compare against the stolen hashes, with no rate limit and no lockout.

- A single high-end GPU computes on the order of 10^10 SHA-256 hashes per second, and attackers rent hundreds of them.
- Guesses come from leaked password lists, dictionaries with mutation rules (`Summer2024!`), and masks, not blind brute force. Human-chosen passwords cluster heavily.
- Unsalted hashes can be looked up in precomputed tables (rainbow tables), and every user with the same password has the same hash.

The arithmetic for an 8-character lowercase-plus-digits password (36^8 ≈ 2.8 × 10^12 candidates):

| Hash | Guesses per second (one GPU, order of magnitude) | Time to exhaust |
|---|---|---|
| SHA-256 | 10^10 | about 5 minutes |
| bcrypt cost 12 / Argon2id tuned | 10^3 to 10^4 | years to centuries |

Password hashing functions (also called password-based KDFs) fix this with three ingredients: a unique **salt**, a tunable **work factor**, and, for modern ones, **memory hardness** so GPUs and ASICs gain little over CPUs.

## Salts

A salt is a random value generated per password and stored next to the hash: `hash = KDF(password, salt)`.

- Makes precomputed tables useless; the attacker must attack each hash separately.
- Two users with the same password get different hashes, so you can't spot shared passwords.
- 16 random bytes from a CSPRNG (`secrets.token_bytes(16)`) is the standard size.
- Not secret. It lives in the same column, usually encoded in the hash string (`$argon2id$v=19$m=19456,t=2,p=1$<salt>$<hash>`).

A single global salt is a mistake: it stops generic rainbow tables but still lets attackers crack all your users in one pass.

## Peppers

A pepper is a secret key applied to every password, stored **outside** the database (secrets manager, KMS, HSM). If only the database leaks, as with SQL injection or a leaked backup, the hashes can't be cracked at all without the pepper.

Two common constructions:

| Construction | How | Rotation |
|---|---|---|
| Pre-hash | `KDF(HMAC(pepper, password), salt)` | Store a pepper version; rehash on next login |
| Encrypt the hash | `AES-GCM(pepper_key, KDF(password, salt))` | Re-encrypt all rows with a new key offline, no passwords needed |

Peppers don't help if the application server is compromised, because the pepper is there. Treat them as defense in depth on top of a proper KDF, never as a replacement. Store a pepper version with each hash so you can rotate, and never put the pepper in the same database or in source control.

## Work factors

The work factor sets how expensive one hash is: iterations for PBKDF2, a cost exponent for bcrypt (`2^cost` rounds), `N` for scrypt, memory and passes for Argon2.

- **Tune on production hardware.** Benchmark and pick the highest cost your login latency and capacity allow; a common budget is tens to a few hundred milliseconds per hash.
- **Raise it over time.** Hardware gets faster; store parameters in each hash so old ones keep verifying and new ones use higher cost.
- **Budget capacity.** Login, signup and password change endpoints become CPU- and memory-bound. 50 concurrent logins with Argon2id at 64 MiB need 3.2 GB of RAM just for hashing. Rate-limit these endpoints (see [Rate limiting]({{ '/backend/rate-limiting/' | relative_url }})) or a login flood becomes a denial of service.
- **Don't hash on the event loop.** In async frameworks, offload to a thread pool (`asyncio.to_thread`, `sync_to_async`), or every request on that worker stalls.

## Choosing an algorithm

| Algorithm | Hardness | Parameters (OWASP minimums, as of 2025) | Notes |
|---|---|---|---|
| Argon2id | Memory + CPU | m=19 MiB, t=2, p=1 (or m=46 MiB, t=1) | First choice; winner of the Password Hashing Competition, RFC 9106 |
| scrypt | Memory + CPU | N=2^17, r=8, p=1 (128 MiB) | Good; in Python's `hashlib` |
| bcrypt | CPU, small fixed memory | cost ≥ 10 | Fine for legacy; 72-byte input limit |
| PBKDF2-HMAC-SHA256 | CPU only | 600,000 iterations | Use when FIPS-140 compliance is required |

PBKDF2 parallelizes perfectly on GPUs because it needs almost no memory. bcrypt's 4 KB state resists GPUs somewhat but not FPGAs. scrypt and Argon2 force each guess to fill and randomly read a large memory buffer, which is what makes massively parallel cracking expensive.

### Argon2id parameters

Argon2 has three variants: Argon2d (data-dependent memory access, strongest against GPUs, but vulnerable to side channels), Argon2i (data-independent, side-channel resistant), and **Argon2id**, a hybrid that RFC 9106 recommends as the default.

| Parameter | Meaning | Guidance |
|---|---|---|
| `m` (memory_cost) | KiB of memory per hash | As high as RAM × concurrency allows |
| `t` (time_cost) | Passes over memory | Increase until you hit the time budget |
| `p` (parallelism) | Lanes (threads) | 1 on busy web servers; more if hashing is rare and cores idle |
| salt | Random per hash | 16 bytes |
| tag length | Output size | 32 bytes |

RFC 9106 suggests `t=1, p=4, m=2 GiB` when you can afford it and `t=3, p=4, m=64 MiB` as the memory-constrained option. Those suit dedicated login services; shared web servers usually land near OWASP's lower settings. Tuning order: choose `p`, set `m` as high as your memory budget allows, then raise `t` until one hash takes your target time.

## Migrating old hashes

You inherit a table of unsalted MD5 or low-iteration PBKDF2. Three tools, usually combined:

1. **Rehash on login.** Verify with the old algorithm; on success you have the plaintext for a moment, so hash it with the current algorithm and save. Django does this automatically for anything that isn't the first entry in `PASSWORD_HASHERS` or has outdated parameters.
2. **Wrap now.** Users who never log in keep weak hashes forever. Wrap them offline: `new = argon2id(md5_hex)`, tagged with a scheme like `argon2-md5`. Verification computes `argon2id(md5(password))`. Every row is protected the day you ship, without knowing any password; Django's docs show this pattern for wrapping SHA-1 in PBKDF2.
3. **Expire stragglers.** After a deadline, force a password reset for accounts still on wrapped or legacy schemes and delete the old hashes.

Encode the scheme and parameters in each stored value (`scheme$params$salt$hash`) so mixed generations can coexist.

```text
 login(password)
      |
      v
 load stored ----> scheme?  legacy md5 / pbkdf2 / wrapped / current
      |                          |
      |                verify with that scheme
      |                          |
      |                 ok and scheme != current
      |                 or params < current?
      |                          | yes
      |                          v
      |               rehash with current scheme, save
      v
 return result (constant-time compare inside verify)
```

## Timing-safe comparison

Comparing derived keys with `==` returns as soon as a byte differs, so response time leaks how many leading bytes matched. For password hashes the practical risk is small because the attacker doesn't control the hash input directly, but it is a real risk for tokens, HMACs and API keys, and the fix is free: `hmac.compare_digest(a, b)` takes time that depends only on length.

The bigger timing leak on login is **user enumeration**: if unknown usernames return in 1 ms and known ones in 200 ms (the hash), an attacker learns which emails have accounts. Run a dummy hash for unknown users so both paths cost the same. Django's `ModelBackend` does this by hashing the submitted password once when the user doesn't exist. Make the error message identical too ("invalid email or password").

## In practice

### A runnable hasher with migration

scrypt with a versioned pepper, verification of a Django-style PBKDF2 legacy hash and an offline-wrapped MD5 hash, automatic upgrade on login, and a dummy hash for unknown users. The demo uses a lower scrypt `N` so it runs quickly; use the table's values in production.

```python
import base64
import hashlib
import hmac
import secrets
import unicodedata

N, R, P = 2**14, 8, 1  # demo cost; OWASP suggests N=2**17 for scrypt
PEPPERS = {1: b"old-pepper", 2: b"current-pepper"}  # load from a secret store
PEPPER_ID = 2


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _scrypt(secret: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=n, r=r, p=p, dklen=32,
                          maxmem=2 * 128 * r * n)


def _encode(scheme: str, secret: bytes) -> str:
    salt = secrets.token_bytes(16)
    peppered = hmac.new(PEPPERS[PEPPER_ID], secret, hashlib.sha256).digest()
    dk = _scrypt(peppered, salt, N, R, P)
    return "$".join([scheme, str(PEPPER_ID), str(N), str(R), str(P),
                     b64(salt), b64(dk)])


def _check_scrypt(secret: bytes, encoded: str) -> bool:
    _, pid, n, r, p, salt, dk = encoded.split("$")
    peppered = hmac.new(PEPPERS[int(pid)], secret, hashlib.sha256).digest()
    got = _scrypt(peppered, base64.b64decode(salt), int(n), int(r), int(p))
    return hmac.compare_digest(got, base64.b64decode(dk))


def hash_password(password: str) -> str:
    normalized = unicodedata.normalize("NFKC", password).encode()
    return _encode("scrypt", normalized)


def wrap_legacy_md5(md5_hex: str) -> str:
    """Offline migration: protect an unsalted MD5 hash without the password."""
    return _encode("scrypt-md5", md5_hex.encode())


def verify_password(password: str, encoded: str) -> bool:
    scheme = encoded.split("$", 1)[0]
    raw = password.encode()  # legacy systems hashed raw UTF-8
    if scheme == "scrypt":
        normalized = unicodedata.normalize("NFKC", password).encode()
        return _check_scrypt(normalized, encoded)
    if scheme == "scrypt-md5":
        return _check_scrypt(hashlib.md5(raw).hexdigest().encode(), encoded)
    if scheme == "pbkdf2_sha256":  # Django's format: algo$iters$salt$hash
        _, iters, salt, dk = encoded.split("$")
        got = hashlib.pbkdf2_hmac("sha256", raw, salt.encode(), int(iters))
        return hmac.compare_digest(b64(got), dk)
    raise ValueError(f"unknown scheme {scheme!r}")


def needs_rehash(encoded: str) -> bool:
    parts = encoded.split("$")
    if parts[0] != "scrypt":
        return True
    return tuple(map(int, parts[1:5])) != (PEPPER_ID, N, R, P)


DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def login(users: dict, username: str, password: str) -> bool:
    encoded = users.get(username)
    if encoded is None:
        verify_password(password, DUMMY_HASH)  # same cost as a real user
        return False
    if not verify_password(password, encoded):
        return False
    if needs_rehash(encoded):
        users[username] = hash_password(password)  # upgrade in place
    return True


if __name__ == "__main__":
    h1, h2 = hash_password("correct horse"), hash_password("correct horse")
    assert h1 != h2  # unique salts
    assert verify_password("correct horse", h1)
    assert not verify_password("correct horsE", h1)
    # NFKC: full-width letters and combining accents match their plain forms
    assert verify_password("ｃａｆé", hash_password("café"))

    salt = "legacysalt123"
    old_dk = hashlib.pbkdf2_hmac("sha256", b"hunter2", salt.encode(), 100_000)
    users = {
        "ana": "$".join(["pbkdf2_sha256", "100000", salt, b64(old_dk)]),
        "raj": wrap_legacy_md5(hashlib.md5(b"letmein").hexdigest()),
    }
    PEPPER_ID = 1                      # simulate a hash from before rotation
    users["kim"] = hash_password("s3cret!")
    PEPPER_ID = 2

    for name, pw in [("ana", "hunter2"), ("raj", "letmein"), ("kim", "s3cret!")]:
        assert needs_rehash(users[name])
        assert login(users, name, pw)
        assert users[name].startswith("scrypt$2$")  # upgraded on login
        assert not needs_rehash(users[name])
        assert login(users, name, pw)

    assert not login(users, "ana", "wrong")
    assert not login(users, "nobody", "anything")
    print("ok")
```

### Django settings

Illustrative; Argon2 needs `pip install "django[argon2]"` (the `argon2-cffi` package).

```python
# settings.py
PASSWORD_HASHERS = [
    "myapp.hashers.TunedArgon2PasswordHasher",            # used for new hashes
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",   # verify + upgrade
    "django.contrib.auth.hashers.ScryptPasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

# myapp/hashers.py
from django.contrib.auth.hashers import Argon2PasswordHasher

class TunedArgon2PasswordHasher(Argon2PasswordHasher):
    time_cost = 3
    memory_cost = 65536      # KiB = 64 MiB
    parallelism = 1
```

Only the first entry hashes new passwords; the rest verify existing ones, and `check_password()` re-saves them with the first hasher, including when only the cost parameters changed. Add a breached-password check (for example the k-anonymity range API from Have I Been Pwned) on signup and password change, as NIST SP 800-63B recommends.

## Pitfalls

- **bcrypt's 72-byte limit.** Bytes beyond 72 are silently ignored, so long passphrases sharing a prefix collide, and some implementations stop at a NUL byte. If you pre-hash for bcrypt, base64-encode the digest first (Django's `BCryptSHA256PasswordHasher` does this); better, use Argon2id.
- **Unbounded password length.** Allow long passwords (NIST asks for at least 64 characters) but cap the request at something like 1,024 characters so megabyte payloads can't burn CPU.
- **Passwords in logs.** Request body logging, Sentry with `send_default_pii`, exception locals. Use Django's `@sensitive_post_parameters` and scrub `password` fields in your logging and error-reporting config.
- **Memory blowups.** Argon2id at 64 MiB on a Gunicorn worker with 16 threads can spike past the container limit during a login burst and get OOM-killed. Size memory cost against worst-case concurrency.
- **"Encrypting" passwords.** Reversible storage means one key leak exposes every password. Hash, never encrypt.
- **Client-side hashing only.** If the browser sends `sha256(password)`, that digest is now the password, and stealing your table lets attackers log in directly. Always hash on the server.
- **Composition rules and forced rotation.** Rules like "one symbol, change every 90 days" produce `Password1!`, `Password2!`. NIST guidance favors length, breached-password checks and MFA.

## Interview questions

**Why not just store salted SHA-256?**
SHA-256 runs at billions of hashes per second per GPU, so a salt only stops precomputation; each hash still falls quickly to a dictionary attack. Password hashing needs a tunable work factor and ideally memory hardness: Argon2id, scrypt, bcrypt or high-iteration PBKDF2.

**What's the difference between a salt and a pepper?**
A salt is random per password, stored with the hash, not secret; it defeats precomputation and hides duplicate passwords. A pepper is one secret key for all passwords, stored outside the database; it makes a database-only leak uncrackable. Use a salt always; a pepper optionally.

**Which algorithm would you choose?**
Argon2id with memory sized to your concurrency, falling back to scrypt if Argon2 isn't available and PBKDF2-HMAC-SHA256 at OWASP's iteration count when FIPS compliance is required. Keep bcrypt for existing hashes.

**How do you migrate a legacy MD5 table without forcing resets?**
Wrap every hash offline with the new KDF (`argon2id(md5_hex)`), tag the scheme, verify by recomputing `argon2id(md5(password))`, and rehash to the plain current scheme on each successful login. After a deadline, force resets for the remainder.

**How do you prevent user enumeration through login timing?**
Run a dummy hash when the user doesn't exist so both paths take the same time, return the same error message, and rate-limit by account and IP. The same goes for password reset ("if an account exists, we sent an email").

**Why can API keys use plain SHA-256 when passwords can't?**
A 256-bit random key can't be guessed no matter how fast the hash is; the work factor exists only to slow guessing of low-entropy human passwords. A fast hash keeps per-request key verification cheap.

## Further reading

- RFC 9106, *Argon2 Memory-Hard Function for Password Hashing and Proof-of-Work Applications*
- OWASP Password Storage Cheat Sheet
- NIST SP 800-63B, *Digital Identity Guidelines: Authentication and Lifecycle Management*
- Provos and Mazières, *A Future-Adaptable Password Scheme* (USENIX 1999), the bcrypt paper
- Django documentation, *Password management in Django*
