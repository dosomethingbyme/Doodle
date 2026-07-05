"""Password hashing, opaque token hashing, and signed booking links."""

import base64
import hashlib
import hmac
import secrets

PASSWORD_ITERATIONS = 310_000


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def new_token(size=32):
    return secrets.token_urlsafe(size)


def token_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def booking_manage_token(secret, booking_ref):
    digest = hmac.new(secret.encode("utf-8"), booking_ref.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_booking_manage_token(secret, booking_ref, token):
    return hmac.compare_digest(booking_manage_token(secret, booking_ref), str(token or ""))
