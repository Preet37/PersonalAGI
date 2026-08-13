"""Classification metrics. Pure functions — no I/O, no model, no database.

Why per-class and not just accuracy: this inbox is roughly 80% automated
mail. A classifier that answers "promotional" for everything scores well on
accuracy and is useless. The number that matters is recall on
`needs_response` — the mail that changes what the day looks like — and
accuracy actively hides it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class ClassMetrics:
    label: str
    support: int  # how many truly belong to this class
    predicted: int  # how many were predicted as this class
    true_positives: int

    @property
    def precision(self) -> float:
        """Of what we called X, how much was X."""
        return self.true_positives / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        """Of what really was X, how much did we catch."""
        return self.true_positives / self.support if self.support else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class EvalReport:
    labels: list[str]
    matrix: dict[tuple[str, str], int]  # (true, predicted) -> count
    per_class: dict[str, ClassMetrics]
    total: int
    correct: int

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def macro_f1(self) -> float:
        """Unweighted mean, so rare classes count as much as common ones."""
        scored = [m for m in self.per_class.values() if m.support or m.predicted]
        return sum(m.f1 for m in scored) / len(scored) if scored else 0.0


def evaluate(pairs: list[tuple[str, str]], labels: list[str] | None = None) -> EvalReport:
    """Build a report from (true, predicted) pairs."""
    observed = sorted({label for pair in pairs for label in pair})
    all_labels = labels or observed
    for label in observed:
        if label not in all_labels:
            all_labels = [*all_labels, label]

    matrix: dict[tuple[str, str], int] = {
        (t, p): 0 for t in all_labels for p in all_labels
    }
    for true, pred in pairs:
        matrix[(true, pred)] = matrix.get((true, pred), 0) + 1

    truth_counts = Counter(t for t, _ in pairs)
    pred_counts = Counter(p for _, p in pairs)

    per_class = {
        label: ClassMetrics(
            label=label,
            support=truth_counts.get(label, 0),
            predicted=pred_counts.get(label, 0),
            true_positives=matrix.get((label, label), 0),
        )
        for label in all_labels
    }

    correct = sum(1 for t, p in pairs if t == p)
    return EvalReport(
        labels=all_labels,
        matrix=matrix,
        per_class=per_class,
        total=len(pairs),
        correct=correct,
    )


def _short(label: str, width: int) -> str:
    return label if len(label) <= width else label[: width - 1] + "."


def render_confusion_matrix(report: EvalReport) -> str:
    """Rows are truth, columns are predictions. The diagonal is correct."""
    labels = report.labels
    col_width = max(8, *(len(_short(x, 12)) + 2 for x in labels))
    row_width = max(len(x) for x in labels) + 2

    header = " " * row_width + "".join(_short(x, 12).rjust(col_width) for x in labels)
    lines = [
        "                     predicted ->",
        header,
        " " * row_width + "-" * (col_width * len(labels)),
    ]
    for true in labels:
        cells = ""
        for pred in labels:
            count = report.matrix.get((true, pred), 0)
            cell = str(count) if count else "."
            # Mark the diagonal so correct predictions read at a glance.
            if true == pred and count:
                cell = f"[{count}]"
            cells += cell.rjust(col_width)
        lines.append(true.ljust(row_width) + cells)
    return "\n".join(lines)


def render_report(report: EvalReport, *, title: str = "Classification eval") -> str:
    lines = [
        f"{title}",
        "=" * len(title),
        "",
        render_confusion_matrix(report),
        "",
        f"{'class':<16}{'prec':>8}{'recall':>8}{'f1':>8}{'support':>9}{'pred':>7}",
        "-" * 56,
    ]
    for label in report.labels:
        m = report.per_class[label]
        lines.append(
            f"{label:<16}{m.precision:>8.2f}{m.recall:>8.2f}{m.f1:>8.2f}"
            f"{m.support:>9}{m.predicted:>7}"
        )
    lines += [
        "-" * 56,
        f"{'accuracy':<16}{report.accuracy:>8.2f}   ({report.correct}/{report.total})",
        f"{'macro f1':<16}{report.macro_f1:>8.2f}",
    ]

    needs = report.per_class.get("needs_response")
    if needs:
        lines += [
            "",
            "The number that matters:",
            f"  needs_response recall    {needs.recall:>6.2f}  "
            f"({needs.true_positives}/{needs.support} caught)",
            f"  needs_response precision {needs.precision:>6.2f}  "
            f"(of {needs.predicted} flagged)",
            "",
            "  Recall is the cost of a MISSED reply. Precision is the cost of",
            "  noise in your brief. Accuracy hides both - this inbox is mostly",
            "  automated mail, so 'always promotional' would score well.",
        ]
    return "\n".join(lines)
