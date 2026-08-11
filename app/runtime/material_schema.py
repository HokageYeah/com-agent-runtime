"""五类素材规范前缀与旧 envelope 的单向归一化。

本模块是 Runtime 与上游 snapshot envelope 之间的**唯一**素材前缀处理点：

- 规范前缀（contract v1.0.0 冻结的 ``material_types``）：``diary``、
  ``completed_bet``、``handbook_note``、``matured_wish``、``bucket_list_completion``。
- 旧前缀 ``bet:`` 经 legacy reader 单向归一化为 ``completed_bet:``，永远
  不会回写到新 provider。
- 新旧 envelope 字段（``bet_items`` 与 ``completed_bet_items``）同时出现时
  fail closed，避免双计数和下游 allowlist 漂移。
- snapshot envelope ``schema_major`` 必须为 1；其他值显式拒绝，业务日期/选日
  等规则不进入 Runtime。

模块只产出稳定 source_ref 与计数，绝不读取也不返回五类素材正文。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# contract v1.0.0 tool_contract.material_types 的运行期镜像；fixture 与本常量
# 必须保持一致，否则跨项目冻结契约会破裂。
CANONICAL_MATERIAL_PREFIXES = frozenset(
    {
        "diary",
        "completed_bet",
        "handbook_note",
        "matured_wish",
        "bucket_list_completion",
    }
)

# 旧前缀 → 规范前缀的单向映射；只有 legacy reader 边界允许做这次转换。
LEGACY_PREFIX_TO_CANONICAL = {"bet": "completed_bet"}

# snapshot envelope schema_major 唯一受支持版本；新 major 必须先扩本表与兼容策略。
SUPPORTED_SNAPSHOT_SCHEMA_MAJOR = 1


class LegacyEnvelopeError(RuntimeError):
    """旧 envelope 字段与规范字段同时出现时由 legacy reader fail closed。"""


class SnapshotSchemaError(RuntimeError):
    """snapshot envelope schema_major 不被 Runtime 支持时显式拒绝。"""


def assert_snapshot_schema_major(schema_major: object) -> None:
    """拒绝未知 schema_major；日期/7+7/60/选日等业务规则永不进入 Runtime。"""
    if (
        not isinstance(schema_major, int)
        or schema_major != SUPPORTED_SNAPSHOT_SCHEMA_MAJOR
    ):
        raise SnapshotSchemaError("MEMORY_SNAPSHOT_SCHEMA_UNSUPPORTED")


def normalize_source_ref(ref: str) -> str:
    """``bet:<id>`` 单向归一化为 ``completed_bet:<id>``；其他原样返回。

    非 ``prefix:id`` 形态或不在 legacy 表中的前缀不做任何转换，交由调用方
    的 allowlist/semantic validator 拦截；本函数只承担 legacy 兼容这一件事。
    """
    prefix, sep, identifier = ref.partition(":")
    if not sep or prefix not in LEGACY_PREFIX_TO_CANONICAL:
        return ref
    return f"{LEGACY_PREFIX_TO_CANONICAL[prefix]}:{identifier}"


@dataclass(frozen=True)
class EnvelopeReadResult:
    """legacy reader 不读取正文，只产出规范 source_ref 与归一化计数。

    - ``source_refs``：按 envelope 顺序产出的规范前缀引用，调用方再次写入
      allowlist / Scene 时仍要过 semantic validator 白名单。
    - ``legacy_normalized_count``：本次由 legacy 字段（``bet_items`` / ``bets``）
      归一化得到的条目数；用于审计与排障，不携带素材正文。
    - ``unknown_skipped_count``：未在 contract 五类中识别的 optional 素材被
      显式跳过的次数；规格要求“未知 optional material type 跳过并记录计数”。
    """

    source_refs: list[str]
    legacy_normalized_count: int
    unknown_skipped_count: int


# envelope 字段名 → 规范前缀；legacy 字段标记为 ``is_legacy`` 用于归一化计数。
# 字段名仅在本表声明一次，避免散落判断；新加素材类型必须同时更新本表与 contract。
_ENVELOPE_SLOTS: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (("diary_items", "diaries"), "diary", False),
    (("completed_bet_items", "completed_bets"), "completed_bet", False),
    # 旧 bet_items/bets 经 legacy reader 单向归一化为 completed_bet 前缀。
    (("bet_items", "bets"), "completed_bet", True),
    (("handbook_notes",), "handbook_note", False),
    (("matured_wishes",), "matured_wish", False),
    (("bucket_list_completions",), "bucket_list_completion", False),
)

# 旧 bet envelope 字段集合，用于和规范 completed_bet 字段做混用 fail closed。
_LEGACY_BET_FIELDS = frozenset({"bet_items", "bets"})
_CANONICAL_BET_FIELDS = frozenset({"completed_bet_items", "completed_bets"})


def detect_envelope_mixing(snapshot: Mapping[str, object]) -> None:
    """新旧 bet envelope 字段同时出现时 fail closed。

    混用会导致同一份素材被双计数、allowlist 漂移，比拒绝更危险；因此本检测
    在 legacy reader 入口执行，任何后续步骤都不再重复判断。
    """
    if any(field in snapshot for field in _LEGACY_BET_FIELDS) and any(
        field in snapshot for field in _CANONICAL_BET_FIELDS
    ):
        raise LegacyEnvelopeError("LEGACY_ENVELOPE_MIXED_WITH_CANONICAL")


def read_material_refs_from_envelope(
    snapshot: Mapping[str, object],
) -> EnvelopeReadResult:
    """从 snapshot envelope 产出规范 source_ref；不向 provider 回写 legacy 形状。

    本函数是 Runtime 唯一允许消费 ``bet_items/bets`` 的入口：

    - 调用 :func:`detect_envelope_mixing` 拒绝新旧字段同时出现；
    - 按 :data:`_ENVELOPE_SLOTS` 顺序读取每类素材的 ``id``，产出规范前缀引用；
    - 未识别的 optional 素材类型（不在 contract 五类）只跳过并计数，不抛错；
    - 不读取素材正文，``content`` / ``summary`` 等字段由 Runner 在脱敏边界
      单独处理；本函数只负责稳定 source_ref。
    """
    detect_envelope_mixing(snapshot)
    refs: list[str] = []
    legacy_normalized = 0
    unknown_skipped = 0

    # 按 _ENVELOPE_SLOTS 顺序读取已识别 slot；保证 source_ref 顺序稳定，不依赖
    # envelope 顶层 dict 的插入顺序。
    for fields, prefix, is_legacy in _ENVELOPE_SLOTS:
        items = next((snapshot[field] for field in fields if field in snapshot), None)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"]:
                refs.append(f"{prefix}:{item['id']}")
                if is_legacy:
                    legacy_normalized += 1
            else:
                unknown_skipped += 1

    # 未知 optional material type：envelope 顶层 list 字段不在五类 slot 表里，
    # 视为未识别素材类型，整体跳过并记录条数（规格要求"跳过并记录计数"）。
    recognized_field_names = {field for fields, _, _ in _ENVELOPE_SLOTS for field in fields}
    for key, value in snapshot.items():
        if key in recognized_field_names or not isinstance(value, list):
            continue
        unknown_skipped += len(value)

    return EnvelopeReadResult(refs, legacy_normalized, unknown_skipped)
