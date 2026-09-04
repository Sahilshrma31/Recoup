# Recoup — AI Revenue Recovery Agent

> A failed payment is not lost revenue. It becomes lost revenue when nobody takes the right next action.

Most payment stacks stop at *"payment failed."* Recoup answers the questions that are actually worth money:
**why** did it fail, **is it recoverable**, **what should we do**, **when**, and **when should we stop trying?**

```
Detect → Diagnose → Predict → Decide → Guard → Act → Verify
```

---

## What it does

A payment fails. Within seconds, Recoup:

1. **Detects** it from a Razorpay webhook (or a simulated event).
2. **Diagnoses** the cause — telling a flaky bank rail apart from a customer who genuinely cannot pay, even when both return the same ambiguous error code.
3. **Predicts** a recovery probability *per candidate action*, with a visible factor breakdown.
4. **Decides** by expected value: `amount × P(success) − cost of acting`.
5. **Guards** the decision through a deterministic policy engine that can veto the AI.
6. **Acts** — re-presents the charge, generates an alternative payment link, sends a reminder, or deliberately does nothing.
7. **Verifies** whether the money actually arrived, and feeds the result back.

Every step is recorded, so any decision answers *"why did the AI do this?"*

---

## The core architectural claim

**The LLM reasons. It does not move money.**

```
Transaction → Feature engine → Rule engine → AI reasoning → Action planner → Policy guard → Execution
                                              (advisory)                      (authoritative)
```

The model receives an already-computed deterministic analysis and proposes an action. That proposal is then **re-planned and re-guarded from scratch**: it can reorder the candidate actions, but it cannot skip a single check. Money is moved only by [`services/executor.py`](backend/app/services/executor.py), which the model cannot call.

Deterministic code owns: money arithmetic, eligibility, retry counts, limits, stopping conditions, idempotency, and API execution.

This is why the system has a real answer to the obvious objection:

> **If the AI fails, the payment system does not fail with it.**

A model outage, a rate limit, a malformed response, or a safety refusal all raise `LLMUnavailable`, and the agent continues on the deterministic path with degraded explanation quality and identical safety. A circuit breaker stops hammering a model that is down.

---

## Quick start

Runs fully offline. No Razorpay account and no API key required.

```bash
# 1. Backend
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m scripts.seed --transactions 10000 --reset   # synthetic dataset
.venv/bin/python -m uvicorn app.main:app --reload --port 8000

# 2. Frontend (new terminal)
cd frontend
npm install
npm run dev                                                      # http://localhost:5173
```

Then, in the dashboard: **Analyse 40** → **Advance clock**, or run one of the three pinned scenarios.

```bash
# 3. Measured baseline-vs-agent comparison
cd backend && .venv/bin/python -m scripts.experiment --limit 2500

# 4. Tests
cd backend && .venv/bin/python -m pytest tests/ -q

# 5. Read-only MCP server for merchant ops (optional)
cd backend && .venv/bin/python -m app.mcp_server
```

### Optional configuration

Copy `.env.example` to `.env`. Everything is optional:

| Variable | Effect when unset |
|---|---|
| `LLM_PROVIDER` | Defaults to `anthropic`; set `nvidia` to use NVIDIA NIM |
| `NVIDIA_API_KEY` / `ANTHROPIC_API_KEY` | Agent runs on the deterministic diagnosis engine |
| `RAZORPAY_KEY_ID` / `_SECRET` | Executes against an in-memory mock gateway |
| `RAZORPAY_WEBHOOK_SECRET` | **Signature verification is skipped** — see Security |
| `DATABASE_URL` | SQLite at `backend/data/recovery.db` |

`/health` reports the active posture, so a simulated demo can never be mistaken for live money movement.

---

## Measured results

Baseline (retry once, then send a reminder — what most merchants actually do) vs. the agent, on the same 2,500 transactions, paired on an identical random stream:

| | Baseline | Recoup | Δ |
|---|---:|---:|---:|
| Recovery rate | 29.6% | **51.7%** | +22.1 pp |
| Revenue recovered | ₹16.5 L | **₹30.0 L** | **+82%** |
| Futile retry rate | 60.5% | **20.8%** | −39.7 pp |
| Customers contacted | 95.2% | **71.0%** | −24.2 pp |
| Deliberately stopped | 0 | **1,090** | — |
| Avg recovery time | 102 min | **47 min** | −54% |

Reproduce with `python -m scripts.experiment --limit 2500`.

### How to read these honestly

- **These are simulated outcomes, not production data.** There is no live traffic behind them.
- **The comparison is designed not to be circular.** Outcomes are drawn from a hidden ground-truth model ([`services/simulator.py`](backend/app/services/simulator.py)) that is structurally *different* from the agent's scorecard: it is driven by a latent true cause and a per-customer willingness-to-pay. The agent never sees either — it sees only a **noisy emission** of that cause, exactly as a merchant would. `do_not_honour` can come from a flaky bank *or* an empty account; `unknown` can come from anything. The agent's job is genuine inference under uncertainty.
- **The baseline is not a straw man.** Retry-once-then-remind is standard practice and recovers real money (29.6%).
- **The agent arm is the *deterministic* agent** — no model calls, since this is thousands of decisions. So this is a **floor, not a ceiling**; the LLM layer adds ambiguity resolution on top.
- **By default each transaction is replayed as if it had just failed** (`--use-actual-age` for the other counterfactual), because comparing recovery policies on a three-week-old backlog mostly measures the staleness.
- **"Messages sent" goes up while "customers contacted" goes down.** That is not a contradiction: the agent bothers far fewer people, but the ones it does contact may get a link and then a follow-up.

---

## How the decisions work

### 1. Diagnosis — the inference that matters

Failure codes lie. The same `do_not_honour` covers a degraded rail and a customer who cannot pay. Recoup resolves it with context:

| Signal | Effect |
|---|---|
| Merchant-wide failure spike on the same rail, right now | Reclassify toward infrastructure |
| Long successful payment history | An ambiguous decline leans transient |
| No history + hard decline | Leans instrument |
| Repeated failures + ignored outreach | Category E — stop |

A spike claim requires a minimum sample, so one failed card payment in a quiet ten-minute window is never mistaken for an outage. A spike also never excuses a genuinely dead instrument.

### 2. Prediction — a transparent scorecard

An additive point score per action, squashed through a logistic curve rather than clipped, so stacked positive evidence compresses instead of piling up at "97% certain". Every contributing factor is shown in the UI. It needs no training data on day one, and it is a drop-in seam — replace `score_actions` with a trained model and nothing else changes.

### 3. Decision — expected value, including the cost of acting

```
net expected value = amount × P(success) − cost(action) − fatigue penalty
```

Each ignored message makes the next one cost more. This is why **stopping emerges from the economics** rather than from a special case.

### 4. Guardrails — the layer that can overrule the AI

Every rule runs on every decision, so the audit trail records what passed as well as what blocked.

| Check | Rule |
|---|---|
| `futile_retry` | Never re-present an instrument that cannot work (insufficient funds, dead card) |
| `attempt_limit` | Max 2 automatic retries |
| `outreach_limit` | Max 2 customer messages |
| `customer_opt_out` | Opted-out customers are never contacted (but may still be silently retried) |
| `probability_floor` | Below 20% recovery probability → no action |
| `expected_value_floor` | Expected recovery must justify the attempt |
| `recovery_window` | Nothing attempted after 14 days |
| `retry_cooldown` | Minimum gap between attempts |
| `amount_limit` | Above ₹10,000 → merchant approval required |

The signature behaviour:

```
Model says:  "Retry the payment."
                  ↓
Policy engine:  BLOCKED — insufficient_funds cannot be fixed by re-presenting the same instrument.
                  ↓
Executed:     CREATE_PAYMENT_LINK
```

Recorded as a policy override, visible in the UI and counted on the dashboard.

---

## Choosing the NVIDIA model

The reasoning layer runs on either **NVIDIA NIM** or **Anthropic**, selected by `LLM_PROVIDER`. The default is `nvidia/nemotron-3-super-120b-a12b`, and it was picked by measurement rather than reputation.

Two things had to be true, in this order:

1. **It must hold the schema.** The agent discards anything that fails `AgentAnalysis` validation, so a model that reasons brilliantly but wraps its JSON in prose is worth less here than a duller one that emits clean output every time.
2. **It must get the ambiguous case right.** The benchmark case is the one the LLM layer exists for: `do_not_honour` — a code that looks like a customer decline — on a customer with 14 successful payments, during a 4.1× UPI failure spike. The correct read is `A_TEMPORARY_TECHNICAL`, not `B_CUSTOMER_PAYMENT_ISSUE`. The deterministic engine hedges here; the model should not.

Then latency, because this runs per transaction.

| Model | Schema | Verdict | Latency |
|---|---|---|---:|
| **`nvidia/nemotron-3-super-120b-a12b`** | **strict `json_schema`** | **correct** | **6.4 s** |
| `openai/gpt-oss-20b` | strict `json_schema` | correct | 70.9 s |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | — | timed out (30 s) | — |
| `moonshotai/kimi-k3` | — | timed out (30 s) | — |

Both models that answered got it right. The 120B MoE is **11× faster** than the alternative, and at ~12B active parameters it is cheaper per call than its size suggests. Reproduce with `scripts/bench_models.py`.

Two consequences of not being on Anthropic, stated plainly:

- **Schema compliance is negotiated, not guaranteed.** `messages.parse` validates server-side; NVIDIA's `response_format` support varies per model. `NvidiaProvider` tries strict `json_schema`, steps down to `json_object`, then to prompt-only, and remembers whichever worked. Output is parsed defensively — code fences, `<think>` blocks and trailing prose are all stripped, and braces inside strings do not end the object.
- **No prompt caching and no adaptive thinking.** The system prompt is re-sent on every call. `AI_EFFORT` applies to Anthropic only.

`temperature` is pinned to `0`: this is a classification and a decision, so the same transaction must produce the same call twice, and a merchant asking "why?" deserves a stable answer.

None of this reaches the rest of the agent. Both providers return the same validated object, and both fail the same way — into the deterministic engine.

---

## Razorpay integration: SDK to act, MCP to ask

Recoup uses **both**, for deliberately different jobs.

### The official SDK moves the money

[`razorpay_client/live.py`](backend/app/razorpay_client/live.py) uses the official Python SDK — `razorpay.Client`, `order.create`, `payment_link.create`, `payment_link.notify_by`, `payment.fetch` — with the SDK's typed errors (`BadRequestError` / `GatewayError` / `ServerError`) mapped to retryable vs. non-retryable so the backoff policy is correct. Webhook signatures go through the SDK's own `verify_webhook_signature` rather than a hand-rolled HMAC.

### Recoup's MCP server answers questions — and only questions

Razorpay ships an MCP server so a model can *call* payment tools. Recoup deliberately does not give a model that ability, so it is not used in the execution path. That is the entire architectural claim: the model reasons, deterministic code acts. Putting an agent's tool loop between the policy engine and the gateway would hand back exactly the capability the design took away — the idempotency key, the retry budget and the approval gate all live in that gap.

So MCP is used for the other half of the problem: **a human asking questions.** [`app/mcp_server.py`](backend/app/mcp_server.py) exposes five tools, every one of them a `SELECT`:

| Tool | Answers |
|---|---|
| `get_transaction` | "What happened to `pay_92831`?" |
| `explain_decision` | "Why did it choose a payment link?" — scorecard, alternatives, all ten guardrail results |
| `recovery_metrics` | "How much did we recover this week?" |
| `list_at_risk` | "What are the biggest open items awaiting a link?" |
| `failure_breakdown` | "What's failing, and on which rail?" |

There is no `recover`, no `retry`, no `approve`. Acting stays behind the API's policy engine and its audit trail, and a test asserts that those tool names never appear here. Every tool is advertised with `read_only_hint: true`, and customer contact details are excluded from the payloads.

```bash
python -m app.mcp_server        # stdio
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "recoup": {
      "command": "/absolute/path/to/backend/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/absolute/path/to/backend"
    }
  }
}
```

---

## Safety properties

- **Bounded action space.** Six actions, not arbitrary API access.
- **Idempotency.** One decision authorises exactly one action. The attempt ordinal is derived from attempts already written — never from counters the action itself increments — and the key is also sent to Razorpay as the payment link `reference_id`, so a duplicate is rejected provider-side too.
- **Enforced state machine.** Illegal transitions raise. Callers that need a state walk the legal path rather than jumping, so no state is silently skipped.
- **Bounded provider retries.** Exponential backoff with jitter, then the attempt is marked `pending_manual` and handed to the merchant — never an unbounded loop against a payment API.
- **Minimal data to the model.** [`sanitise_for_llm`](backend/app/agent/prompts.py) is a closed allowlist: names, emails, phone numbers, card data and internal ids never leave the process. A new column cannot silently start leaking.
- **Structured output only.** The model must return a validated schema; anything else is discarded and the deterministic decision stands.

---

## Metric definitions

Stated explicitly, because "recovery rate" can mean five things.

| Metric | Definition |
|---|---|
| Revenue at risk | Total value of every failed / abandoned transaction |
| Revenue recovered | Value actually collected by a recovery action |
| Estimated recoverable | Recovered + (amount × P) for open, already-scored transactions — **a forecast** |
| Recovery rate | Recovered ÷ estimated recoverable |
| Action precision | Share of completed attempts that converted |
| False retry rate | Share of executed retries that did not convert |
| Avg recovery time | Failure → money (dominated by backlog age) |
| Avg agent recovery time | **First action → money** — the part the agent is responsible for |

The two recovery-time figures are separate on purpose. The agent-attributable one is recorded at verification time in simulated minutes, because the demo compresses wall-clock time and a naive subtraction would report seconds.

---

## Project layout

```
backend/
  app/
    agent/       features · diagnosis · predictor · planner · prompts · llm · providers · orchestrator
    policy/      rules · guardrails          ← can veto the AI
    razorpay_client/  base · live · mock · factory · webhooks
    services/    executor · verification · state_machine · simulator · runtime · analytics · activity
    api/         transactions · recovery · analytics · decisions · webhooks · stream · demo
    mcp_server.py     read-only MCP tools for merchant ops
  scripts/       seed.py · experiment.py
  tests/         72 tests, concentrated on the guardrails and the MCP boundary
frontend/
  src/pages/     Overview · Transactions · TransactionDetail · Experiment
  src/components/ Primitives · ActivityFeed (SSE)
```

---

## What is real and what is simulated

Being precise about this matters more than the demo looking impressive.

| Component | Status |
|---|---|
| Diagnosis, scoring, planning, policy engine, state machine, idempotency, audit trail | **Real** — same code in every mode |
| Webhook ingestion + SDK signature verification | **Real** |
| Payment link creation, order/payment fetch (official SDK) | **Real** against Razorpay test keys; mocked otherwise |
| Read-only MCP server | **Real** — queries the same tables the dashboard reads |
| Recovery outcomes | **Simulated** unless `RAZORPAY_LIVE=true`, where they come from polling real order status |
| Customer messaging (SMS/WhatsApp/email) | **Not built** — a reminder is logged and, in live mode, uses Razorpay's link notification |
| A/B numbers | **Simulated**, per the caveats above |

**On what "retry" can honestly mean:** Razorpay has no server-initiated re-charge for a *failed one-off payment* — the customer authorises each attempt. A retry is therefore implemented as re-presenting the same order through a fresh link tagged as a retry, rather than pretending the server can debit an account on its own. Recurring mandates are the exception, which is why `RETRY_SUBSCRIPTION` is a separate action.

---

## Security

- Credentials come from the environment; nothing is hardcoded and all defaults are empty.
- Webhook signatures are HMAC-verified when `RAZORPAY_WEBHOOK_SECRET` is set. **With no secret configured, verification is skipped** so the demo runs offline — `/health` reports `webhook_signature_verification: false`. Set the secret before pointing anything real at the endpoint.
- The `/api/demo/*` router is separated so a production deployment can simply not mount it.

---

## Roadmap

Real-time production event streaming · trained recovery model replacing the scorecard · multi-channel outreach (WhatsApp/SMS/email) with per-channel optimisation · merchant-specific policies · feedback learning from realised outcomes.
