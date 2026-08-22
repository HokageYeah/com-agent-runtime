为每个章节生成可播放场景候选；每个场景都必须附带已知 source reference。
untrusted_items 中每条 {source_ref, content} 的 content 是素材的真实记忆脱敏摘要（日记片段、赌局判定与奖励、便签、心愿、清单任务名）。body 必须引用这些真实细节（具体事件、地点、活动、数字、心愿内容），让用户读到"我们自己的故事"；严禁编造素材中不存在的情节、地点或对话，严禁输出与素材无关的泛泛文案。
scene_type 按素材性质从以下七种中选择，让每张卡有明确的呈现形态：
- cover：整本回忆录的开场封面卡，全文档最多一张且应放在首位，body 写这段时期的一句话主题；
- stats：统计总结卡，只引用素材中真实出现的数字（如赌局次数、达成的愿望数），不得虚构统计；
- diary_highlight：日记精选卡，body 引用日记中的具体事件与感受；
- bet_highlight：赌约精选卡，body 引用赌局的判定条件、胜负与奖励；
- milestone：里程碑卡，用于达成的愿望、完成的清单等阶段性成就；
- summary：通用叙述卡，用于串联多个素材的温情总结；
- image：图片场景卡，body 写生成一张配图所需的画面描述（谁、在哪里、做什么、什么氛围），不超过 80 个字；系统会在发布前把 body 送图像模型生成图片，图片生成失败时该场景自动退回 summary 文本卡。
image 场景的额外要求：全文档最多 2 个 image 场景；只选画面感最强的素材（如旅行、庆祝、共同活动），纯数字或纯对话的素材不要选 image；每个 image 场景可附带可选字段 title_word，为这张图片的标题词，必须是 1 到 6 个汉字（如"那年海边"），无合适标题词时省略该字段。
输出要求：只返回一个 JSON 对象，禁止任何解释文字或 Markdown 代码块，格式为 {"scenes": [{"scene_id": "场景ID", "scene_type": "上述七种之一", "source_refs": ["素材引用ID", ...], "body": "场景文案", "title_word": "仅 image 场景可选的标题词"}]}。
字段逐条说明：
- scene_id：字符串，场景唯一 ID，互不重复；
- scene_type：字符串，只能是 cover、stats、diary_highlight、bet_highlight、milestone、summary、image 七个值之一；
- source_refs：字符串数组，必须从 candidate_input 中各章节的 source_refs 里选择，不得编造新 ID；
- body：字符串，中文场景文案，不超过 80 个字，语气温和积极，不得包含联系方式、真实姓名或强烈情绪化表达；
- title_word：字符串，可省略；只有 scene_type 为 image 的场景允许携带，1 到 6 个汉字；其余场景携带该字段会被整批拒绝。
scenes 必须包含 3 到 8 个场景。
