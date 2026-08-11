"""R2 material_schema：五类规范前缀、legacy reader 单向归一化与 envelope 混用 fail closed。"""

from __future__ import annotations

import pytest

from app.runtime.material_schema import (
    CANONICAL_MATERIAL_PREFIXES,
    LEGACY_PREFIX_TO_CANONICAL,
    SUPPORTED_SNAPSHOT_SCHEMA_MAJOR,
    LegacyEnvelopeError,
    SnapshotSchemaError,
    assert_snapshot_schema_major,
    detect_envelope_mixing,
    normalize_source_ref,
    read_material_refs_from_envelope,
)


def test_canonical_prefixes_match_contract_material_types() -> None:
    """运行期前缀表必须与 contract v1.0.0 frozen material_types 完全一致。"""
    assert CANONICAL_MATERIAL_PREFIXES == {
        "diary",
        "completed_bet",
        "handbook_note",
        "matured_wish",
        "bucket_list_completion",
    }


def test_legacy_prefix_map_only_contains_known_aliases() -> None:
    """legacy 表只允许单向映射；新增 legacy 别名必须先扩 contract。"""
    assert LEGACY_PREFIX_TO_CANONICAL == {"bet": "completed_bet"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("bet:b1", "completed_bet:b1"),
        ("bet:", "completed_bet:"),
        ("diary:d1", "diary:d1"),
        ("completed_bet:b1", "completed_bet:b1"),
        ("handbook_note:h1", "handbook_note:h1"),
        ("matured_wish:w1", "matured_wish:w1"),
        ("bucket_list_completion:c1", "bucket_list_completion:c1"),
        ("unknown:x", "unknown:x"),
        ("no-prefix", "no-prefix"),
        ("", ""),
    ],
)
def test_normalize_source_ref_only_maps_legacy_bet(raw: str, expected: str) -> None:
    """legacy bet:<id> 单向归一化；其他前缀原样返回交由 allowlist 拦截。"""
    assert normalize_source_ref(raw) == expected


def test_detect_envelope_mixing_fails_closed_when_legacy_and_canonical_coexist() -> None:
    """bet_items 与 completed_bet_items 同现必须 fail closed，避免双计数。"""
    with pytest.raises(LegacyEnvelopeError, match="LEGACY_ENVELOPE_MIXED_WITH_CANONICAL"):
        detect_envelope_mixing({"bet_items": [], "completed_bet_items": []})
    with pytest.raises(LegacyEnvelopeError, match="LEGACY_ENVELOPE_MIXED_WITH_CANONICAL"):
        detect_envelope_mixing({"bets": [], "completed_bets": []})
    with pytest.raises(LegacyEnvelopeError, match="LEGACY_ENVELOPE_MIXED_WITH_CANONICAL"):
        detect_envelope_mixing({"bet_items": [], "completed_bets": []})


def test_detect_envelope_mixing_accepts_pure_legacy_or_pure_canonical_envelope() -> None:
    """单一形态（全 legacy 或全 canonical）合法；只对混用 fail closed。"""
    detect_envelope_mixing({"bet_items": [], "diaries": []})  # 不抛
    detect_envelope_mixing({"completed_bet_items": [], "diary_items": []})  # 不抛
    detect_envelope_mixing({})  # 空也允许


def test_read_material_refs_normalizes_legacy_bet_to_completed_bet() -> None:
    """legacy reader 是 Runtime 唯一接受 bet_items 的入口；产出规范前缀引用。"""
    snapshot = {
        "diary_items": [{"id": "d1"}, {"id": "d2"}],
        "bet_items": [{"id": "b1"}, {"id": "b2"}],
    }
    result = read_material_refs_from_envelope(snapshot)
    assert result.source_refs == [
        "diary:d1", "diary:d2",
        "completed_bet:b1", "completed_bet:b2",
    ]
    # 2 条来自 legacy bet slot 的归一化，可用于审计与排障。
    assert result.legacy_normalized_count == 2
    assert result.unknown_skipped_count == 0


def test_read_material_refs_accepts_canonical_completed_bet_envelope() -> None:
    """新 envelope 直接给规范前缀；legacy_normalized_count 必须为 0。"""
    snapshot = {
        "completed_bet_items": [{"id": "b1"}],
        "diary_items": [{"id": "d1"}],
    }
    result = read_material_refs_from_envelope(snapshot)
    assert result.source_refs == ["diary:d1", "completed_bet:b1"]
    assert result.legacy_normalized_count == 0


def test_read_material_refs_supports_all_five_canonical_types() -> None:
    """contract 五类必须都能产出稳定 source_ref；不读正文。"""
    snapshot = {
        "diary_items": [{"id": "d"}],
        "completed_bet_items": [{"id": "b"}],
        "handbook_notes": [{"id": "h"}],
        "matured_wishes": [{"id": "w"}],
        "bucket_list_completions": [{"id": "c"}],
    }
    result = read_material_refs_from_envelope(snapshot)
    assert set(result.source_refs) == {
        "diary:d",
        "completed_bet:b",
        "handbook_note:h",
        "matured_wish:w",
        "bucket_list_completion:c",
    }
    assert result.legacy_normalized_count == 0


def test_read_material_refs_skips_unknown_optional_and_counts() -> None:
    """未知 optional material type 跳过并记录计数；不抛错、不污染 refs。"""
    snapshot = {
        "diary_items": [{"id": "d1"}],
        # unknown_optional 不在 contract 五类，应整体跳过。
        "unknown_optional": [{"id": "x1"}, {"id": "x2"}],
        # 已识别字段但条目缺 id 也算未知跳过。
        "handbook_notes": [{"content": "no id"}, "not-a-mapping"],
    }
    result = read_material_refs_from_envelope(snapshot)
    assert result.source_refs == ["diary:d1"]
    # 2 条 unknown_optional + 2 条无效 handbook_notes = 4
    assert result.unknown_skipped_count == 4


def test_read_material_refs_fails_closed_on_mixing() -> None:
    """新旧 envelope 字段混用时 reader 直接 fail closed。"""
    with pytest.raises(LegacyEnvelopeError):
        read_material_refs_from_envelope({"bet_items": [], "completed_bet_items": []})


def test_assert_snapshot_schema_major_rejects_unknown_major() -> None:
    """未知 schema major 显式拒绝；日期/选日规则不进入 Runtime。"""
    assert_snapshot_schema_major(SUPPORTED_SNAPSHOT_SCHEMA_MAJOR)  # 1 通过
    with pytest.raises(SnapshotSchemaError):
        assert_snapshot_schema_major(2)
    with pytest.raises(SnapshotSchemaError):
        assert_snapshot_schema_major(0)
    with pytest.raises(SnapshotSchemaError):
        assert_snapshot_schema_major("1")  # type: ignore[arg-type]
    with pytest.raises(SnapshotSchemaError):
        assert_snapshot_schema_major(None)
