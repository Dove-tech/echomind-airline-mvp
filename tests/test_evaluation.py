"""设计 §24 对应的回归评测框架验收测试。"""

from airline_mvp.evaluation import run_offline_evaluation


def test_checked_in_eval_dataset_passes(tmp_path) -> None:
    report = run_offline_evaluation(runtime_root=tmp_path)
    assert report["summary"]["caseCount"] >= 6
    assert report["summary"]["passRate"] == 1.0
    assert all(value == 1.0 for value in report["metrics"].values())
