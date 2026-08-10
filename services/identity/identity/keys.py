"""Load-or-generate the RSA signing key.

Local dev: a PEM file under infra/local/keys/ (gitignored) so the kid — and
therefore issued tokens — survive restarts. AWS later: Secrets Manager behind
this same function.
"""

import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from smartfood_auth import RsaKey, generate_rsa_key


def _b64url_uint(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_or_generate(path: str) -> RsaKey:
    p = Path(path)
    if not p.exists():
        key = generate_rsa_key()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(key.private_pem)
        return key

    private = serialization.load_pem_private_key(p.read_bytes(), password=None)
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ValueError(f"{path} is not an RSA private key")
    numbers = private.public_key().public_numbers()
    n_bytes = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    kid = hashlib.sha256(n_bytes).hexdigest()[:16]
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }
    return RsaKey(kid=kid, private_pem=p.read_text(), public_jwk=jwk)
