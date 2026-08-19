为每个章节生成可播放场景候选；每个场景都必须附带已知 source reference。
输出要求：只返回一个 JSON 对象，禁止任何解释文字或 Markdown 代码块，格式为 {"scenes": [{"scene_id": "场景ID", "scene_type": "summary", "source_refs": ["素材引用ID", ...], "body": "场景文案"}]}。
scenes 必须包含 3 到 8 个场景，scene_id 互不重复，scene_type 固定为 "summary"；source_refs 必须从 candidate_input 中各章节的 source_refs 里选择，不得编造新 ID。
body 为可选的中文场景文案，不超过 80 个字，语气温和积极，不得包含联系方式、真实姓名或强烈情绪化表达。
