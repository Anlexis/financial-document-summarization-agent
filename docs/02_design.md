# Design — FIN-C2-004 Financial Document Summarization Agent

**Template ID**: FIN-C2-004
**Category**: Cat 2 (domain-specific flat pipeline)
**Industry**: FIN
**Pattern**: flat summarization pipeline on the standard 5-node backbone

---

## Position in the Framework Architecture

| Aspect | Value |
|---|---|
| Agent Class | `FinC2004Agent` |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| State | flat TypedDict (`src/schemas/state.py`) |
| Nodes | `FunctionNode` subclasses (`src/nodes/*.py`) |
| Graph | composition via `register_nodes()` (`src/graph/graph.py`) |

---

## 1. Node Composition

```
START
  └→ InitializeNode       (injected by super().register_nodes())
  └→ PreProcessNode       [pre_process slot] — input gate + metadata validation
       on ERROR → FinalizeNode
       on DECLINE (§7a) → SummarizeNode, which stands down
  └→ SummarizeNode        [main slot]        — chunk + extractive summarization
       on SUCCESS → PostProcessNode
       on ERROR   → FinalizeNode
       on RETRY   → PreProcessNode (up to max_retry)
  └→ PostProcessNode      [post_process slot]— output gate + report formatting,
                                               or the refusal sentence (§7a)
  └→ FinalizeNode         (injected by super().register_nodes())
END
```

A declined-but-correctable request (§7a) leaves `pre_process` with
`status = SUCCESS`, so it follows the ordinary edge rather than the error edge.
It is `SummarizeNode` and `PostProcessNode` standing down on the reason code —
not the routing — that stops any work being done on it.

`ChunkNode` is a standalone FunctionNode (`src/nodes/chunk_node.py`). Its
`chunk_text()` helper is imported by `SummarizeNode`. It is not wired as a
backbone slot; it is independently unit-testable.

### Slot mapping

| Backbone slot  | Domain class      | Responsibility                                        |
|----------------|-------------------|-------------------------------------------------------|
| `pre_process`  | PreProcessNode    | Validate document + caller metadata, injection/PII scan |
| `main`         | SummarizeNode     | Chunk input, compute the structured summary            |
| `post_process` | PostProcessNode   | Output gate (3 layers), format `formatted_output`      |

---

## 2. Node Configuration

| Node           | Responsibility                                  | Input State Keys             | Output State Keys                                              | Class            |
|----------------|-------------------------------------------------|------------------------------|----------------------------------------------------------------|------------------|
| initialize     | Set schema_version, session_id, trust_level     | —                            | schema_version, session_id, caller_trust_level                 | InitializeNode   |
| pre_process    | Input gate: type/empty/length/injection/PII + caller-metadata validation | user_input, input_context | input_text, validated_input, document_type, fiscal_period, materiality_threshold, focus_terms, status — or `error_code` alone when the request is declined | PreProcessNode   |
| main           | Chunk text + compute structured summary; stands down when the run carries a reason code | validated_input, materiality_threshold, focus_terms, error_code | chunked_segments, summary_output, key_figures, risk_flags, action_items, status | SummarizeNode    |
| post_process   | Output gate + format structured report, or render the refusal sentence | summary_output, key_figures, risk_flags, action_items, document_type, fiscal_period, validated_input, error_code | formatted_output, status | PostProcessNode  |
| finalize       | Build response_metadata, total_time_ms          | —                            | response_metadata, total_time_ms                               | FinalizeNode     |

---

## 3. Data Flow

```
user_input (str) + input_context (dict — caller metadata)
  │
  ▼ PreProcessNode.execute()
    - type guard (must be str)
    - empty/whitespace guard
    - length guard (max 50,000 chars)
    - injection scan (regex)
    - PII detection (SSN/CC/email/phone; [MASKED] sentinel)
    - caller-metadata validation (identifiers inert, numerics finite+bounded,
      fail CLOSED naming the field, value never echoed; absent → defaults)
    → DECLINE (type/empty/length/metadata — §7a):
             {status:SUCCESS, error_code, error_log, error_message}
    → REFUSE  (injection/PII — §7a): {status:ERROR, error_log, error_message}
    → OK:    {input_text, validated_input, document_type, fiscal_period,
              materiality_threshold, focus_terms, status:SUCCESS}
  │
  ▼ SummarizeNode.execute()
    - error_code present → stand down, pass the reason through untouched
    - chunk_text(validated_input) → chunked_segments list[str]
    - extractive executive summary (keyword/monetary/position scoring)
    - key figures: monetary + percentage extraction → AGGREGATES only
      (count, total, largest), materiality-filtered, grid-rounded
    - risk flags: risk-category lexicon counts + caller focus terms
    - action items: directive-language sentences
    → {chunked_segments, summary_output, key_figures, risk_flags,
       action_items, status:SUCCESS}
  │
  ▼ PostProcessNode.execute()
    - error_code present → render the fixed sentence for that code as
      formatted_output; no report is assembled and no gate layer runs,
      because there is nothing to gate
    - layer 1: whole-document verbatim redaction (report is a reduction)
    - layer 2: monetary precision grid (every monetary-form token snapped
      to the nearest 1,000; marker/delimiter/sign preserved; audited)
    - layer 3: credential/PII scan on the final report (block on hit)
    → ERROR: {status:ERROR, error_log, error_message}
    → OK:    {formatted_output, status:SUCCESS}
  │
  ▼ invoke() result: {"output": formatted_output, "status": ..., ...}
```

---

## 4. State Definition (`src/schemas/state.py`)

Extends `AgentState` as a flat TypedDict (no Pydantic, no nested objects —
graph checkpoints use msgpack serialization).

| Field                   | Type          | Set by         | Description                        |
|-------------------------|---------------|----------------|------------------------------------|
| `input_text`            | str           | PreProcessNode | Stripped document text             |
| `chunked_segments`      | list          | SummarizeNode  | Overlapping text segments          |
| `document_type`         | str           | PreProcessNode | Validated caller metadata (identifier) |
| `fiscal_period`         | str           | PreProcessNode | Validated caller metadata (identifier) |
| `materiality_threshold` | float         | PreProcessNode | Validated caller metadata (finite, bounded) |
| `focus_terms`           | list          | PreProcessNode | Validated caller metadata (≤10 identifiers) |
| `summary_output`        | str           | SummarizeNode  | Executive summary (extractive)     |
| `key_figures`           | str           | SummarizeNode  | Aggregate monetary/percentage figures |
| `risk_flags`            | str           | SummarizeNode  | Risk category counts               |
| `action_items`          | str           | SummarizeNode  | Directive-language sentences       |
| `error_message`         | Optional[str] | any node       | Human-readable error description   |
| `error_code`            | Optional[str] | PreProcessNode | Reason a run completed without carrying out the request (`EMPTY_INPUT` / `QUESTION_TOO_LONG` / `INVALID_REQUEST`); internal — never released in the response envelope (§7a) |
| *(inherited)*           | —             | framework      | `user_input`, `input_context`, `status`, `session_id`, `node_history`, `error_log`, `formatted_output`, `validated_input`, `hitl_*` |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage risk)
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, or arbitrary Python objects (msgpack incompatible)

---

## 5. Caller-Data Contract (`input_context`)

The `/invoke` adapter accepts an optional `input_context` object (size-capped
at 256 KB serialized). Every field the pipeline consumes is validated in
`PreProcessNode`; invalid values fail CLOSED (the field is named in `error_log`,
the value is never echoed) and the run completes carrying the reason rather than
terminating — §7a; absent fields degrade to defaults. Unknown fields are ignored
and can never influence the report.

| Field | Contract | Default |
|---|---|---|
| `document_type` | identifier `[a-z0-9_]{1,32}` | `financial_document` |
| `fiscal_period` | identifier `[a-z0-9_]{1,32}` | omitted |
| `materiality_threshold` | finite number in [0, 1e12]; NaN/±Infinity rejected (they parse as floats but compare False against every bound — silent fail-open otherwise) | `0.0` |
| `focus_terms` | list of ≤10 identifiers; underscores match space/hyphen separators when counting | `[]` |

---

## 6. Summarization Strategy

Deterministic, extractive — no LLM call, no external service:

- **Executive summary**: sentences scored on financial-keyword density,
  monetary content, and document position; top 3 rendered in document order.
- **Key figures**: monetary tokens recognised with the SAME grammar the output
  gate enforces (single source of truth: `_NUM_TOKEN_RE`); reported as
  aggregates only (count, total, largest of material amounts), pre-rounded to
  the 1,000 grid. Percentage figures counted.
- **Risk flags**: per-category lexicon alternations (longest phrase first so
  overlapping phrases count once) + the caller's validated focus terms.
- **Action items**: sentences carrying directive/commitment language.

---

## 7. Security Gate Design

### Trust-Level Gate

| Node               | `required_trust_level`   | Rationale                                          |
|--------------------|--------------------------|----------------------------------------------------|
| PreProcessNode     | `VERIFIED_EXTERNAL`      | External entry gate — must authenticate            |
| SummarizeNode      | `ANONYMOUS`              | Internal pipeline step                             |
| PostProcessNode    | `ANONYMOUS`              | Internal pipeline step                             |
| ChunkNode          | `ANONYMOUS`              | Standalone testable utility                        |

### Input Gate (PreProcessNode)

- Type guard → declines non-string inputs
- Empty guard → declines blank documents
- Length guard → declines documents > 50,000 characters
- Injection scan → regex catches `ignore previous instructions`, `DROP TABLE`, `<script>`, etc. — **refused outright**
- PII detection → SSN, credit card, email, phone; `[MASKED]` sentinel (pre-masked by the framework input gate) — **refused outright**
- Caller-metadata validation → §5 contract; fail CLOSED per field, declined
- All paths emit `emit_trace_event`

"Declines" and "refuses outright" are two different outcomes, not two words for
one — see §7a.

### 7a. Refusing a request

A refusal never processes the document: nothing is chunked, nothing is
summarized, no report is assembled, and the reason is written to `error_log` and
an audit event exactly as before. What differs between the two classes of
refusal is how the outcome is reported, and that follows from whether the caller
can act on it.

**A reason the caller can correct completes the run.** A non-string input, an
empty document, a document over the 50,000-character limit, and every
out-of-contract `input_context` field (§5) are values the caller can fix. The run
finishes with `status = SUCCESS` carrying a reason code in State — `EMPTY_INPUT`,
`QUESTION_TOO_LONG` for the length guard, `INVALID_REQUEST` for the rest — and the
caller-facing body is the fixed sentence for that code
(`src/services/failure_message.py`). Terminating instead would end the calling
surface's turn and surface an exception type alone, leaving the reason reachable
only from the audit trail; completing lets the caller correct the submission and
send it again on the same conversation.

**A reason the caller cannot correct terminates the run.** Injection content and
PII in the document are refused outright with `status = ERROR`: resubmitting the
same document unchanged is not a correction, and inviting a resend of content the
agent will not read would be misleading. So are the outcomes the caller has no
part in — the output gate's own violation, and a missing `validated_input` at
`chunk` or `summarize`, which means the pipeline reached a state it promised could
not occur.

**The reason code stays internal.** `error_code` is a State field and is never
released in the response envelope. The caller is told what to correct by the
sentence, never by a code.

**Everything after the mark stands down.** Once `error_code` is set, `chunk`,
`summarize` and the whole report-assembly path in `post_process` return the mark
untouched, so no summarization runs on a document that was already declined and no
report-shaped value is produced. `post_process` renders the sentence as
`formatted_output`, which is what the envelope surfaces as `output`; a run that did
not carry out the request returns that sentence, never something shaped like a
report.

### Output Gate (PostProcessNode)

Three independent layers on the final report, each audited:

1. Whole-document verbatim redaction — the report is a reduction; a verbatim
   embedding of the full validated document is replaced with `[REDACTED]`.
2. Monetary precision grid — every monetary-form token (comma-grouped,
   5+-digit runs, or short values in currency context on either side, signed,
   any whitespace run) must sit on the 1,000 grid; off-grid values are
   snapped with marker/delimiter/sign preserved and an audit event emitted.
   A token may carry a decimal fraction, and the fraction belongs to the SAME
   token: an amount in currency context snaps on its whole value as one number
   (`JPY 1234.56` → `JPY 1,000`), never with the fraction left dangling. There
   is no decimal exemption — a ratio, percentage or version tag is simply not a
   monetary token, and a match may not begin inside a number, so those stay
   byte-identical.
3. Credential/PII scan — API keys, bearer tokens, private-key headers,
   SSN/credit-card/email/phone anywhere in the final report withhold the
   output entirely.

The domain output gate is implemented as module-level helpers called from
`execute()`; the framework's own gate methods are final and are not shadowed
(`_extra_security_gate_input` / `_extra_security_gate_output` hooks are not
used by this template).

### Audit Logging

All `emit_trace_event` calls use positional form: `emit_trace_event(event, payload, state)`.

| Node            | Events emitted                                                      |
|-----------------|---------------------------------------------------------------------|
| PreProcessNode  | `pre_process_complete`, `pre_process_error`, `pre_process_blocked`  |
| ChunkNode       | `chunk_complete`, `chunk_error`                                     |
| SummarizeNode   | `summarize_complete`, `summarize_error`                             |
| PostProcessNode | `post_process_complete`, `post_process_blocked`, `post_process_degraded`, `output_precision_redaction`, `output_verbatim_redaction` |

### Credential Handling

- No credentials stored in State fields
- No hardcoded credentials in node code
- The standalone server provisions secrets via the framework
  `secrets_factory()` / `bound_secrets()`; `POST /invoke` promotes callers
  presenting a valid `INVOKE_AUTH_TOKEN` Bearer credential to
  `VERIFIED_EXTERNAL`

---

## 8. Framework Utilization

- `AgentBaseGraph` — graph base class (config: `max_retry`, `timeout_s` from
  `config/config.yaml`, loaded by the platform registry or the standalone
  server and passed to the constructor)
- `FunctionNode` — node base class (all domain nodes)
- `AgentStatus` — enum for SUCCESS/ERROR/RETRY
- `TrustLevel` — enum for required_trust_level declarations
- `emit_trace_event` — audit logging
- `InvocationContext` — caller identity; `input_context` — caller metadata
  channel (validated in PreProcessNode)

---

## 9. Import Isolation Confirmation

- Template does not import the platform-internal SDK (`agenticstar`) — prohibited
- Import targets: `framework.*` and `shared.*` only
- `src.*` imports within the template are allowed (inter-node)

---

## 10. Design Decision Record

| Decision            | Option A              | Option B              | Chosen    | Rationale                                        |
|---------------------|-----------------------|-----------------------|-----------|--------------------------------------------------|
| Base type           | AgentBaseGraph        | AutonomousBaseGraph   | AgentBaseGraph | Fixed pipeline, no autonomous loop needed   |
| Graph shape         | Nested subgraph       | Flat 3-slot           | Flat      | Domain complexity fits in the standard backbone  |
| Chunk strategy      | ChunkNode as backbone | ChunkNode as utility  | Utility   | 3-slot backbone constraint; SummarizeNode calls chunk_text() |
| Summarization       | LLM call              | Deterministic extractive | Deterministic | Reproducible, self-contained, no provider dependency |
| Monetary recognition| Separate extract/enforce grammars | One shared grammar | Shared | Extraction and gate must agree or figures leak   |
| Output gate hook    | `_extra_security_gate_output()` | module-level helper | module-level helper | Framework gate methods are final; helpers keep the layers unit-testable |
| Reporting a caller-correctable refusal | Terminate the run | Complete the run carrying a reason code | Complete | Terminating ends the calling surface's turn and shows an exception type only, leaving the reason reachable from the audit trail alone; completing tells the caller what to correct and lets them resend on the same conversation. The refusal itself is unchanged — nothing is summarized and no report is released |
| Stopping work after a decline | Route the declined run to finalize | Each downstream node stands down on the reason code | Stand down | The declined run leaves `pre_process` at SUCCESS, so the error edge does not apply; making every downstream node check the mark keeps the stop where the work is, rather than relying on a routing rule the declined status no longer triggers |
| Injection / PII in the document | Decline like a bad field | Refuse outright | Refuse | Resubmitting the same document is not a correction; inviting a resend of content the agent will not read would be misleading |
| Reason code in the response envelope | Publish it alongside the message | Keep it in State, publish the sentence only | State only | A code is an internal routing label; what the caller needs is the sentence naming what to correct, and publishing both invites callers to branch on a label the template is then never free to change |
