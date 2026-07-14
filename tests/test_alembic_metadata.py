from __future__ import annotations

from app.db.metadata import get_target_metadata


def test_project_metadata_contains_diary_entries_table() -> None:
    """模板工程里的首个模型表，应该已经收口到当前正式业务骨架语义。"""
    metadata = get_target_metadata()
    assert "diary_entries" in metadata.tables
    assert "articles" not in metadata.tables

    diary_entries_table = metadata.tables["diary_entries"]
    assert "mood" in diary_entries_table.columns
    assert "entry_date" in diary_entries_table.columns
