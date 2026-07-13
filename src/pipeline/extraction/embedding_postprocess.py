"""Post-storage embedding generation for extracted turns."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src.shared.config.config_loader import get_config
from src.shared.database.db_schema import init_shared_db
from src.shared.logging.logger import get_logger
from src.shared.models.workspace import WorkspaceExtractionResult
from src.shared.search.search_indexer import generate_embeddings

logger = get_logger(__name__)


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def run_embedding_postprocess(
    result: WorkspaceExtractionResult,
    db_path: Path,
) -> dict[str, Any]:
    """Generate embeddings for the newly stored turn range when configured."""
    config = get_config().search
    if not config.auto_embed_on_extraction:
        logger.info(
            f"[PERF] embedding_postprocess {result.workspace_id} | skipped: 0.0ms "
            "(auto_embed_on_extraction=false)"
        )
        return {"enabled": False, "updated": 0, "skipped": 0, "total": 0}

    min_turn_id = result.embedding_min_turn_id
    max_turn_id = result.embedding_max_turn_id
    if not result.success or not min_turn_id or not max_turn_id:
        logger.info(
            f"[PERF] embedding_postprocess {result.workspace_id} | skipped: 0.0ms "
            "(no new turn range)"
        )
        return {"enabled": True, "updated": 0, "skipped": 0, "total": 0}

    perf_start = time.perf_counter()
    conn = init_shared_db(db_path, verbose=False)
    try:
        stats = generate_embeddings(
            conn,
            model_name=config.semantic_model,
            batch_size=config.embedding_batch_size,
            min_turn_id=min_turn_id,
            max_turn_id=max_turn_id,
            verbose=True,
        )
    except (ValueError, RuntimeError, OSError):
        logger.info(
            f"[PERF] embedding_postprocess {result.workspace_id} | failed: "
            f"{_elapsed_ms(perf_start):.1f}ms"
        )
        return {"enabled": True, "updated": 0, "skipped": 0, "total": 0, "failed": True}
    finally:
        conn.close()

    logger.info(
        f"[PERF] embedding_postprocess {result.workspace_id} | TOTAL: "
        f"{_elapsed_ms(perf_start):.1f}ms "
        f"({stats.get('updated', 0)} updated, {stats.get('skipped', 0)} skipped)"
    )
    return {"enabled": True, **stats}
