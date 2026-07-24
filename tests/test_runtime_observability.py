from app.runtime.observability import RuntimeObservabilityReport


def test_observability_report_contains_only_safe_aggregate_counts() -> None:
    report = RuntimeObservabilityReport.from_counts(
        evaluations=4, evaluation_passed=3, fallbacks=1, model_cost=1.2,
        reserved_cost=0.4, unknown_outcomes=2, tool_calls=5,
        actual_model_cost=0.8, execution_attempts=2, material_reference_passed=3,
        hallucinations=1,
    )

    assert report.as_dict() == {
        "evaluation_count": 4, "evaluation_pass_rate": 0.75, "fallback_count": 1,
        "fallback_rate": 0.25, "model_cost": 0.8, "actual_model_cost": 0.8, "reserved_cost": 0.4,
        "unknown_outcome_count": 2,
        "tool_call_count": 5, "model_attempt_count": 0,
        "execution_attempt_count": 2, "aborted_before_send_count": 0, "model_elapsed_ms": 0,
        "tool_elapsed_ms": 0, "active_elapsed_ms": 0,
        "schema_pass_rate": 0.0, "grounding_pass_rate": 0.0,
        "material_reference_pass_rate": 0.75, "hallucination_rate": 0.25,
        "emotional_safety_pass_rate": 0.0,
    }
