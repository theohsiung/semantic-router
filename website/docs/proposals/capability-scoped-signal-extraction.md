---
title: Capability-Scoped Signal Extraction
description: Lets one deployment declare named capabilities so several signals can read one model, and makes schema-constrained decoding the norm for chat-protocol classifiers.
created: 2026-09-22
status: Proposal
---

> **Status:** Proposal · **Created:** 2026-09-22

## Problem

A request is described by several signals — domain, PII, jailbreak, safety, complexity,
fact-check, feedback, modality. Today each one is produced by its own model.
`config/config.yaml` declares eleven distinct `Vela-1.0-Encoder-307M-*` artifacts, one per
signal, and every one of them reads the same request text from scratch.

Model families are arriving that answer several of those questions from one invocation.
The router cannot use them, and the obstacle is not performance work — **the configuration
language has no way to say it**. Two `switch name` blocks in the config layer hard-code
"one consumer name → exactly one contract → one legacy per-signal struct":

- `validateTaskModelBinding` (`pkg/config/model_deployments.go`) maps each consumer name to
  exactly one required contract and ends in `if decl.Contract != want`.
- `ProjectRecipeModelBindings` (`pkg/config/model_binding_projection.go`) writes each binding
  into one legacy per-signal struct — `scoped.CategoryModel.ModelID`, `scoped.PIIModel.ModelID`,
  `scoped.PromptGuard.ModelID`, and so on.

A binding that produces several results has no representation in either. This is the shape
`AR020` describes in `tools/agent/docs/architecture-risks.md`, and that entry's acceptance
criterion is the goal of this proposal restated:

> A new classifier family can land through a family-owned adapter and focused tests without
> unrelated constructor edits.

## Why now

This is not a new argument. The backend-block work deferred `Deployment`/`Capability`
deliberately, because every classifier pointed at its own service and the object would have
been a one-to-one indirection. That deferral recorded three triggers that would reverse it.
The first was:

> **One service, several signals.** Someone wants the same deployed model to serve two signals
> with different capabilities. A single `protocol` per catalog entry cannot express this;
> **named capabilities can.**

That trigger has fired. This proposal collects on a decision already made rather than
re-opening it, and it is deliberately the smallest structure that discharges it — it does not
build `ModelManifest`, `Receipt`, or a conformance pipeline.

## Proposal

Two optional config fields and one contract value.

| Config | Meaning |
| --- | --- |
| `deployments.<name>.capabilities[]` | what this deployment can produce: `{name, kind, labels \| labels_from}` |
| `bindings.<consumer>.capability` | which one this consumer reads |
| `contract: capability_results.v1` | the multi-result envelope |

The consumer name stays the key. `domain_classifier`, `pii_classifier`, `prompt_guard`,
`classifier.<name>` and `safety.<name>.hazard` already *are* the observation names — they key
`model_bindings`, both central switches, and the visibility rule in `model_bindings_global.go`.
A second naming plane on top would be the parallel discriminator inventory the config contract
forbids, so this proposal introduces no new noun for a signal producer.

### Resolution is grouping, not search

Bindings sharing `(recipe, deployment, contract, adapter, head)` form one **binding group**.
Within a group, `capability` must be unique, must exist in the deployment's `capabilities:`,
and its `kind` must equal the contract that consumer is registered to read. Four equality tests
at config load. **No priority rule, no fallback, and nothing that picks a winner among candidate
producers.**

Two reasons. `requireDeclaredContract` (`pkg/config/classifier_backend.go`) already refuses to
default the moment more than one reading is possible, because guessing wrong surfaces per
request rather than at config load; a resolver that searched would reintroduce exactly that.
And a component that *chooses* which model produces a signal has drifted into the layer
`architecture-guardrails.md` reserves — signals extract facts, decisions compose them,
algorithms choose among models.

### The type system already anticipated this

`binding.Identity{Recipe, Name, Deployment, Contract, Adapter, Head}` holds logical coordinates
over a shared physical resource, deliberately excluded from `ResourceIdentity.Key()`, and
`Pool.Acquire` already "loads at most one resource for an identity", refcounted. `Capability` is
one more coordinate of that kind.

The escape hatch exists twice already: the `classifier.` prefix bypasses both central switches —
`validateTaskModelBinding`'s `default:` branch and `ProjectRecipeModelBindings`' `continue` —
and `safety.` is the second instance. A third family is not a new mechanism.

### Config surface

```yaml
# One local multi-head model, three signals
deployments:
  vela-multi:
    artifact: models/Vela-2.0-MultiHead-500M
    provider: candle
    input: { max_tokens: 512, overflow: truncate }
    capabilities:
      - { name: domain,     kind: label_distribution.v1, labels_from: mapping }
      - { name: complexity, kind: score.v1 }
      - { name: pii,        kind: token_spans.v1,        labels_from: mapping }
bindings:
  domain_classifier:
    { deployment: vela-multi, contract: capability_results.v1, capability: domain,
      adapter: multihead, mapping_path: config/domain_mapping.json }
  complexity:
    { deployment: vela-multi, contract: capability_results.v1, capability: complexity,
      adapter: multihead }
  pii_classifier:
    { deployment: vela-multi, contract: capability_results.v1, capability: pii,
      adapter: multihead, mapping_path: config/pii_mapping.json }

# One chat model, constrained decoding, two signals
deployments:
  guard-llm:
    provider: http
    external_model: guard-llm
    capabilities:
      - { name: jailbreak, kind: label_decision.v1, labels: [safe, unsafe, controversial] }
      - { name: domain,    kind: label_distribution.v1, labels_from: mapping }
bindings:
  prompt_guard:
    { deployment: guard-llm, contract: capability_results.v1, capability: jailbreak,
      adapter: http_chat }
  domain_classifier:
    { deployment: guard-llm, contract: capability_results.v1, capability: domain,
      adapter: http_chat, mapping_path: config/domain_mapping.json }
```

Today's per-signal form is unchanged and still valid. Mixing the two is not a feature — grouping
is per deployment, so a config that reads domain and complexity from a shared model while PII
keeps its own dedicated one needs no new syntax at all.

## The contract

`capability_results.v1` is an envelope over the existing leaf contracts, not a new payload
inventory. It is modelled on `tokenSpansEnvelope` (`pkg/classification/http_token_classifier.go`),
the only extensible envelope on the current wire surface and the one whose comment already says it
keeps the door open to this shape. It keeps each of that envelope's properties: `json.RawMessage`
members so missing, null and wrong-type stay distinguishable; an optional `model` member checked
against the configured name; and an `error` member that is never interpolated into a Go error,
because it routinely echoes the classified text.

It has to be a new contract. The existing `http_classify` label path is structurally single-task:
the request's `inputs` is a `string` with no batching slot, the label response is a bare JSON
array with no envelope, `alignIndependentLabelScores` requires every configured label exactly once
and rejects unknown labels, and `alignScoresToMapping` requires the array to sum to 1 ± 0.02 —
which forbids carrying two independent distributions by construction.

Per-capability partial failure is expressible, so each signal keeps its own `on_error: allow|block`.

## Where the boundary sits

`pkg/classification/AGENTS.md` is explicit that families own their backend and mapping behaviour
and that no further shared backend-selection matrix belongs in `classifier.go`. The fan-out
therefore lives in a shared handle, not a dispatcher:

- A `sharedCapabilityBackend` exposes `View(capability)` satisfying today's
  `SequenceClassifierBackend`, token and score interfaces. `evaluateDomainSignal` and its siblings
  are untouched, and `ownedSequenceBackend` stays for the per-signal case. A signal cannot tell
  whether its backend is private or a view.
- `validateTaskModelBinding` splits. Its consumer-name-to-contract table becomes a **consumer
  registry** — `ConsumerSpec{Contracts, Validate, Project}`, one registration per family declared
  in that family's own file, reusing `requireDeclaredContract`'s rule rather than restating it.
  Its provider and adapter rules (OpenVINO supports only embeddings and sequence distributions,
  HTTP adapters cannot enforce a local tokenizer budget, remote tasks cannot bind a local head)
  are genuinely physical and stay.
- `ProjectRecipeModelBindings` dies as a consequence, not as a goal: each `case` body moves
  verbatim into its family's `Project`, and a multi-capability binding writes a nullable
  `SharedCapabilityRef` instead of a per-signal `ModelID` that does not exist. That projection is
  preparation-only and never exported, so the reference costs nothing in the generated schema,
  the CRD or the dashboard contract.

The review check from the backend-block work still applies: if anything outside the backend
factory reads `contract`, the boundary has leaked.

## Shared execution

**Lazy skip moves up one level.** `runSignalDispatchers` skips a signal entirely when no decision
consumes it. That is preserved and extended to group granularity: a group runs only if at least
one of its capabilities feeds a used signal, and the requested set is passed to the adapter. For
a chat adapter the generated schema then contains only the requested capabilities, which is
strictly better than today. For a local multi-head model the unused heads are computed anyway;
the cost is head projections against a shared trunk, which is the point of such a model. Whether
the requested set is honoured is a property of the adapter, not of a config flag.

**Co-location is not always a win.** Guard and PII run 8k/32k windowed while domain runs at 512.
Sharing a forward across heads that disagree on input geometry is a regression, so co-location is
allowed only when tokenizer, window geometry and budget agree — which `binding.Limits` and
`WindowCapability` already encode. This is a capability check at prepare, in the layer that
already performs capability checks.

**Text can diverge per signal.** `textForSignal` may return different text per signal type and
`SkipCompressionSignals` is per-request, so two consumers of one group may genuinely require two
forwards. Correctness comes from including the text hash in the per-request cache key; a result
is never shared across different inputs. A purely static rejection is impossible because the
policy is per-request, so the group emits a metric when it performs more than one forward in a
single request, making the cliff observable rather than silent.

## Schema-constrained decoding

This half pays off even if no multi-output model ever ships.

Chat-protocol jailbreak classification currently sends no `response_format` at all and recovers a
verdict by sniffing the model name, scraping with a regular expression, stripping code fences
before a JSON decode with boolean fallbacks, and finally matching against a fixed
`safe`/`unsafe`/`controversial` vocabulary (`pkg/classification/vllm_jailbreak_parser.go`).

The `http_chat` adapter for `capability_results.v1` instead generates a strict JSON schema from
the requested capabilities and sends it as a **top-level `response_format`**, generalising the
hand-built schema in `hallucination_detector_endpoint.go` — the one working precedent in tree,
already using `strict: true` with enum-constrained fields. The cascade above is then deleted, and
`AR054` (model eligibility enforces context capacity but not structured-output support) becomes
enforceable.

One defect to note rather than build on: `GenerationOptions.ExtraBody` marshals as a nested
`extra_body` object and has no flattening `MarshalJSON`. `extra_body` is a client-side concept in
the OpenAI Python client; over the wire it is ignored. The plumbing is not merely uncalled — it
would silently no-op if called, and should be removed or fixed rather than used as the transport
for this.

## Compatibility

Both new fields are `omitempty` and every new code path is inert unless
`contract: capability_results.v1` is set, so existing configuration round-trips byte for byte and
no migration is required. `ProjectRecipeModelBindings`' output is preparation-only, so
`SharedCapabilityRef` is invisible to the generated schema, the operator CRD and the dashboard
contract.

`binding.Capability` already exists and means what a loaded provider can actually execute. The
config field declares; `binding.Capability` observes. They are in different packages and the pair
is the design's honesty statement rather than a collision: a local capability can be verified
against the loaded handle at prepare, while a remote one stays a declaration until conformance
receipts exist.

## Delivery

| # | Scope | Contract surface |
| --- | --- | --- |
| 1 | `tasks.CapabilityResult` / `CapabilityResultSet` and the `capability_results.v1` decoder; unit tests only, nothing wired | none |
| 2 | Consumer registry replacing the consumer-name switch; pure refactor | none |
| 3 | `capabilities:` / `capability:` fields, contract enum value, grouping validation; preparation rejects with an explicit "not yet executable" error | schema, CRD, dashboard |
| 4a | Per-family `Project` replacing the projection switch; `SharedCapabilityRef` | none |
| 4b | Grouped preparation, `sharedCapabilityBackend` and `View`, group-level skip, per-request cache keyed by text hash | none |
| 5 | `http_chat` schema generation from the requested capabilities; retire the sniff-and-scrape parser | none |
| 6 | Local multi-head: providers declare head sharing instead of `candleSharesBackbone`'s switch; open `LoRAMultiTaskResult`'s three hardcoded fields and export `classify_multi_task` | none |
| 7 | `AR054` structured-output eligibility, docs, an e2e profile exercising a mixed config | docs |

Phase 3 is the only phase that regenerates `router-config-v0.3.schema.json`, the operator CRD and
`routerConfigContract.ts`; it is placed early so contract churn lands once, before the large
runtime phases. Phases 3–7 are behaviour-visible and carry E2E updates. Phase 6 is the only phase
entitled to a performance claim and must carry its own benchmark.

Phase 6 is worth stating precisely, because the mechanism it needs already exists and works.
`candleSharesBackbone` drops the token/sequence discriminator from the pool key for
`modernbert`/`mmbert*`, so one encoder is loaded and each task binds its head onto it — **weights
are already shared; the forward is not**. And `classify_multi_task`
(`candle-binding/src/model_architectures/lora/bert_lora.rs`) already runs the BERT backbone once
and applies each adapter and head to the pooled output. It is unreachable for two reasons: its
result type hardcodes exactly three fields (`intent`, `pii`, `security`), and there is no
`extern "C"` export for it. The config layer's switch has a counterpart one language down.

## Open questions

1. **Availability coupling.** A dedicated model fails one signal; a shared model fails every
   signal in its group at once. Each member still applies its own `on_error`, but the
   correlated-failure profile is new. Should group members be required to declare `on_error`
   explicitly instead of inheriting `allow`?
2. **Text divergence.** Hash-keyed cache plus a metric, or additionally a load-time rejection of
   provably incompatible pairs?
3. **Envelope name.** `capability_results.v1` is consistent with the `capability:` selector;
   `observations.v1` is the alternative.
4. **Per-capability calibration.** `OperatingPointReference` is `classifier.`-only today. Extend
   it to `capabilities[].operating_point`, or defer?
5. **Verifying declared capabilities.** Extend `binding.Capability` with the declared set and
   validate at prepare, the way `validateNativeLabelOrder` already validates label order?
6. **Metrics attribution.** `recordSignalExtraction` would charge one shared forward to every
   reading signal. A group-level duration plus a per-signal "shared" marker is needed.
7. **Response-direction signals** are out of scope here, but a response-stage group is the
   obvious next ask and the contract should not preclude it.
8. **`tasks.ClassificationBatch`** is the only existing three-signal type and its own doc says it
   does not describe a joint model forward. `CapabilityResultSet` is the type that does. Retire
   the old one onto it, or leave it and record the gap?

## References

- [Epic #2782](https://github.com/vllm-project/semantic-router/issues/2782) ·
  [RFC #2779](https://github.com/vllm-project/semantic-router/pull/2779) ·
  [#2760](https://github.com/vllm-project/semantic-router/issues/2760)
- Backend-block sub-issues: #2920, #2921, #2922, #3126, #3128
- `tools/agent/docs/architecture-risks.md` — `AR020`, `AR054`
- `tools/agent/docs/architecture-guardrails.md` — router ownership decomposition
- `pkg/classification/AGENTS.md` — family-owned adapters
- [Unified Config Contract v0.3](./unified-config-contract-v0-3)
- [Prompt Classification Routing](./prompt-classification-routing)
