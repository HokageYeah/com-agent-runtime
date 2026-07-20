from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.runtime.state import AgentState


def test_load_snapshot_writes_only_runtime_memory_state():
    class Gateway:
        def get_snapshot(self, connector_id, archive_id, snapshot_id, run_id, generation_epoch):
            return {"diaries": ["私密正文"]}
    run = type("Run", (), {"input_json": {"archive_id": "a", "snapshot_id": "s", "generation_epoch": 0}, "business_connector_id": "c", "run_id": "r"})()
    state = AgentState()
    assert MemoirNodeRunner(Gateway()).run_node({"node_id": "load_snapshot"}, run, state) == {"node_id": "load_snapshot", "snapshot_loaded": True}
    assert state.snapshot == {"diaries": ["私密正文"]}


def test_publish_document_writes_only_publish_result():
    class Gateway:
        def publish_playback_document(self, *args):
            return {"revision": 1, "content_digest": "digest"}
    run = type("Run", (), {"input_json": {"archive_id": "a", "snapshot_id": "s", "generation_epoch": 1}, "business_connector_id": "c", "run_id": "r"})()
    state = AgentState(playback_document={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []})
    assert MemoirNodeRunner(Gateway()).run_node({"node_id": "publish_document"}, run, state) == {"node_id": "publish_document", "published": True}
    assert state.publish_result == {"revision": 1, "content_digest": "digest"}


def test_compute_stats_uses_snapshot_counts_and_empty_fallback():
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(snapshot={"diaries": [{"content": "私密 1"}, {"content": "私密 2"}], "bets": [{"title": "私密"}]})
    assert runner.run_node({"node_id": "compute_stats"}, run, state) == {"node_id": "compute_stats", "stats_ready": True}
    assert state.stats == {"diary_count": 2, "bet_count": 1, "has_material": True}
    empty = AgentState(snapshot={})
    runner.run_node({"node_id": "compute_stats"}, run, empty)
    assert empty.stats == {"diary_count": 0, "bet_count": 0, "has_material": False}


def test_extract_highlights_uses_stable_source_ids_without_copying_content():
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(snapshot={"diaries": [{"id": "d-1", "content": "私密正文"}, {"id": "d-2", "content": "更多正文"}], "bets": [{"id": "b-1", "title": "私密赌约"}]})
    assert runner.run_node({"node_id": "extract_highlights"}, run, state) == {"node_id": "extract_highlights", "fallback": True}
    assert state.highlights == {"source_refs": ["diary:d-1", "diary:d-2", "bet:b-1"], "mode": "template"}
    assert "私密正文" not in str(state.highlights)


def test_template_chapters_scenes_and_actions_form_playable_fallback():
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        stats={"diary_count": 2, "bet_count": 1, "has_material": True},
        highlights={"source_refs": ["diary:d-1", "bet:b-1"], "mode": "template"},
    )
    assert runner.run_node({"node_id": "plan_chapters"}, run, state) == {"node_id": "plan_chapters", "fallback": True}
    assert runner.run_node({"node_id": "generate_scenes"}, run, state) == {"node_id": "generate_scenes", "fallback": True}
    assert runner.run_node({"node_id": "generate_actions"}, run, state) == {"node_id": "generate_actions", "fallback": True}
    assert state.chapter_plan == {"chapters": [{"chapter_id": "chapter-1", "source_refs": ["diary:d-1", "bet:b-1"], "kind": "memory_overview"}]}
    assert state.scenes == [{"scene_id": "scene-1", "scene_type": "summary", "source_refs": ["diary:d-1", "bet:b-1"]}]
    assert state.actions == [{"action_id": "action-1", "scene_id": "scene-1", "action_type": "show_card", "duration_ms": 3000}]


def test_safety_review_builds_complete_document_and_falls_back_for_invalid_actions():
    runner = MemoirNodeRunner(object())
    run = type("Run", (), {"run_id": "r"})()
    state = AgentState(
        scenes=[{"scene_id": "scene-1", "scene_type": "summary", "source_refs": ["diary:d-1"]}],
        actions=[{"action_id": "action-1", "scene_id": "scene-1", "action_type": "show_card", "duration_ms": 3000}],
    )
    assert runner.run_node({"node_id": "safety_review"}, run, state) == {"node_id": "safety_review", "safe": True}
    assert state.playback_document == {"schema_version": "1.0.0", "scenes": state.scenes, "actions": state.actions, "media_manifest": []}
    invalid = AgentState(scenes=[], actions=[{"scene_id": "missing", "duration_ms": -1}])
    runner.run_node({"node_id": "safety_review"}, run, invalid)
    assert invalid.safety_report == {"decision": "fallback", "reason": "INVALID_PLAYBACK_STRUCTURE"}
    assert invalid.playback_document["scenes"][0]["scene_id"] == "scene-1"
