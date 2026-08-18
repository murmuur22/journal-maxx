import base64
import hashlib
import hmac
import struct
import time
from cryptography.fernet import Fernet
from django.conf import settings

def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)

def encrypt_secret(secret): return _fernet().encrypt(secret.encode()).decode()
def decrypt_secret(value): return _fernet().decrypt(value.encode()).decode()

def generate_totp_secret(): return base64.b32encode(__import__("secrets").token_bytes(20)).decode().rstrip("=")

def totp(secret, timestamp=None):
    timestamp = timestamp or time.time(); counter = int(timestamp // 30)
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    digest = hmac.new(base64.b32decode(padded), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return str((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 1_000_000).zfill(6)

def verify_totp(secret, code):
    return bool(code) and any(hmac.compare_digest(totp(secret, time.time() + drift), str(code).strip()) for drift in (-30, 0, 30))

def issue_recovery_codes(user, count=8):
    from .models import RecoveryCode
    codes = ["-".join([__import__("secrets").token_hex(2), __import__("secrets").token_hex(2)]) for _ in range(count)]
    RecoveryCode.objects.bulk_create([RecoveryCode(user=user, code_hash=hashlib.sha256(code.encode()).hexdigest()) for code in codes])
    return codes

def verify_second_factor(user, code):
    from django.utils import timezone
    from .models import RecoveryCode
    clean = str(code or "").strip().lower()
    if user.totp_confirmed and verify_totp(decrypt_secret(user.totp_secret_encrypted), clean): return True
    digest = hashlib.sha256(clean.encode()).hexdigest()
    recovery = RecoveryCode.objects.filter(user=user, code_hash=digest, used_at__isnull=True).first()
    if recovery:
        recovery.used_at = timezone.now(); recovery.save(update_fields=["used_at"]); return True
    return False

def verify_privileged_credential(user, credential):
    """Re-authenticate a privileged action with MFA when enrolled, else password."""
    if user.totp_confirmed:
        return verify_second_factor(user, credential)
    return user.check_password(str(credential or ""))
