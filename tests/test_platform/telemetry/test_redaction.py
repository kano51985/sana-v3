from sana.platform.telemetry.redaction import TelemetryRedactor


def test_telemetry_attributes_are_allowlisted_and_identifiers_are_hashed() -> None:
    redactor = TelemetryRedactor(hash_salt="fixture-salt")

    attributes = redactor.attributes(
        {
            "search.mode": "RESEARCH",
            "search.policy_version": "search-v1",
            "run.id_hash": "raw-run-id",
            "user.message": "private health question",
            "model.prompt": "private full prompt",
            "document.body": "private page body",
            "provider.url": "https://private.example/path",
            "authorization": "Bearer secret",
        }
    )

    assert attributes["search.mode"] == "RESEARCH"
    assert attributes["search.policy_version"] == "search-v1"
    assert attributes["run.id_hash"].startswith("sha256:")
    assert "raw-run-id" not in attributes["run.id_hash"]
    assert set(attributes) == {
        "search.mode",
        "search.policy_version",
        "run.id_hash",
    }


def test_diagnostic_payload_keeps_metrics_but_redacts_content_and_unknown_fields() -> None:
    payload = {
        "mode": "FAST",
        "metrics": {
            "latency_ms": 420,
            "coverage": {"covered": 2, "total": 3},
        },
        "prompt": "do not retain this prompt",
        "document": {"body": "do not retain this body"},
        "notes": "free form user content",
        "url": "https://example.com/private/path",
        "reasoning_content": "private chain of thought",
        "raw_provider_payload": {"content": "private"},
    }

    result = TelemetryRedactor().diagnostic_payload(payload)
    rendered = repr(result)

    assert result["mode"] == "FAST"
    assert result["metrics"]["latency_ms"] == 420
    assert result["metrics"]["coverage"] == {"covered": 2, "total": 3}
    assert "do not retain" not in rendered
    assert "private chain of thought" not in rendered
    assert "private/path" not in rendered
    assert result["prompt"] == "[REDACTED]"
    assert result["document"] == "[REDACTED]"
    assert result["notes"] == "[REDACTED]"


def test_identifier_hash_is_stable_only_within_same_salt() -> None:
    first = TelemetryRedactor(hash_salt="one")
    same = TelemetryRedactor(hash_salt="one")
    other = TelemetryRedactor(hash_salt="two")

    assert first.hash_identifier("tenant-1") == same.hash_identifier("tenant-1")
    assert first.hash_identifier("tenant-1") != other.hash_identifier("tenant-1")
