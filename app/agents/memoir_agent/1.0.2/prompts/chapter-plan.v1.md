根据已验证的高光生成章节计划；所有引用必须来自允许的素材 ID。
输出要求：只返回一个 JSON 对象，禁止任何解释文字或 Markdown 代码块，格式为 {"chapters": [{"chapter_id": "章节ID", "source_refs": ["素材引用ID", ...], "kind": "memory_overview"}]}。
chapters 必须包含 1 到 3 个章节，chapter_id 互不重复；每个章节的 source_refs 从 candidate_input.source_refs 中选择且最多 8 个，不得编造新 ID。
