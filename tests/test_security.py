"""Security tests: encryption, redaction, and non-leakage of secrets."""

from __future__ import annotations

import json

import pytest

from app.security.crypto import DecryptionError, decrypt_secret, encrypt_secret
from app.security.hashing import (
    api_key_preview,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)
from app.utils.redaction import mask_secret, redact_mapping, redact_text

SECRET_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.s3cr3t-signature-value"


class TestCrypto:
    def test_round_trip(self) -> None:
        ciphertext = encrypt_secret(SECRET_TOKEN)
        assert SECRET_TOKEN not in ciphertext
        assert decrypt_secret(ciphertext) == SECRET_TOKEN

    def test_ciphertext_is_non_deterministic(self) -> None:
        assert encrypt_secret(SECRET_TOKEN) != encrypt_secret(SECRET_TOKEN)

    def test_tampered_ciphertext_rejected(self) -> None:
        ciphertext = encrypt_secret(SECRET_TOKEN)
        tampered = ciphertext[:-4] + "AAAA"
        with pytest.raises(DecryptionError):
            decrypt_secret(tampered)

    def test_empty_secret_refused(self) -> None:
        with pytest.raises(ValueError):
            encrypt_secret("")


class TestHashing:
    def test_password_hash_verifies(self) -> None:
        encoded = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", encoded)
        assert not verify_password("wrong", encoded)
        assert "correct horse" not in encoded

    def test_api_key_hash_is_stable_and_opaque(self) -> None:
        key = generate_api_key()
        assert key.startswith("qwg_")
        digest = hash_api_key(key)
        assert digest == hash_api_key(key)
        assert key not in digest

    def test_preview_hides_most_of_key(self) -> None:
        key = generate_api_key()
        preview = api_key_preview(key)
        assert preview.startswith("qwg_")
        assert key not in preview
        assert len(preview) < len(key)


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "Authorization: Bearer sk-abcdef1234567890",
            f"cookie: token={SECRET_TOKEN}",
            "key=qwg_AbCdEf1234567890abcdef",
            f"payload {SECRET_TOKEN}",
        ],
    )
    def test_secret_patterns_are_redacted(self, text: str) -> None:
        cleaned = redact_text(text)
        assert "sk-abcdef1234567890" not in cleaned
        assert SECRET_TOKEN not in cleaned
        assert "qwg_AbCdEf1234567890abcdef" not in cleaned

    def test_sensitive_keys_masked(self) -> None:
        cleaned = redact_mapping(
            {
                "authorization": "Bearer abc",
                "cookie": "token=xyz",
                "password": "hunter2",
                "model": "qwen",
                "nested": {"api_key": "qwg_secret", "ok": "value"},
            }
        )
        assert cleaned["authorization"] == "[REDACTED]"
        assert cleaned["password"] == "[REDACTED]"
        assert cleaned["nested"]["api_key"] == "[REDACTED]"
        assert cleaned["model"] == "qwen"
        assert cleaned["nested"]["ok"] == "value"

    def test_mask_secret_shows_only_tail(self) -> None:
        masked = mask_secret("supersecretvalue1234")
        assert masked.endswith("1234")
        assert "supersecret" not in masked


class TestNoSecretLeakage:
    async def test_credential_secret_never_returned_by_admin_api(
        self, admin_client, app_context
    ) -> None:
        response = await admin_client.post(
            "/api/admin/credentials",
            json={"name": "leak-check", "secret": SECRET_TOKEN, "auth_mode": "portal"},
        )
        assert response.status_code == 201, response.text
        created = response.json()
        assert SECRET_TOKEN not in json.dumps(created)
        assert created["secret_hint"].startswith("•")

        listing = await admin_client.get("/api/admin/credentials")
        assert SECRET_TOKEN not in listing.text
        assert "encrypted_secret" not in listing.text

    async def test_secret_not_in_request_history(
        self, admin_client, auth_headers, app_context
    ) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="hist", secret="mock:normal", provider="mock")
        await admin_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        history = await admin_client.get("/api/admin/requests")
        assert history.status_code == 200
        assert "mock:normal" not in history.text
        assert "encrypted" not in history.text

    async def test_secrets_not_in_logs(self, admin_client, app_context) -> None:
        await admin_client.post(
            "/api/admin/credentials",
            json={"name": "log-check", "secret": SECRET_TOKEN},
        )
        logs = await admin_client.get("/api/admin/logs")
        assert logs.status_code == 200
        assert SECRET_TOKEN not in logs.text

    async def test_api_key_plaintext_only_returned_once(self, admin_client, app_context) -> None:
        created = await admin_client.post("/api/admin/api-keys", json={"name": "once"})
        assert created.status_code == 201
        plaintext = created.json()["api_key"]
        assert plaintext.startswith("qwg_")

        listing = await admin_client.get("/api/admin/api-keys")
        assert plaintext not in listing.text
        assert listing.json()[0]["key_preview"].startswith("qwg_")

    async def test_database_stores_no_plaintext_credential(self, app_context) -> None:
        from sqlalchemy import select

        from app.database import session_scope
        from app.models.db import QwenCredential
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="db", secret=SECRET_TOKEN)
            rows = list((await session.execute(select(QwenCredential))).scalars())

        assert rows[0].encrypted_secret != SECRET_TOKEN
        assert SECRET_TOKEN not in rows[0].encrypted_secret
        assert decrypt_secret(rows[0].encrypted_secret) == SECRET_TOKEN

    async def test_error_responses_have_no_stack_traces(
        self, client, auth_headers, app_context
    ) -> None:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 503
        text = response.text
        assert "Traceback" not in text
        assert 'File "' not in text
        assert "app/gateway" not in text


class TestAdminProtection:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/admin/credentials"),
            ("post", "/api/admin/credentials"),
            ("get", "/api/admin/api-keys"),
            ("get", "/api/admin/logs"),
            ("get", "/api/admin/requests"),
            ("get", "/api/admin/settings"),
            ("get", "/api/admin/overview"),
        ],
    )
    async def test_admin_requires_authentication(self, client, method, path) -> None:
        response = await getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert response.status_code == 401, f"{path} was reachable unauthenticated"

    async def test_bad_admin_password_rejected(self, client) -> None:
        response = await client.post(
            "/api/admin/login", json={"username": "admin", "password": "wrong"}
        )
        assert response.status_code == 401

    async def test_client_api_key_cannot_access_admin(self, client, auth_headers) -> None:
        response = await client.get("/api/admin/credentials", headers=auth_headers)
        assert response.status_code == 401

    async def test_logout_invalidates_session(self, admin_client) -> None:
        assert (await admin_client.get("/api/admin/credentials")).status_code == 200
        await admin_client.post("/api/admin/logout")
        assert (await admin_client.get("/api/admin/credentials")).status_code == 401
