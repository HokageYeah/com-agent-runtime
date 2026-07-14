## 复核结论
先说结论：这三份文档(需求设计文档、总控开发计划、后端开发计划)与四轮迭代出来的15份设计文档高度一致,没有出现方向性偏离。核心契约——状态机、event_seq全局单调递增、HMAC签名(含创建AgentRun这次也补齐了签名要求)、幂等键TTL与冲突码、双重脱敏边界、对账补偿参数、手动/自动重试计数隔离、model_policy六类映射、SSE方案A、MCP/RAG/长期记忆延后到二三期——全部被完整搬运并转化成了可执行的任务清单和表结构,可以直接进入开发。

而且这套实现文档在一处上做得比原15份设计文档更严谨:给"业务系统调用Runtime创建/查询/重试/取消AgentRun"这个方向也补上了HMAC签名要求(X-Agent-Client-Id)。这在此前四轮review中我曾关注过callback和工具调用两个方向的签名,但没意识到"创建请求"这个入站方向本身在原15份文档里其实没有强制要求签名——这次的需求设计文档主动把这个口子堵上了,是一个净提升。

## 发现的问题(均为文档内部一致性小问题,不影响整体架构方向)

1. max_tool_calls 数值在同一份文档内自相矛盾(建议优先修)
需求设计文档.md 5.1节的agent.yaml示例写的是:
```yaml
policy:
  max_tool_calls: 12
```
但同一份文档11.3节"第一版默认参数表"、以及15-业务Agent接入规范.md的agent.yaml示例、总控开发计划都统一是:
max_tool_calls: 20
这是文档内部数值冲突,建议把5.1节的示例改成20,保持与全局默认参数表和15号文档一致。

2. 需求设计文档.md 5.1节的 agent.yaml 示例字段不完整
对比15号文档给出的完整示例,需求设计文档 5.1的示例缺少 max_auto_retry_per_step、max_manual_run_retry_count、stop_when 三个字段。虽然这些字段在11.3节的参数表里有单独说明,但既然是同一个memoir_agent@1.0.0包的示例,建议两处保持逐字一致,避免开发者对着不同文档抄出两份不同的agent.yaml。

3. AgentDefinition 落库同步动作在任务清单里没有被显式覆盖
后端开发计划 Task 2 建了agent_definitions表,Task 3 实现了AgentPackageService.load()做文件加载和schema校验,但两个Task的checklist里都没有一条"AgentPackage加载成功后同步/upsert到agent_definitions表"的动作项。数据模型定义了这张表,但没有任何任务显式负责往里写数据——建议在Task 3或Task 4末尾补一条子项,比如"AgentPackageService.load 成功后 upsert agent_definitions(agent_id, version, status=active)",否则这张表可能会被实现时遗漏。

4. 队列技术选型仍是开放项,与"契约先冻结"的整体基调略有出入
后端开发计划 Task 4.5 写的是"选择第一版队列实现,默认使用 Arq 或 RQ;实现细节写进 README.md"——这是内部实现细节,不是跨服务契约,不会影响Runtime与业务后端之间的接口,所以不算严重问题。但既然整个四轮设计一直强调"先冻结契约再开发,避免漂移",队列选型这种会影响Worker启动方式、锁机制写法的技术决策,建议在开发前直接拍板一个(比如考虑到FastAPI + httpx原生异步生态,Arq会比RQ更顺手),而不是留到开发时"二选一"。

5. Prompt文件清单在两份文档间有细微出入
需求设计文档 5.1 的 prompts/ 目录只列了4个文件(highlight-extract、chapter-plan、scene-generate、safety-review),缺少 action-generate.v1.md;而后端开发计划第2节的工程目录结构里包含了这个文件。由于generate_actions第一版默认走规则生成、模型是可选路径(04号文档已明确),这个不一致影响很小,但建议统一,避免开发者疑惑这个prompt文件到底要不要建。

## 总体判断
以上5点全部是文档间/文档内的细节对齐问题,不是架构层面的方向性错误,也没有出现"跑偏"——三层边界(业务系统/Runtime/业务Agent)、运行时契约(签名、幂等、乱序保护、补偿对账)、分期策略(第一版只做Workflow Agent、二期才接客服/MCP/RAG)都被完整且正确地继承了下来。建议在正式排期开发前花很少的时间把上面5点小问题改掉(尤其是第1、3点),之后这套需求设计文档和两份开发计划就可以视为与四轮设计评审完全对齐、可直接执行的状态。