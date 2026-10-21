"""Retained trace accounting only: no SQL, SDK, model or operational entrypoint."""

from copy import deepcopy

import pytest

from pipmind.shadow.evaluation_usage import ACCOUNTING_VERSION, PHASES, summarize_usage


def test_pre_rpc_refusal_is_not_an_attempt_or_unknown_provider_usage():
    result = summarize_usage(
        [{"phase": "triage", "attempted": False, "input_tokens": None, "output_tokens": None}]
    )
    assert result["trace_records"] == result["non_rpc_traces"] == 1
    assert result["model_calls"] == result["unknown_usage_calls"] == 0
    assert result["usage_by_phase"]["triage"]["calls"] == 0


def test_explicit_unknown_retains_known_sides_and_does_not_imply_zero_cost():
    result = summarize_usage(
        [
            {
                "phase": "decision",
                "attempted": True,
                "input_tokens": 10,
                "output_tokens": 2,
                "usage_unknown": True,
            }
        ]
    )
    assert result["model_calls"] == result["unknown_usage_calls"] == 1
    assert result["input_tokens"] == 10 and result["output_tokens"] == 2
    assert result["missing_usage_calls"] == 0


def test_zero_rpc_repair_is_separate_and_repair_rpc_has_its_own_phase():
    result = summarize_usage(
        [
            {
                "phase": "memory_repair",
                "attempted": False,
                "method": "punctuation_spacing",
                "input_tokens": 0,
                "output_tokens": 0,
            },
            {
                "phase": "memory_repair",
                "attempted": True,
                "input_tokens": 3,
                "output_tokens": None,
                "usage_unknown": True,
            },
        ]
    )
    repair = result["usage_by_phase"]["memory_repair"]
    assert repair["trace_records"] == 2
    assert repair["calls"] == repair["non_rpc_traces"] == repair["zero_rpc_repairs"] == 1
    assert repair["unknown_usage_calls"] == repair["partially_missing_usage_calls"] == 1
    assert repair["input_tokens"] == result["input_tokens"] == 3


@pytest.mark.parametrize("flag", [None, 0, 1, "false", [], {}])
def test_invalid_attempt_flags_are_conservatively_counted_but_unconfirmed(flag):
    result = summarize_usage(
        [{"phase": "decision", "attempted": flag, "input_tokens": 1, "output_tokens": 0}]
    )
    assert result["model_calls"] == result["unconfirmed_attempt_calls"] == 1
    assert result["unknown_usage_calls"] == 0


def test_legacy_missing_attempt_flag_preserves_uncertainty_and_known_tokens():
    result = summarize_usage([{"phase": "decision", "input_tokens": 7, "output_tokens": None}])
    assert result["model_calls"] == result["unconfirmed_attempt_calls"] == 1
    assert result["unknown_usage_calls"] == result["partially_missing_usage_calls"] == 1
    assert result["input_tokens"] == 7


@pytest.mark.parametrize("value", [None, True, False, -1, 2.5, "4"])
def test_invalid_token_side_is_unknown_while_other_side_remains_a_lower_bound(value):
    result = summarize_usage([{"attempted": True, "input_tokens": value, "output_tokens": 4}])
    assert result["input_tokens"] == 0 and result["output_tokens"] == 4
    assert result["unknown_usage_calls"] == result["missing_usage_calls"] == 1
    assert result["partially_missing_usage_calls"] == 1


def test_missing_both_token_sides_differs_from_confirmed_zero_counts():
    result = summarize_usage(
        [{"attempted": True}, {"attempted": True, "input_tokens": 0, "output_tokens": 0}]
    )
    assert result["model_calls"] == 2
    assert result["unknown_usage_calls"] == result["missing_usage_calls"] == 1
    assert result["partially_missing_usage_calls"] == 0


@pytest.mark.parametrize("attempted", [False, True, None])
def test_synthetic_traces_do_not_become_provider_calls_or_token_cost(attempted):
    result = summarize_usage(
        [
            {
                "phase": "memory_repair",
                "attempted": attempted,
                "synthetic": True,
                "method": "punctuation_spacing",
                "input_tokens": 100,
                "output_tokens": 200,
                "usage_unknown": True,
            }
        ]
    )
    assert result["trace_records"] == result["synthetic_traces"] == 1
    assert result["model_calls"] == result["non_rpc_traces"] == result["zero_rpc_repairs"] == 0
    assert result["input_tokens"] == result["output_tokens"] == result["unknown_usage_calls"] == 0


def test_excluded_nonrpc_tokens_and_unknown_flags_do_not_leak_into_totals():
    result = summarize_usage(
        [{"attempted": False, "input_tokens": 100, "output_tokens": 200, "usage_unknown": True}]
    )
    assert result["non_rpc_traces"] == 1
    assert result["input_tokens"] == result["output_tokens"] == result["unknown_usage_calls"] == 0


@pytest.mark.parametrize("phase", [None, "unrecognized", "x" * 100, [], {}])
def test_unknown_phases_use_one_bounded_bucket(phase):
    result = summarize_usage(
        [{"phase": phase, "attempted": True, "input_tokens": 2, "output_tokens": 1}]
    )
    assert tuple(result["usage_by_phase"]) == PHASES
    assert result["usage_by_phase"]["other"]["calls"] == 1


def test_mixed_trace_totals_partition_and_equal_phase_sums_without_mutation():
    traces = [
        {"phase": "triage", "attempted": True, "input_tokens": 10, "output_tokens": 2},
        {
            "phase": "decision",
            "attempted": True,
            "input_tokens": 4,
            "output_tokens": 3,
            "usage_unknown": True,
        },
        {"phase": "memory_repair", "attempted": True, "input_tokens": 6, "output_tokens": None},
        {"phase": "triage", "attempted": False, "input_tokens": 100},
        {
            "phase": "memory_repair",
            "attempted": False,
            "method": "punctuation_spacing",
            "input_tokens": 0,
            "output_tokens": 0,
        },
        {"phase": "decision", "synthetic": True, "input_tokens": 900, "output_tokens": 900},
        {"input_tokens": 1, "output_tokens": 1},
        {"phase": "unknown", "attempted": 0, "input_tokens": None, "output_tokens": True},
    ]
    before = deepcopy(traces)
    result = summarize_usage(iter(traces))
    assert traces == before
    assert result["usage_accounting_version"] == ACCOUNTING_VERSION
    assert "not confirmed execution or billing" in result["usage_scope"]
    expected = {
        "trace_records": 8,
        "model_calls": 5,
        "input_tokens": 21,
        "output_tokens": 6,
        "unknown_usage_calls": 3,
        "missing_usage_calls": 2,
        "partially_missing_usage_calls": 1,
        "unconfirmed_attempt_calls": 2,
        "non_rpc_traces": 2,
        "synthetic_traces": 1,
        "zero_rpc_repairs": 1,
    }
    for key, count in expected.items():
        assert result[key] == count
        phase_key = "calls" if key == "model_calls" else key
        assert sum(row[phase_key] for row in result["usage_by_phase"].values()) == count
    for row in result["usage_by_phase"].values():
        assert (
            row["trace_records"] == row["calls"] + row["non_rpc_traces"] + row["synthetic_traces"]
        )


def test_empty_summary_has_bounded_zero_counters():
    result = summarize_usage([])
    assert result["trace_records"] == result["model_calls"] == result["unknown_usage_calls"] == 0
    assert tuple(result["usage_by_phase"]) == PHASES


def test_nontrace_value_refuses_instead_of_disappearing():
    with pytest.raises(ValueError, match="evaluation_usage_trace"):
        summarize_usage([None])
