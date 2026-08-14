from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_logged_out_app_renders_without_accessing_backend() -> None:
    app_path = (
        Path(__file__).resolve().parents[3]
        / "sana"
        / "clients"
        / "streamlit"
        / "app.py"
    )

    app = AppTest.from_file(str(app_path), default_timeout=10).run()

    assert not app.exception
    assert len(app.title) == 1
    assert len(app.button) == 1


def test_client_source_has_no_legacy_data_or_secret_adapters() -> None:
    client_root = (
        Path(__file__).resolve().parents[3] / "sana" / "clients" / "streamlit"
    )
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in client_root.rglob("*.py")
    )

    for forbidden in (
        "SanaAgent",
        "pymongo",
        "chromadb",
        "user_profile.json",
        "get_user_env",
        "set_user_env",
        "use_container_width",
        "unsafe_allow_html",
    ):
        assert forbidden not in source
