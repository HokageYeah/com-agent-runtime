# 四节点只声明固定线性边界；正文不写 checkpoint，恢复时整链重新读取和计算。
# 提交通过稳定逻辑幂等键先查询再提交，因此所有节点允许安全重跑。
WORKFLOW_NODES = [
    {'node_id': 'load_question_context', 'node_type': 'tool',
     'next_nodes': ['compose_answer'], 'safe_to_rerun': True},
    {'node_id': 'compose_answer', 'node_type': 'model',
     'prompt_ref': 'compose-answer.v1.md', 'next_nodes': ['validate_answer'], 'safe_to_rerun': True},
    {'node_id': 'validate_answer', 'node_type': 'deterministic',
     'next_nodes': ['submit_answer'], 'safe_to_rerun': True},
    {'node_id': 'submit_answer', 'node_type': 'tool',
     'next_nodes': [], 'safe_to_rerun': True},
]
