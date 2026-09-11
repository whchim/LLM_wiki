import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import eval_sales_state


def test_synthetic_dataset_has_expected_shape():
    cases = eval_sales_state.build_cases()
    assert len(cases) == 36
    assert {case.kind for case in cases} == {"normal", "prompt_injection", "numeric_unprotected", "future_time"}


def test_synthetic_evaluation_is_repeatable_and_blocks_risky_inputs():
    first = eval_sales_state.evaluate()
    second = eval_sales_state.evaluate()
    assert first == second
    assert first["dataset"]["name"] == "synthetic_sales_state_v1"
    assert first["metrics"]["blocked_by_preprocess"] > 0
    assert first["metrics"]["agent_contract_valid"] == 1
    assert first["failures"] == []


def test_dataset_can_be_written_as_jsonl(tmp_path):
    output = tmp_path / "synthetic.jsonl"
    cases = eval_sales_state.build_cases()
    output.write_text("\n".join(json.dumps(case.__dict__, ensure_ascii=False) for case in cases), encoding="utf-8")
    assert len(output.read_text(encoding="utf-8").splitlines()) == 36
