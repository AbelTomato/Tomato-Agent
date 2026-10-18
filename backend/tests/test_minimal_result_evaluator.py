from app.evals.minimal_result import (
    EvaluationInput,
    EvidenceObservation,
    MinimalResultEvaluator,
    TaskEvaluationSpec,
    ToolCallObservation,
    UiObservation,
)


def test_accepts_different_legal_tool_paths_when_result_contract_matches():
    spec = TaskEvaluationSpec(
        required_output_fields=frozenset({"answer"}),
        required_evidence_refs=frozenset({"test-log"}),
        allowed_tools=frozenset({"read_file", "run_test"}),
    )
    evaluator = MinimalResultEvaluator()

    first = evaluator.evaluate(
        spec,
        EvaluationInput(
            execution_status="completed",
            output={"answer": "ok"},
            tool_calls=(ToolCallObservation(name="read_file"), ToolCallObservation(name="run_test")),
            evidence=(EvidenceObservation(ref="test-log", kind="test_result"),),
        ),
    )
    second = evaluator.evaluate(
        spec,
        EvaluationInput(
            execution_status="completed",
            output={"answer": "ok"},
            tool_calls=(ToolCallObservation(name="run_test"), ToolCallObservation(name="read_file")),
            evidence=(EvidenceObservation(ref="test-log", kind="test_result"),),
        ),
    )

    assert first.overall.status == second.overall.status == "pass"
    assert first.task.status == second.task.status == "pass"


def test_hard_safety_violation_fails_overall_even_when_task_succeeds():
    report = MinimalResultEvaluator().evaluate(
        TaskEvaluationSpec(required_output_fields=frozenset({"answer"})),
        EvaluationInput(
            execution_status="completed",
            output={"answer": "ok"},
            safety_violations=("unauthorized_write",),
            blocking_safety_violations=("unauthorized_write",),
        ),
    )

    assert report.task.status == "pass"
    assert report.safety.blocking is True
    assert report.overall.status == "fail"


def test_ui_is_not_scored_when_task_has_no_ui_requirement():
    report = MinimalResultEvaluator().evaluate(
        TaskEvaluationSpec(),
        EvaluationInput(execution_status="completed", ui=UiObservation(applicable=False)),
    )

    assert report.ui.status == "not_applicable"
    assert report.overall.status == "pass"


def test_budget_overrun_is_reported_without_being_a_hard_protocol_failure():
    report = MinimalResultEvaluator().evaluate(
        TaskEvaluationSpec(max_tool_calls=1, max_total_tokens=10),
        EvaluationInput(
            execution_status="completed",
            tool_calls=(ToolCallObservation(name="read_file"), ToolCallObservation(name="run_test")),
            total_tokens=11,
        ),
    )

    assert report.budget.status == "fail"
    assert report.protocol.status == "pass"
    assert report.overall.status == "partial"