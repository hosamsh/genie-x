from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.pipeline.extraction.embedding_postprocess import run_embedding_postprocess
from src.shared.models.workspace import WorkspaceExtractionResult


def _result(**overrides) -> WorkspaceExtractionResult:
    payload = {
        "status": "success",
        "workspace_id": "ws-1",
        "workspace_name": "demo",
        "workspace_folder": "",
        "session_count": 1,
        "turn_count": 2,
        "duration_ms": 10,
        "embedding_min_turn_id": 11,
        "embedding_max_turn_id": 12,
    }
    payload.update(overrides)
    return WorkspaceExtractionResult(**payload)


def test_embedding_postprocess_skips_when_auto_embed_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.pipeline.extraction.embedding_postprocess.get_config",
        lambda: SimpleNamespace(
            search=SimpleNamespace(
                auto_embed_on_extraction=False,
                semantic_model="model",
                embedding_batch_size=64,
            )
        ),
    )

    def fail_init(*_args, **_kwargs):
        raise AssertionError("database should not be opened when embedding postprocess is disabled")

    monkeypatch.setattr("src.pipeline.extraction.embedding_postprocess.init_shared_db", fail_init)

    stats = run_embedding_postprocess(_result(), tmp_path / "genie.db")

    assert stats == {"enabled": False, "updated": 0, "skipped": 0, "total": 0}


def test_embedding_postprocess_uses_stored_turn_range(monkeypatch, tmp_path: Path) -> None:
    calls = {}

    class FakeConnection:
        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(
        "src.pipeline.extraction.embedding_postprocess.get_config",
        lambda: SimpleNamespace(
            search=SimpleNamespace(
                auto_embed_on_extraction=True,
                semantic_model="model",
                embedding_batch_size=32,
            )
        ),
    )
    monkeypatch.setattr(
        "src.pipeline.extraction.embedding_postprocess.init_shared_db",
        lambda db_path, verbose=False: calls.setdefault("db", (db_path, verbose)) and FakeConnection(),
    )

    def fake_generate_embeddings(conn, **kwargs):
        calls["conn"] = conn
        calls["kwargs"] = kwargs
        return {"total": 2, "updated": 2, "skipped": 0}

    monkeypatch.setattr(
        "src.pipeline.extraction.embedding_postprocess.generate_embeddings",
        fake_generate_embeddings,
    )

    db_path = tmp_path / "genie.db"
    stats = run_embedding_postprocess(_result(), db_path)

    assert stats == {"enabled": True, "total": 2, "updated": 2, "skipped": 0}
    assert calls["db"] == (db_path, False)
    assert calls["kwargs"] == {
        "model_name": "model",
        "batch_size": 32,
        "min_turn_id": 11,
        "max_turn_id": 12,
        "verbose": True,
    }
    assert calls["closed"] is True
