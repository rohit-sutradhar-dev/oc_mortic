from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opencode_voice.response_contract import ResponseCase, evaluate_response


@dataclass(frozen=True)
class RegradedTrial:
    case_id: str
    trial: int
    classification: str
    old_passed: bool
    new_passed: bool
    old_codes: tuple[str, ...]
    new_codes: tuple[str, ...]


def regrade_baseline(
    baseline_dir: Path,
    cases: list[ResponseCase],
    *,
    output_root: Path = Path("runs/response-evals/comparisons"),
) -> Path:
    """Regrade an immutable JSONL run and write a separate comparison report."""
    source = baseline_dir / "trials.jsonl"
    if not source.is_file():
        raise FileNotFoundError(source)
    case_map = {case.case_id: case for case in cases}
    results: list[RegradedTrial] = []
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            case_id = str(raw.get("case_id") or raw.get("caseId") or "")
            case = case_map.get(case_id)
            if case is None:
                continue
            # Regrade the response the historical harness actually selected;
            # first/repair candidates remain immutable evidence in the source run.
            candidate = raw.get("response") or raw.get("first_response")
            evaluation = evaluate_response(candidate, case)
            old_violations = raw.get("first_pass_violations") or raw.get("final_violations") or []
            old_codes = tuple(str(item.get("code")) for item in old_violations if isinstance(item, dict))
            old_passed = bool(raw.get("passed", not old_codes and not raw.get("error")))
            new_codes = tuple(item.code for item in evaluation.violations)
            new_passed = not new_codes and not raw.get("error")
            results.append(
                RegradedTrial(
                    case_id=case_id,
                    trial=int(raw.get("trial") or 0),
                    classification=_classification(old_passed, new_passed, evaluation.violations),
                    old_passed=old_passed,
                    new_passed=new_passed,
                    old_codes=old_codes,
                    new_codes=new_codes,
                )
            )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = output_root / stamp
    output.mkdir(parents=True, exist_ok=False)
    with (output / "regraded.jsonl").open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
    counts = {
        name: sum(item.classification == name for item in results)
        for name in sorted({item.classification for item in results})
    }
    manifest: dict[str, Any] = {
        "schema": "mortic.response-eval-comparison.v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "source": str(baseline_dir),
        "sourceModified": False,
        "trials": len(results),
        "classifications": counts,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    lines = ["# Response evaluation regrade", "", f"Source: `{baseline_dir}`", ""]
    lines.extend(f"- {key}: {value}" for key, value in counts.items())
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def _classification(old_passed: bool, new_passed: bool, violations: tuple[Any, ...]) -> str:
    if old_passed and new_passed:
        return "unchanged_pass"
    if not old_passed and new_passed:
        return "evaluator_false_positive"
    if old_passed and not new_passed:
        return "new_gate_regression"
    if any(item.gate == "safety" or item.code in {"contradiction", "forbidden_pattern"} for item in violations):
        return "genuine_response_failure"
    return "indeterminate"
