"""Encryption utilities for sensitive data"""
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend
import base64
import os
from config import Config


def get_encryption_key():
    """Generate or retrieve encryption key"""
    key = Config.ENCRYPTION_KEY
    
    # If key is not 32 bytes, derive a proper key
    if len(key) != 32:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'attendance_system_salt',
            iterations=100000,
            backend=default_backend()
        )
        key = base64.urlsafe_b64encode(kdf.derive(key[:32].ljust(32, b'0')))
    else:
        key = base64.urlsafe_b64encode(key)
    
    return key


def encrypt_data(data):
    """Encrypt string data"""
    if not data:
        return None
    fernet = Fernet(get_encryption_key())
    encrypted = fernet.encrypt(data.encode())
    return encrypted.decode()


def decrypt_data(encrypted_data):
    """Decrypt string data"""
    if not encrypted_data:
        return None
    fernet = Fernet(get_encryption_key())
    decrypted = fernet.decrypt(encrypted_data.encode())
    return decrypted.decode()
