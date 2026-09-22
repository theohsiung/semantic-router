---
title: Capability-Scoped Signal Extraction
description: Makes one deployment able to answer several signals, with the same spelling whether the model is loaded in-process or reached over HTTP.
created: 2026-09-22
status: Proposal
---

> **Status:** Proposal · **Created:** 2026-09-22

## Problem

A request is described by several signals — domain, PII, jailbreak, safety, complexity,
fact-check, feedback, modality. Today each one is produced by its own model:
`config/config.yaml` declares eleven distinct `Vela-1.0-Encoder-307M-*` artifacts, one per
signal, and every one of them reads the same request text from scratch.

Model families are arriving that answer several of those questions from one invocation, and
they may be loaded in-process or reached over HTTP. Neither is expressible, for two separate
reasons.

**The configuration language cannot say "one model, several signals."** Two `switch name`
blocks hard-code one consumer name to exactly one contract and one legacy per-signal struct:
`validateTaskModelBinding` (`pkg/config/model_deployments.go`) ends in
`if decl.Contract != want`, and `ProjectRecipeModelBindings`
(`pkg/config/model_binding_projection.go`) writes each binding into
`scoped.CategoryModel.ModelID`, `scoped.PIIModel.ModelID`, `scoped.PromptGuard.ModelID` and
their siblings. A binding producing several results has no representation in either.

**And attaching a model has two parallel spellings**, so "internal or external" is not
uniformly available even for one signal at a time. That second problem is the more
interesting one, because the code has already half-solved it.

## What the code already says

Four observations, all checkable in tree. Together they say the provider-agnostic mechanism
exists, is already in use, and is being shadowed by a parallel one.

**1. `deployment` + `binding` is already provider-agnostic.** `ModelDeployment` distinguishes
internal from external by `artifact` versus `external_model`, and the same
`ResolvedModelBinding` flows into the candle, ORT, OpenVINO and HTTP preparation paths.
Safety already uses this to reach a remote model: `prepareRemoteSafetyClassifier`
(`pkg/classification/safety_classifier_remote.go`) takes a `ResolvedModelBinding` and never
touches the `backend:` block.

**2. But four signals reach remote models a different way.** `CategoryModel`, `PIIModel`,
`PromptGuardConfig` and `ComplexityModelConfig` carry a `backend:` block
(`RemoteClassifierBackend`), which is remote-only by construction and covers only those four.

**3. The two spellings have already converged on the same values.** `backend:.protocol` takes
`http_classify` or `http_chat`. So does the binding layer's `adapter`: `model_deployments.go`
compares `decl.Adapter` against `RemoteClassifierProtocolHTTPChat` for `prompt_guard`, and
requires `hallucination_detector`'s adapter to be one of those two constants. **One value
space, two fields, in two different config blocks.**

**4. The consumer table already admits that a contract is not a property of a name.** In that
same switch, `prompt_guard` wants `label_distribution.v1` — unless
`deployment.Provider == "http" && decl.Adapter == http_chat`, in which case it wants
`label_decision.v1`. The contract is therefore a property of the *(consumer, location)* pair.
Once that is true of even one consumer, a table keyed on the consumer name cannot be where the
contract is decided.

A fifth, smaller symptom: the shared contract vocabulary is filed as though it were remote-only.
`label_distribution.v1` and its siblings are declared as `RemoteClassifierContract*` in
`pkg/config/classifier_backend.go`, yet `model_deployments.go` compares local bindings against
them; `text_pair_distribution.v1` and `embedding.v1` are bare literals in the switch, and
`relevance_scores.v1` lives in a third file.

## Requirement: internal or external, for every topology

The deployment form of a multi-signal model is not settled, so this proposal treats internal
and external as two boundaries around the same declaration rather than two designs. Four
topologies, each of which must be expressible either way:

| | Internal (loaded in-process) | External (reached over HTTP) |
| --- | --- | --- |
| **One model per signal** | works today for all eleven signals | partial — safety through the binding layer, four more through `backend:`, the rest not at all |
| **One model, several signals, one pass** | `classify_multi_task` runs the backbone once and applies each adapter and head, but it is unexported and its result type hardcodes three fields | no envelope exists; the label wire is structurally single-task |
| **A generative model producing 1..N signals** | `GenerativeClassifier` exists with zero callers in the router; there is no grammar machinery in the binding at all | precedent exists: one detector already sends a strict `json_schema` |
| **Mixtures of the above** | falls out of grouping, once everything is declared in one place | same |

The bottom row is the test of the design: if mixing costs new syntax, the design is wrong.

## Proposal

Two optional fields and one contract value.

| Config | Meaning |
| --- | --- |
| `deployments.<name>.capabilities[]` | what this deployment can produce: `{name, kind, labels \| labels_from}` |
| `bindings.<consumer>.capability` | which one this consumer reads |
| `contract: capability_results.v1` | the multi-result envelope |

They sit on the deployment and the binding — the layer that is already provider-agnostic —
not on `backend:`, which is remote-only. That single placement decision is what makes every
row of the table above have two columns.

### Validation is an intersection, not a table

A deployment declares what it produces. A consumer declares which contracts it can read.
Validation is the intersection of the two, so no central table needs to know that
`domain_classifier` exists.

Concretely: bindings sharing `(recipe, deployment, contract, adapter, head)` form one **binding
group**; within a group `capability` must be unique, must appear in the deployment's
`capabilities:`, and its `kind` must equal a contract that consumer is registered to read.
Four equality tests at config load — no priority rule, no fallback, and nothing that picks a
winner among candidate producers. `requireDeclaredContract`
(`pkg/config/classifier_backend.go`) already refuses to default the moment more than one
reading is possible, because guessing wrong surfaces per request rather than at config load;
a resolver that searched would reintroduce exactly that. It would also drift into the layer
`architecture-guardrails.md` reserves — signals extract facts, decisions compose them,
algorithms choose among models.

The consumer half of the intersection replaces the switch: a `ConsumerSpec{Contracts, Validate,
Project}` registered by each family **in that family's own file**. That is the acceptance
criterion `AR020` states — *a new classifier family can land through a family-owned adapter and
focused tests without unrelated constructor edits* — and it is also what
`pkg/classification/AGENTS.md` requires, since it keeps family behaviour out of a shared matrix.

**Prior art.** This inversion — the loaded thing declares what it supports, and the framework
validates a request against that declaration — is how vLLM admits arbitrary model
architectures: a registry keyed on the architecture rather than a table of known names, plus
capability traits checked at load. The half worth borrowing is exactly that inversion, and the
registry half already exists here in `binding.RegisterTask`. The form has to differ, because
this project's configuration contract is generated and mirrored into the operator CRD, so a
capability must be a data record rather than an interface assertion. The rest of vLLM's design
does not transfer: it answers one request with one model, so nothing in it prefigures a
multi-result envelope; its scheduler and KV-cache economics have no counterpart for a 307M
encoder at 512 tokens, where `admission.Admissioner` is already the right-sized control; and
its out-of-tree registration assumes runtime import of arbitrary code, which the rule layer
here does not allow. A new family remains configuration plus a compiled-in registration.

### Config surface

```yaml
# Internal: one multi-head model, three signals
deployments:
  vela-multi:
    artifact: models/Vela-2.0-MultiHead-500M
    provider: candle
    input: { max_tokens: 512, overflow: truncate }
    capabilities:
      - { name: domain,     kind: label_distribution.v1, labels_from: mapping }
      - { name: complexity, kind: score.v1 }
      - { name: pii,        kind: token_spans.v1,        labels_from: mapping }

# External: one chat model, two signals - same two fields
deployments:
  guard-llm:
    provider: http
    external_model: guard-llm
    capabilities:
      - { name: jailbreak, kind: label_decision.v1, labels: [safe, unsafe, controversial] }
      - { name: domain,    kind: label_distribution.v1, labels_from: mapping }

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
  prompt_guard:
    { deployment: guard-llm, contract: capability_results.v1, capability: jailbreak,
      adapter: http_chat }
```

The internal and external deployments differ in `artifact` versus `external_model` and in
`adapter`. Nothing about `capabilities:` or `capability:` changes between them, and a binding
group is scoped to one deployment, so the mixture above — three signals from a local model and
one from a remote one — needs no additional syntax.

### The contract is enforced at the boundary

A capability declares a `kind`; how that kind is *guaranteed* is the boundary's business, and
the three boundaries already have three different means:

- **In-process**: the FFI return type is already typed, and label order is checked at prepare
  by `validateNativeLabelOrder`. A local model needs no grammar, because the shape cannot be
  wrong by construction.
- **Remote `http_classify`**: the response is validated on arrival —
  `alignIndependentLabelScores` requires every configured label exactly once and rejects
  unknown ones.
- **Remote `http_chat`**: the only text-in, text-out boundary, and therefore the only one that
  needs the shape constrained *before* the response exists. The adapter generates a strict JSON
  schema from the requested capabilities and sends it as a top-level `response_format`,
  generalising the hand-built schema in `hallucination_detector_endpoint.go`.

That last point retires a real piece of guesswork independently of everything else: chat-protocol
jailbreak classification currently sends no `response_format` at all and recovers a verdict by
sniffing the model name, scraping with a regular expression, stripping code fences before a JSON
decode with boolean fallbacks, and matching against a fixed vocabulary
(`pkg/classification/vllm_jailbreak_parser.go`). It also makes `AR054` — model eligibility
enforces context capacity but not structured-output support — enforceable.

One defect to route around rather than build on: `GenerationOptions.ExtraBody` marshals as a
nested `extra_body` object with no flattening `MarshalJSON`. `extra_body` is a client-side
concept in the OpenAI Python client; over the wire it is ignored. The plumbing is not merely
uncalled — it would silently no-op if called.

### The envelope

`capability_results.v1` is an envelope over the existing leaf contracts, not a new payload
inventory. It is modelled on `tokenSpansEnvelope` (`pkg/classification/http_token_classifier.go`),
the only extensible envelope on the current wire surface, and keeps each of its properties:
`json.RawMessage` members so missing, null and wrong-type stay distinguishable; an optional
`model` member checked against the configured name; and an `error` member never interpolated
into a Go error, because it routinely echoes the classified text.

It has to be a new contract rather than an extension. The existing label path is structurally
single-task: `inputs` is a `string` with no batching slot, the response is a bare JSON array with
no envelope, and `alignScoresToMapping` requires that array to sum to 1 ± 0.02 — which forbids
carrying two independent distributions by construction. Per-capability partial failure is
expressible, so each signal keeps its own `on_error: allow|block`.

The same envelope describes an in-process multi-head result. Internally there is no wire, so the
envelope is the Go type `CapabilityResultSet` and the adapter fills it directly from the typed
FFI return; externally it is the JSON body. One declaration, two boundaries.

## Superseding the `backend:` block

`backend:` describes a remote-only attachment for four signals, with fields `protocol`,
`contract`, `model` and `deadline_ms`. Every one of those has a binding-layer counterpart or
needs one: `protocol` is already spelled as `adapter` for `prompt_guard` and
`hallucination_detector`, `contract` is already a `ModelBinding` field, and `model` names an
`external_model` entry that a deployment can name instead. Keeping both means the same concept
stays spelled two ways depending on which signal is being configured, and it guarantees that
half the signals can never take an external model.

This proposal therefore treats `backend:` as **superseded by the binding layer**, on a path that
breaks nothing:

1. The binding layer gains `capabilities:` / `capability:` and accepts an `http` deployment for
   every consumer, not only those with a `backend:` block.
2. `deadline_ms` — the one field with no binding-layer counterpart — moves onto the deployment,
   where it applies to internal and external calls alike. Note it is not uniformly honoured
   today: two of the outbound HTTP paths hard-code their own timeout.
3. `backend:` keeps working, and is documented as the earlier spelling of a binding to an
   external deployment. It is removed only once every consumer it serves can be expressed the
   other way and the shipped configurations have moved.

Nothing here reverses the decisions in #2930 and #3542, which made a single remote spelling out
of seven; it extends that consolidation one layer up, so that the single spelling is also the
one internal models use.

## Compatibility

Both new fields are `omitempty` and every new path is inert unless
`contract: capability_results.v1` is set, so existing configuration round-trips byte for byte and
no migration is required. `ProjectRecipeModelBindings`' output is preparation-only and never
exported, so the internal `SharedCapabilityRef` it writes for a grouped binding is invisible to
the generated schema, the operator CRD and the dashboard contract.

`binding.Capability` already exists and means what a loaded provider can actually execute. The
config field **declares**; `binding.Capability` **observes**. They are in different packages, and
the pair is the design's honesty statement rather than a collision: an internal capability can be
verified against the loaded handle at prepare, while an external one remains a declaration until
there is conformance evidence to check it against.

## Delivery

| # | Scope | Contract surface |
| --- | --- | --- |
| 1 | `tasks.CapabilityResult` / `CapabilityResultSet` and the `capability_results.v1` decoder; unit tests only, nothing wired | none |
| 2 | Consumer registry replacing the consumer-name switch; contract constants moved out of the remote-only file into a neutral one; pure refactor | none |
| 3 | `capabilities:` / `capability:`, contract enum value, grouping validation; preparation rejects with an explicit "not yet executable" error | schema, CRD, dashboard |
| 4a | Per-family `Project` replacing the projection switch; `SharedCapabilityRef` | none |
| 4b | Grouped preparation, a shared handle exposing per-capability views, group-level skip, per-request cache keyed by text hash | none |
| 5 | `http_chat` schema generation from the requested capabilities; retire the sniff-and-scrape parser; accept an `http` deployment for every consumer | none |
| 6 | Internal multi-head: providers declare head sharing instead of `candleSharesBackbone`'s switch; open `LoRAMultiTaskResult`'s three hardcoded fields and export `classify_multi_task` | none |
| 7 | `AR054` structured-output eligibility; `deadline_ms` on the deployment; docs; an e2e profile exercising a mixed internal/external config | docs |

Phase 3 is the only phase that regenerates the JSON schema, the operator CRD and the dashboard
contract; it is placed early so contract churn lands once. Phases 3–7 are behaviour-visible and
carry E2E updates. Phase 6 is the only phase entitled to a performance claim and must carry its
own benchmark.

Phase 6 is narrower than it looks, because the mechanism exists and works. `candleSharesBackbone`
already drops the token/sequence discriminator from the pool key for `modernbert`/`mmbert*`, so
one encoder is loaded and each task binds its head onto it — **weights are already shared; the
forward is not**. And `classify_multi_task`
(`candle-binding/src/model_architectures/lora/bert_lora.rs`) already runs the backbone once and
applies each adapter and head to the pooled output. It is unreachable for two reasons: its result
type hardcodes `intent`, `pii` and `security`, and there is no `extern "C"` export. The config
layer's name-keyed table has a counterpart one language down.

## Open questions

1. **Availability coupling.** A dedicated model fails one signal; a shared model fails every
   signal in its group at once. Each member still applies its own `on_error`, but the
   correlated-failure profile is new. Should group members declare `on_error` explicitly instead
   of inheriting `allow`?
2. **Co-location eligibility.** Sharing a pass across heads that disagree on input geometry is a
   regression — guard and PII run 8k/32k windowed while domain runs at 512. Co-location should be
   allowed only when tokenizer, window geometry and budget agree, which `binding.Limits` and
   `WindowCapability` already encode. Enforce at prepare, or warn?
3. **Text divergence.** `textForSignal` may return different text per signal type and the
   skip-compression set is per-request, so two members of a group can genuinely need two passes.
   Correctness comes from the text hash in the per-request cache key. Emit a metric when a group
   passes twice, or additionally reject provably incompatible pairs at load?
4. **Unused capabilities.** For a chat model the requested set shrinks the generated schema, which
   is strictly cheaper. For a multi-head model the unused heads are computed regardless — head
   projections against a shared trunk. Accept that silently, or report it?
5. **`deadline_ms` placement.** On the deployment, as proposed, or kept per binding?
6. **Envelope name.** `capability_results.v1` is consistent with the `capability:` selector.
7. **Metrics attribution.** `recordSignalExtraction` would charge one shared pass to every reading
   signal. A group-level duration plus a per-signal marker is needed.
8. **Response-direction signals** are out of scope here, but a response-stage group is the obvious
   next ask and the contract should not preclude it.
9. **`tasks.ClassificationBatch`** is the only existing three-signal type, and its own doc says it
   does not describe a joint model forward. `CapabilityResultSet` is the type that does. Retire
   the old one onto it, or leave it and record the gap?

## References

- `tools/agent/docs/architecture-risks.md` — `AR020`, `AR054`
- `tools/agent/docs/architecture-guardrails.md` — router ownership decomposition
- `pkg/classification/AGENTS.md` — family-owned adapters
- Remote-spelling consolidation: #2930, #3542
- [Unified Config Contract v0.3](./unified-config-contract-v0-3)
- [Prompt Classification Routing](./prompt-classification-routing)
