根据已验证的高光生成章节计划；所有引用必须来自允许的素材 ID。
untrusted_items 中每条 {source_ref, content} 的 content 是素材的真实记忆脱敏摘要。你要按真实内容的主题脉络分组（如共同出行、日常陪伴、约定与心愿、挑战与成就），让每个章节有具体故事可讲；不要按素材类型机械分组，也不要产出"日常生活"这类无具体内容的章节。
输出要求：只返回一个 JSON 对象，禁止任何解释文字或 Markdown 代码块，格式为 {"chapters": [{"chapter_id": "章节ID", "source_refs": ["素材引用ID", ...], "kind": "memory_overview"}]}。
chapters 必须包含 1 到 3 个章节，chapter_id 互不重复；每个章节的 source_refs 从 candidate_input.source_refs 中选择且最多 8 个，不得编造新 ID。
