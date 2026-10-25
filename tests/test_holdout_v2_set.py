"""R2 DataCrew 留出集 v2 的自检（不连库）：规模/分布/唯一性/金标形态。"""
from __future__ import annotations

from collections import Counter

from eval.holdout_set import HOLDOUT, HOLDOUT_V2


def test_留出集v2三十题与类别分布():
    assert len(HOLDOUT_V2) == 30
    assert Counter(item[1] for item in HOLDOUT_V2) == {
        "unanswerable": 8,
        "should_refuse": 8,
        "ambiguous": 6,
        "time_range": 4,
        "simple_agg": 2,
        "multi_join": 2,
    }


def test_题目不重复且金标只读():
    questions = [item[0] for item in HOLDOUT_V2]
    assert len(set(questions)) == len(questions)
    for question, _category, gold_sql, _notes in HOLDOUT_V2:
        assert gold_sql.lstrip().upper().startswith("SELECT"), question
        assert ";" not in gold_sql.strip().rstrip(";"), question


def test_与v1题面不重复():
    assert not ({item[0] for item in HOLDOUT} & {item[0] for item in HOLDOUT_V2})
