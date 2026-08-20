为每个章节生成可播放场景候选；每个场景都必须附带已知 source reference。
untrusted_items 中每条 {source_ref, content} 的 content 是素材的真实记忆脱敏摘要（日记片段、赌局判定与奖励、便签、心愿、清单任务名）。body 必须引用这些真实细节（具体事件、地点、活动、数字、心愿内容），让用户读到"我们自己的故事"；严禁编造素材中不存在的情节、地点或对话，严禁输出与素材无关的泛泛文案。
scene_type 按素材性质从以下六种中选择，让每张卡有明确的呈现形态：
- cover：整本回忆录的开场封面卡，全文档最多一张且应放在首位，body 写这段时期的一句话主题；
- stats：统计总结卡，只引用素材中真实出现的数字（如赌局次数、达成的愿望数），不得虚构统计；
- diary_highlight：日记精选卡，body 引用日记中的具体事件与感受；
- bet_highlight：赌约精选卡，body 引用赌局的判定条件、胜负与奖励；
- milestone：里程碑卡，用于达成的愿望、完成的清单等阶段性成就；
- summary：通用叙述卡，用于串联多个素材的温情总结。
输出要求：只返回一个 JSON 对象，禁止任何解释文字或 Markdown 代码块，格式为 {"scenes": [{"scene_id": "场景ID", "scene_type": "上述六种之一", "source_refs": ["素材引用ID", ...], "body": "场景文案"}]}。
scenes 必须包含 3 到 8 个场景，scene_id 互不重复；source_refs 必须从 candidate_input 中各章节的 source_refs 里选择，不得编造新 ID。
body 为中文场景文案，不超过 80 个字，语气温和积极，不得包含联系方式、真实姓名或强烈情绪化表达。
