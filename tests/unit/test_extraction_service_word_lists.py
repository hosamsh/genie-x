from __future__ import annotations

import sqlite3

from src.web.services.extraction_service import generate_word_lists


def test_generate_word_lists_truncates_text_before_tokenizing() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sample(role TEXT, model_id TEXT, text TEXT, thinking_text TEXT)")
    conn.execute(
        "INSERT INTO sample(role, model_id, text, thinking_text) VALUES (?, ?, ?, ?)",
        (
            "assistant",
            "model-a",
            "Alpha keep keep keep ZetaDrop ZetaDrop ZetaDrop",
            None,
        ),
    )

    result = generate_word_lists(
        conn,
        "SELECT role, model_id, text, thinking_text FROM sample",
        top_model_ids=["model-a"],
        min_word_length=3,
        max_words_per_group=50,
        max_chars_per_text=20,
    )

    assistant_words = dict(result["assistant_all"]["response"])
    assert "keep" in assistant_words
    assert "zetadrop" not in assistant_words

    conn.close()