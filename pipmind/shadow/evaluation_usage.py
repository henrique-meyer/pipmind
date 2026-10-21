"""Pure accounting for retained evaluation traces, without inferring provider bills."""

ACCOUNTING_VERSION = "shadow-usage-v1"
PHASES = ("triage", "decision", "memory_repair", "other")
COUNTERS = (
    "trace_records",
    "calls",
    "input_tokens",
    "output_tokens",
    "unknown_usage_calls",
    "missing_usage_calls",
    "partially_missing_usage_calls",
    "unconfirmed_attempt_calls",
    "non_rpc_traces",
    "synthetic_traces",
    "zero_rpc_repairs",
)


def _trace_counts(trace):
    counts = dict.fromkeys(COUNTERS, 0)
    counts["trace_records"] = 1
    # Categories partition the trace records. A scripted trace is never provider
    # evidence, even if it also carries attempted=False or invented token counts.
    if trace.get("synthetic") is True:
        counts["synthetic_traces"] = 1
        return counts
    if trace.get("attempted") is False:
        counts["non_rpc_traces"] = 1
        counts["zero_rpc_repairs"] = int(
            trace.get("phase") == "memory_repair" and trace.get("method") == "punctuation_spacing"
        )
        return counts
    # Legacy/invalid flags cannot prove a call did not happen. Retain that
    # conservative assumption explicitly; an attempt does not prove execution.
    counts["calls"] = 1
    counts["unconfirmed_attempt_calls"] = int(trace.get("attempted") is not True)
    known = []
    for side in ("input", "output"):
        value = trace.get(side + "_tokens")
        valid = type(value) is int and value >= 0
        known.append(valid)
        if valid:
            counts[side + "_tokens"] = value
    counts["unknown_usage_calls"] = int(bool(trace.get("usage_unknown")) or not all(known))
    counts["missing_usage_calls"] = int(not all(known))
    counts["partially_missing_usage_calls"] = int(known[0] != known[1])
    return counts


def summarize_usage(traces):
    """Count attempts and known-token lower bounds without altering raw traces.

    Explicit synthetic traces take precedence over explicit non-RPC traces.
    Missing/invalid attempt flags are counted conservatively and marked
    unconfirmed. Unknown phases share one fixed bucket to keep summaries bounded.
    """
    total = dict.fromkeys(COUNTERS, 0)
    phases = {phase: dict.fromkeys(COUNTERS, 0) for phase in PHASES}
    for trace in traces:
        if not isinstance(trace, dict):
            raise ValueError("evaluation_usage_trace")
        phase = trace.get("phase")
        if not isinstance(phase, str) or phase not in PHASES:
            phase = "other"
        counts = _trace_counts(trace)
        for key, value in counts.items():
            total[key] += value
            phases[phase][key] += value
    return {
        "usage_accounting_version": ACCOUNTING_VERSION,
        "usage_scope": (
            "Model calls are attempted or conservatively assumed attempts, not confirmed execution "
            "or billing; token totals are known-side lower bounds, unknown usage is not zero cost."
        ),
        "model_calls": total.pop("calls"),
        **total,
        "usage_by_phase": phases,
    }
