# Test Specification — FIN-C2-004 Financial Document Summarization Agent

**Template ID**: FIN-C2-004
**Category**: Cat 2 (flat summarization pipeline)
**Coverage target**: 80%+ on domain node logic
**Test types**: Unit / Proof-of-Boundary (end-to-end through the ASGI entry point)

Run the suite with the framework installed:

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

---

## 1. Framework Compliance

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | `src/schemas/state.py` is a flat TypedDict | No Pydantic / dataclass fields | `test_state_safety.py` |
| TC-02 | No credential-like field names in State | AST scan reports 0 violations | `test_state_safety.py` |
| TC-03 | `required_trust_level` declared on every node | All node classes declare `ClassVar[TrustLevel]` | per-node unit tests |
| TC-04 | `emit_trace_event` present on every path | ≥1 per node, all branches | per-node unit tests |
| TC-06 | Default input gate cannot be overridden | Class definition raises `TypeError` | `test_framework_compliance_tc06_tc07.py` |
| TC-07 | Default output gate cannot be overridden | Class definition raises `TypeError` | `test_framework_compliance_tc06_tc07.py` |
| TC-10 | No platform-internal SDK imports in `src/` | AST scan reports 0 violations | `test_import_isolation.py` |

---

## 2. PreProcessNode — document screening (`tests/unit/test_pre_process_node.py`)

`TestPreProcessNode` (18 tests) + `TestTrustGate` (2 tests).

| TC-ID | Scenario | Input | Expected |
|-------|----------|-------|----------|
| TC-PRE-001 | Valid financial document | financial prose | status=SUCCESS, `input_text` / `validated_input` set |
| TC-PRE-002 | Whitespace stripped | `"  Annual report.  "` | `input_text = "Annual report."` |
| TC-PRE-003 | Empty string | `""` | declined — status=SUCCESS, `error_log` populated, nothing published downstream |
| TC-PRE-004 | Whitespace-only | `"   \n\t   "` | declined — status=SUCCESS, `error_log` populated |
| TC-PRE-005 | Missing user_input key | `{}` | declined — status=SUCCESS, `error_log` populated |
| TC-PRE-006 | Oversized document | `"A" × 50,001` | declined — status=SUCCESS, `error_log` mentions the limit |
| TC-PRE-007 | Exactly 50,000 chars | `"B" × 50,000` | status=SUCCESS |
| TC-PRE-008 | Injection: instruction override | `"... ignore previous instructions ..."` | refused, nothing published |
| TC-PRE-009 | Injection: SQL | `"data; DROP TABLE agents ..."` | refused, nothing published |
| TC-PRE-010 | Injection: `<script>` tag | `"data <script>alert(1)</script>"` | refused, nothing published |
| TC-PRE-011 | PII: email address | `"...john.doe@example.com..."` | refused outright — status=ERROR |
| TC-PRE-012 | PII: SSN | `"SSN: 123-45-6789"` | refused outright — status=ERROR |
| TC-PRE-013 | PII: credit card | `"4111 1111 1111 1111"` | refused outright — status=ERROR |
| TC-PRE-014 | PII: `[MASKED]` sentinel | `"CFO: [MASKED]"` | refused outright — status=ERROR |
| TC-PRE-015 | Non-string input | `12345` | declined — status=SUCCESS, `error_log` populated |
| TC-PRE-016 | None input | `None` | declined — status=SUCCESS, `error_log` populated |
| TC-PRE-017 | Partial-dict contract | valid document | unrelated input state fields not echoed |
| TC-PRE-018 | Trust gate | ANONYMOUS vs VERIFIED_EXTERNAL caller | denied before `execute()` / passes |

**Injection assertions test BEHAVIOUR** (the run terminates with `status=ERROR` and
no `validated_input` / `input_text` is published), never the wording of whichever
layer refused it — the framework input gate may reject high-confidence injection
content before `execute()` runs, and the node keeps its own scan as defense in
depth.

**Declined vs refused is asserted, not glossed.** A refusal the caller can correct
(type, empty, length, metadata) is asserted as `status=SUCCESS` with the document
not carried forward; a refusal the caller cannot correct (injection, PII) is
asserted as `status=ERROR`. Asserting one where the other belongs is the defect
these cases exist to catch — see `docs/02_design.md` §7a.

---

## 3. Caller-metadata contract (`TestCallerMetadataValidation`, 42 tests)

Every field of `input_context` is hostile until proven bounded.

| TC-ID | Scenario | Expected |
|-------|----------|----------|
| TC-CTX-001 | Absent metadata | defaults applied (`financial_document`, `""`, `0.0`, `[]`) |
| TC-CTX-002 | Valid metadata | values carried into state |
| TC-CTX-003 | Non-dict `input_context` | declined — status=SUCCESS, nothing carried forward |
| TC-CTX-004 | Identifier fields (`document_type`, `fiscal_period`) × 7 hostile values each — free text, uppercase, over-length, empty, markup, wrong type, `None` | declined — status=SUCCESS, `error_log` names the field; value never echoed |
| TC-CTX-005 | `materiality_threshold` non-finite matrix — `NaN`, `Infinity`, `-Infinity`, their string forms, numeric strings, negative, over-magnitude, bool, `None`, list | declined — status=SUCCESS, `error_log` names the field (fail CLOSED: nothing is computed from the value) |
| TC-CTX-006 | `materiality_threshold` in range — `0`, `0.0`, `1`, `10000`, `999_999_999_999.0`, `1e12` | status=SUCCESS, value carried as float |
| TC-CTX-007 | `focus_terms` invalid — not a list, non-inert entry, non-string entry, over entry cap (11), `[None]` | declined — status=SUCCESS, `error_log` names the field; value never echoed |
| TC-CTX-008 | `focus_terms` at the 10-entry cap | status=SUCCESS |
| TC-CTX-009 | Unknown context field | ignored — never carried, never rendered |

---

## 4. ChunkNode (`tests/unit/test_chunk_node.py`)

`TestChunkText` (7 tests) + `TestChunkNode` (5 tests): empty input, short text, exact
chunk size, multi-segment coverage, segment-length bound, overlap correctness, custom
parameters; node-level success, empty-input error, `input_text` fallback, ANONYMOUS
trust declaration, partial-dict contract.

---

## 5. SummarizeNode (`tests/unit/test_summarize_node.py`, 20 tests)

| TC-ID | Scenario | Expected |
|-------|----------|----------|
| TC-SUM-001 | All four summary fields computed | non-empty strings |
| TC-SUM-002 | Output reflects the input | different documents → different summaries |
| TC-SUM-003 | Determinism | identical input → identical output |
| TC-SUM-004 | Aggregation with materiality filter | count / total / largest match the arithmetic |
| TC-SUM-005 | Zero threshold | every detected amount counted |
| TC-SUM-006 | Rendered aggregates on the grid | every rendered value ≥1,000 is a multiple of 1,000 |
| TC-SUM-007 | No monetary amounts | reported explicitly |
| TC-SUM-008 | Percentage figures counted | count reported |
| TC-SUM-009 | Risk lexicon categories | matched categories counted |
| TC-SUM-010 | Overlapping phrases | counted once, not twice |
| TC-SUM-011 | Focus-term separator variants | `supply_chain` matches `supply-chain` |
| TC-SUM-012 | No risk terms | reported explicitly |
| TC-SUM-013 | Action items | directive sentences captured |
| TC-SUM-014 | No action items | reported explicitly |
| TC-SUM-015 | Fallback to `chunked_segments` | status=SUCCESS |
| TC-SUM-016 | No input at all | status=ERROR — a missing `validated_input` here is a broken invariant, not a value a caller can restate, so it terminates rather than completing |
| TC-SUM-017 | Segments written back to state | non-empty list |
| TC-SUM-018 | Defensive metadata handling | junk threshold / focus terms degrade, never raise |
| TC-SUM-019 | Trust declaration | ANONYMOUS |

---

## 6. PostProcessNode — the output boundary (`tests/unit/test_post_process_node.py`)

### 6.1 Formatting (`TestPostProcessFormatting`, 4 tests)

Full report with all sections and the precision schema note; empty-field placeholder;
partial-field rendering; ANONYMOUS trust declaration.

### 6.2 Block scan (`TestOutputBlockScan`, 7 tests)

| TC-ID | Scenario | Expected |
|-------|----------|----------|
| TC-POST-004 | API key in the summary | status=ERROR, no `formatted_output` |
| TC-POST-005 | Bearer token in key figures | status=ERROR, no `formatted_output` |
| TC-POST-006 | Email in risk flags | status=ERROR, no `formatted_output` |
| TC-POST-007 | SSN in action items | status=ERROR, no `formatted_output` |
| TC-POST-008 | Labelled PII (`SSN`/`TAX`/`VSA` + digits) | blocked — the scan runs before any numeric rewriting, so a label that the precision grammar reads as a currency marker cannot hide the sequence |

### 6.3 Whole-document redaction (`TestWholeDocumentRedaction`, 2 tests)

A verbatim embedding of the full validated document is replaced with `[REDACTED]`;
short quotes are untouched.

### 6.4 Precision grid (`TestPrecisionGridEnforcement`, 30 tests)

The stated invariant — every monetary-form value sits on the 1,000 grid — is verified
for every representation, in both directions:

- **Snaps** (17 forms): marker-adjacent grouped (`JPY 1,234` → `JPY 1,000`), short value
  in currency context, symmetric value-then-marker, multi-space / tab / newline
  delimiters, attached and fullwidth symbols, postfix kanji and won markers, signed
  attached and spaced forms, grouped values below 10,000 (no magnitude exemption),
  large grouped forms, bare 5+-digit runs, signed bare runs.
- **Byte-identical** (10 forms): on-grid values, embedded acronym plus year
  (`STAR 2026`), bare years, horizon tokens (`90d`), page references (`p.21`), version
  tags (`v12`), bare counts, percentages.
- **Node level** (3 tests): off-grid figures in the assembled report are snapped;
  on-grid sections pass through unchanged; a full-report scan with the gate's own
  recogniser finds no off-grid token.

### 6.5 Decimal precision grid (`TestDecimalPrecisionGrid`, 27 tests)

A decimal fraction belongs to the token it follows. Read as a value of its own it
produced two different wrong answers on the shipped surface — a non-monetary figure
rewritten (`8.512345` → `8.512,000`, `9999.99999%` → `9999.100,000%`) and a real amount
snapped on its integer part with the fraction left dangling (`JPY 1234.56` →
`JPY 1,000.56`), which is neither the true figure nor on the grid. Verified in both
directions; decimals are NOT exempt from the grid.

- **Decimal amounts snap, as ONE number** (11 forms): `JPY 1234.56` → `JPY 1,000`,
  multi-space and single-newline delimiters preserved, symmetric value-then-marker,
  attached symbol and postfix kanji markers, signed forms with the explicit plus
  preserved, grouped-with-fraction, bare 5+-digit-with-fraction, and rounding taken on
  the whole value rather than the integer part (`JPY 1500.5` → `JPY 2,000`).
- **Sentence-ending amount still snaps** (1 test): `The book totals JPY 9999.` →
  `The book totals JPY 10,000.` — the decimal point joins the LEADING guard only; in a
  trailing guard an amount that ends a sentence would walk off the grid.
- **Non-monetary decimals byte-identical** (11 forms): bare decimals with long
  fractions, percentages with long fractions, ratios in prose, version tags, page
  references, ISO dates, and on-grid amounts carrying a zero fraction (`JPY 1,000.00`).
- **Grammar properties** (2 tests): a match may not BEGIN inside a number (guarding the
  decimal point alone lets the engine enter the fraction one digit later), and fraction
  absorption survives backtracking (a plain optional fraction falls back to the integer
  part when the following text fails the trailing guard, restoring `JPY 1,000.56m`).
- **Node level** (2 tests): the assembled report keeps caller ratios and percentages
  while snapping caller amounts; the corruption signature — a decimal point immediately
  followed by a comma-grouped run — appears nowhere in the report.

---

## 7. Proof-of-Boundary Tests

### PB-4: Import isolation (`test_import_isolation.py`)

AST scan of all `src/*.py` files — zero platform-internal SDK imports allowed.

### PB-2/PB-5: State safety (`test_state_safety.py`)

AST scan of `src/schemas/state.py` — no credential-like field names, no `BaseModel` or
`InvocationContext` annotations.

### PB-6: Backbone invoke order (`test_pb_invoke_order.py`)

Full `FinC2004Agent().invoke()` with a `VERIFIED_EXTERNAL` caller and a SUCCESS-yielding
payload. Expected `node_history` order:

```
InitializeNode → PreProcessNode → SummarizeNode → PostProcessNode → FinalizeNode
```

Asserts status SUCCESS, non-empty `result["output"]`, and all five nodes in order.

### PB-7: HITL interrupt propagation (`test_pb7_hitl_interrupt_propagation.py`)

This template does not enable cross-boundary HITL propagation (`propagate_hitl` is not
declared), so there is no propagation behaviour to assert. Ships as a real, importable
module that skips with an explicit reason and is filled in if HITL propagation is wired.

### PB-8: End-to-end through `/invoke` (`test_pb_invoke_endpoint.py`, 21 tests)

The full stack via the real ASGI entry point — HTTP adapter, Bearer trust promotion,
runtime config load, compiled graph, output gate:

| TC-ID | Scenario | Expected |
|-------|----------|----------|
| TC-E2E-001 | `/health` | 200, status ok |
| TC-E2E-002 | Runtime config reaches the graph | `max_retry=3`, `timeout_s=30` on the compiled agent |
| TC-E2E-003 | Authenticated invoke with metadata | SUCCESS, non-empty report computed from this document (header, materiality count, largest aggregate, focus-term count) |
| TC-E2E-004 | Output varies with the document | different documents → different reports |
| TC-E2E-005 | Unauthenticated caller | refused, no output |
| TC-E2E-006 | Wrong Bearer token | refused, no output |
| TC-E2E-007 | Invalid metadata (9 parametrized cases incl. non-finite numerics) | declined — HTTP 200, `status=SUCCESS`, and NO report in the body (`Summary Report` absent); the rejected value is never echoed anywhere in the response |
| TC-E2E-008 | Raw JSON `NaN` / `Infinity` / `-Infinity` literals | never a report: either the adapter rejects the body (400/422), or the run completes (`status=SUCCESS`) carrying the reason with `Summary Report` absent from `output` |
| TC-E2E-009 | Oversized `input_context` | HTTP 413 at the adapter |
| TC-E2E-010 | Injection content | refused, nothing published |
| TC-E2E-011 | Report scans on the monetary grid | no off-grid token on the real surface |
| TC-E2E-012 | Caller decimals in the published report | ratios/percentages byte-identical; a currency-context decimal snaps as ONE number; no `\d.\d+,\d{3}` splice artifact |

---

## 8. Test Execution Summary

| Category | Tests |
|----------|-------|
| Unit — PreProcessNode (screening + trust gate) | 20 |
| Unit — PreProcessNode (caller-metadata contract) | 42 |
| Unit — ChunkNode | 12 |
| Unit — SummarizeNode | 20 |
| Unit — PostProcessNode (formatting / block scan / redaction / precision grid) | 43 |
| Unit — PostProcessNode (decimal precision grid, `TestDecimalPrecisionGrid`) | 27 |
| Unit — Framework compliance (TC-06 / TC-07) | 2 |
| PoB — Import isolation (PB-4) | 1 |
| PoB — State safety (PB-2/5) | 1 |
| PoB — Invoke order (PB-6) | 1 |
| PoB — HITL (PB-7, skipped) | 1 |
| PoB — End-to-end `/invoke` (PB-8) | 22 |
| **Total** | **192 (191 passing, 1 skipped)** |
