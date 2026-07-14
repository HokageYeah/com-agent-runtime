# 复核结论

确认修复。上一轮列出的5个问题在这一轮全部被准确修复，且修复方式都是"对症下药"而不是简单打补丁：

| 问题修复情况 | 描述 |
| :--- | :--- |
| **max_tool_calls 12/20 数值矛盾** ✅ | 需求设计文档 5.1 的 agent.yaml 示例已改为 20，与 11.3 参数表、15号文档、总控计划完全一致。 |
| **agent.yaml 示例字段不完整** ✅ | 已补齐 max_auto_retry_per_step、max_manual_run_retry_count、stop_when，与 15 号文档示例对齐。 |
| **AgentDefinition 落库同步动作缺失** ✅ | 后端开发计划 Task 3 新增明确子项："AgentPackageService.load() 成功后 upsert agent_definitions，保存 agent_id/version/runtime_type/status/definition_json/package_hash/updated_at"，并补了"重复加载同一版本会更新而非新建"的测试项。 |
| **队列技术选型悬而未决** ✅ | 需求设计文档、总控计划、后端计划三处口径统一收敛为"第一版固定用 Arq"，并明确写出"RQ 和 Celery 不进入第一版范围"，不再留二选一的开放决策。 |
| **Prompt 文件清单不一致** ✅ | 需求设计文档 5 节目录已补上 action-generate.v1.md，与后端计划工程结构一致。 |

到这一版为止，三份文档与四轮迭代出来的 15 份设计文档在契约层面(状态机、event_seq 全局单调递增、HMAC 签名含创建请求方向、幂等 TTL 与冲突码、双重脱敏边界、对账补偿参数、重试计数隔离、model_policy 映射、SSE 方案 A)完全对齐，没有出现方向性偏离，可以视为可执行的最终版。

## 本轮修复顺带带出的一个极小遗漏(不影响开发启动，建议顺手补)

Task 3 新增的描述里提到 upsert agent_definitions 时要保存 package_hash 字段，但后端开发计划第 4 节"数据模型"里 agent_definitions 表的 DDL 草图并没有这个字段：

```sql
### `agent_definitions`
id bigint pk
agent_id varchar(80) not null
version varchar(40) not null
runtime_type varchar(24) not null
definition_json json not null
status varchar(24) not null
created_at datetime not null
updated_at datetime not null
unique(agent_id, version)
```

建议在这张表定义里补一行 `package_hash varchar(64) nullable`，否则 Task 3 的执行者会发现文字描述里要求写入的字段在表结构里不存在，需要临时决定要不要加这个字段。这是这次修复动作本身派生出的一个新的、非常小的文档内部一致性缺口，顺手改掉即可，不需要专门再走一轮评审。

## 总体判断
这套需求设计文档和两份开发计划已经具备直接排期开发的成熟度，四轮设计评审沉淀下来的所有关键契约都被完整、准确地转化成了可执行任务。除了上面这一处一句话就能补齐的表字段遗漏，不再需要额外优化。