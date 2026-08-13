"""Metrics are the thing the whole eval rests on, so they get real tests."""

from personalagi.evals.metrics import evaluate, render_report


def test_perfect_predictions():
    pairs = [("fyi", "fyi"), ("spam", "spam"), ("needs_response", "needs_response")]
    report = evaluate(pairs)

    assert report.accuracy == 1.0
    assert report.macro_f1 == 1.0
    for metrics in report.per_class.values():
        assert metrics.precision == 1.0
        assert metrics.recall == 1.0


def test_the_degenerate_classifier_scores_well_on_accuracy():
    """The reason this harness reports per-class metrics at all.

    8 of 10 messages are promotional. A classifier that answers 'promotional'
    unconditionally gets 80% accuracy and catches zero of the mail that
    actually matters.
    """
    pairs = [("promotional", "promotional")] * 8 + [
        ("needs_response", "promotional"),
        ("fyi", "promotional"),
    ]
    report = evaluate(pairs)

    assert report.accuracy == 0.8  # looks fine
    assert report.per_class["needs_response"].recall == 0.0  # is not fine
    assert report.macro_f1 < 0.35  # and macro f1 says so


def test_precision_and_recall_are_not_symmetric():
    # 2 truly needs_response; we flag 3, catching 1 of the 2.
    pairs = [
        ("needs_response", "needs_response"),
        ("needs_response", "fyi"),
        ("fyi", "needs_response"),
        ("promotional", "needs_response"),
        ("fyi", "fyi"),
    ]
    report = evaluate(pairs)
    needs = report.per_class["needs_response"]

    assert needs.support == 2
    assert needs.predicted == 3
    assert needs.true_positives == 1
    assert needs.recall == 0.5
    assert needs.precision == 1 / 3


def test_zero_support_class_does_not_divide_by_zero():
    report = evaluate([("fyi", "fyi")], labels=["fyi", "spam"])
    spam = report.per_class["spam"]

    assert spam.support == 0
    assert spam.precision == 0.0
    assert spam.recall == 0.0
    assert spam.f1 == 0.0


def test_unseen_predicted_label_is_added_not_dropped():
    """An 'unclassified' tombstone must appear in the matrix, not vanish."""
    report = evaluate([("fyi", "unclassified")], labels=["fyi", "spam"])

    assert "unclassified" in report.labels
    assert report.matrix[("fyi", "unclassified")] == 1
    assert report.accuracy == 0.0


def test_matrix_rows_are_truth_columns_are_predictions():
    report = evaluate([("spam", "fyi")])
    assert report.matrix[("spam", "fyi")] == 1
    assert report.matrix.get(("fyi", "spam"), 0) == 0


def test_render_includes_the_needs_response_callout():
    report = evaluate([("needs_response", "fyi"), ("fyi", "fyi")])
    text = render_report(report)

    assert "needs_response recall" in text
    assert "predicted ->" in text
    assert "accuracy" in text


def test_empty_pairs_do_not_crash():
    report = evaluate([])
    assert report.total == 0
    assert report.accuracy == 0.0
