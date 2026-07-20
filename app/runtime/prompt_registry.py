"""版本化 Prompt 的只读注册表；调用方永远不能请求 latest 或写入模板正文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class PromptRegistryError(ValueError):
    """Prompt 清单或精确版本不可信时的安全拒绝。"""


@dataclass(frozen=True)
class PromptDefinition:
    """可信 Prompt 元数据；模板只在进程内使用，不进入日志或 usage 账本。"""

    prompt_id: str
    version: str
    owner_agent: str
    input_schema: str
    output_schema: str
    model_policy: str
    guardrail_policy: str
    status: str
    template: str


class PromptRegistry:
    """从部署内 AgentPackage 读取显式 manifest，不提供模糊匹配或版本回退。"""

    _REQUIRED = {
        "prompt_id", "version", "file", "owner_agent", "input_schema",
        "output_schema", "model_policy", "guardrail_policy", "status",
    }

    def __init__(self, package_root: Path) -> None:
        self._package_root = package_root.resolve()

    def load(
        self, agent_id: str, agent_version: str, prompt_id: str, version: str,
    ) -> PromptDefinition:
        """仅加载 ``prompt_id@version``；缺失、停用和越界一律拒绝。"""
        if not all(isinstance(value, str) and value and value != "latest" for value in (
            agent_id, agent_version, prompt_id, version,
        )):
            raise PromptRegistryError("Prompt 必须精确指定 id 与 version，禁止 latest")
        prompts_dir = (self._package_root / agent_id / agent_version / "prompts").resolve()
        if self._package_root not in prompts_dir.parents:
            raise PromptRegistryError("Prompt 路径越界")
        manifest_path = prompts_dir / "manifest.yaml"
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PromptRegistryError("Prompt manifest 不可读取") from exc
        entries = raw.get("prompts") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise PromptRegistryError("Prompt manifest 格式无效")
        entry = next((item for item in entries if isinstance(item, dict)
                      and item.get("prompt_id") == prompt_id and item.get("version") == version), None)
        if not isinstance(entry, dict):
            raise PromptRegistryError("Prompt 精确版本不存在")
        if set(entry) != self._REQUIRED or any(not isinstance(entry[key], str) or not entry[key] for key in self._REQUIRED):
            raise PromptRegistryError("Prompt manifest 字段不完整")
        if entry["owner_agent"] != agent_id or entry["status"] != "active":
            raise PromptRegistryError("Prompt owner 或状态不可执行")
        file_path = (prompts_dir / entry["file"]).resolve()
        if prompts_dir not in file_path.parents or file_path.suffix != ".md":
            raise PromptRegistryError("Prompt 文件路径非法")
        try:
            template = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptRegistryError("Prompt 模板不存在") from exc
        return PromptDefinition(template=template, **{key: entry[key] for key in self._REQUIRED - {"file"}})
