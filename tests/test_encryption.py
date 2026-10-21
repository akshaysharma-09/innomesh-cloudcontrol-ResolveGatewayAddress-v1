"""
Encryption parity with innomesh-cloudcontrol-pushAssetMetadata-v1 (RFC A-07).

A gateway password this lambda writes must be indistinguishable in form from
a device password that lambda writes, because the Sight worker decrypts both
with one routine:

    base64url( RSA-OAEP-SHA1( utf8(password), tenant RSA public key ) )

These tests encrypt with this lambda's code and decrypt with the worker's
routine written out longhand, so a change to either side fails here rather
than silently orphaning every stored credential.
"""

import base64
import os
import sys
from unittest.mock import patch

import pytest
from Crypto.Cipher import PKCS1_OAEP
from Crypto.PublicKey import RSA

with patch("boto3.client"), patch("boto3.resource"), patch("boto3.Session"):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

    from app import build_rsa_cipher, encrypt_password


@pytest.fixture(scope="module")
def keypair():
    key = RSA.generate(2048)
    # The SSM parameter holds base64 of the DER public key.
    public_key_ssm_value = base64.b64encode(key.publickey().export_key(format="DER")).decode("ASCII")
    return key, public_key_ssm_value


def worker_decrypt(private_key, stored):
    """innosight-worker-v2 shared/awshelper/kms.go DecryptPassword, in Python."""
    raw = base64.urlsafe_b64decode(stored)
    return PKCS1_OAEP.new(key=private_key).decrypt(raw).decode("utf-8")


class TestEncryptionParity:
    @pytest.mark.parametrize(
        "plaintext",
        [
            "hunter2",
            " ",
            "pässwörd-中文",
            "a|b=c&d?e/f+g#h%i\"j'k\\l",
            "x" * 214,  # the OAEP-SHA1 ceiling for a 2048-bit key
        ],
    )
    def test_round_trips_through_the_worker_routine(self, keypair, plaintext):
        private_key, public_key_ssm_value = keypair
        cipher = build_rsa_cipher(public_key_ssm_value)

        stored, is_encrypted = encrypt_password(cipher, plaintext)

        assert is_encrypted is True
        assert worker_decrypt(private_key, stored) == plaintext

    def test_stored_form_is_url_safe_base64(self, keypair):
        _, public_key_ssm_value = keypair
        stored, _ = encrypt_password(build_rsa_cipher(public_key_ssm_value), "hunter2")

        # The worker decodes with Go's base64.URLEncoding, which requires
        # padding and rejects the standard alphabet's + and /.
        assert "+" not in stored and "/" not in stored
        assert "\n" not in stored
        assert len(stored) == 344  # 2048-bit ciphertext, padded base64

    def test_empty_password_is_stored_plain_and_unflagged(self, keypair):
        _, public_key_ssm_value = keypair
        stored, is_encrypted = encrypt_password(build_rsa_cipher(public_key_ssm_value), "")

        # Valid and common: EvertzAV NUCLEUS has no auth.
        assert stored == ""
        assert is_encrypted is False

    def test_encryption_is_randomised(self, keypair):
        private_key, public_key_ssm_value = keypair
        cipher = build_rsa_cipher(public_key_ssm_value)

        first, _ = encrypt_password(cipher, "hunter2")
        second, _ = encrypt_password(cipher, "hunter2")

        # OAEP is randomised, so a redeploy rewrites the attribute with
        # fresh bytes. The state stays semantically idempotent, not
        # byte-idempotent.
        assert first != second
        assert worker_decrypt(private_key, first) == "hunter2"
        assert worker_decrypt(private_key, second) == "hunter2"

    def test_password_too_long_for_the_key_is_refused(self, keypair):
        _, public_key_ssm_value = keypair
        cipher = build_rsa_cipher(public_key_ssm_value)

        with pytest.raises(ValueError):
            encrypt_password(cipher, "x" * 215)
