WORKFLOW_NODES = [
    {'node_id': 'load_turn_context', 'node_type': 'tool',
     'next_nodes': ['compose_turn'], 'safe_to_rerun': True},
    {'node_id': 'compose_turn', 'node_type': 'model',
     'prompt_ref': 'compose-reply.v1.md', 'next_nodes': ['validate_turn'], 'safe_to_rerun': True},
    {'node_id': 'validate_turn', 'node_type': 'deterministic',
     'next_nodes': ['submit_turn_result'], 'safe_to_rerun': True},
    {'node_id': 'submit_turn_result', 'node_type': 'tool',
     'next_nodes': [], 'safe_to_rerun': True},
]
