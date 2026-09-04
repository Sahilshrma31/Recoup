"""Benchmark NVIDIA-hosted models against Recoup's real analysis task.

Selection criteria, in order:
  1. Returns output matching our exact schema (non-negotiable -- the agent
     discards anything that does not validate).
  2. Gets the ambiguous case RIGHT: `do_not_honour` on a 14-time payer during
     a 4.1x rail spike is infrastructure, not inability to pay.
  3. Latency -- this runs per transaction.
"""
import json, os, sys, time
sys.path.insert(0, ".")
from openai import OpenAI

KEY = [l.split("=",1)[1].strip() for l in open("../.env")
       if l.startswith("NVIDIA_API_KEY=")][0]
client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=KEY, timeout=30.0)

from app.agent.prompts import AgentAnalysis, SYSTEM_PROMPT, build_user_message

SCHEMA = AgentAnalysis.model_json_schema()

FEATURES = {
    "amount_rupees": 7500.0, "currency": "INR", "method": "upi", "kind": "payment",
    "failure_reason": "do_not_honour", "is_subscription": False,
    "is_checkout_abandonment": False,
    "customer_previous_successful_payments": 14, "customer_previous_failed_payments": 1,
    "customer_success_rate": 0.93, "customer_is_new": False, "customer_opted_out": False,
    "days_since_last_payment": 12.0, "attempt_number": 1, "retry_count": 0,
    "outreach_count": 0, "minutes_since_failure": 2.0, "days_since_failure": 0.001,
    "payment_method_failure_rate": 0.11, "merchant_failure_rate": 0.14,
    "recent_failure_spike_ratio": 4.1, "recent_failure_spike": True,
    "historical_recovery_rate": 0.62,
}
USER = build_user_message(
    safe_features=FEATURES,
    diagnosis={"category":"B_CUSTOMER_PAYMENT_ISSUE","cause":"issuer_declined_unspecified",
               "confidence":0.55,"rationale":["Observed code do_not_honour is ambiguous"]},
    scored_actions=[
        {"action":"RETRY_DELAYED","probability":0.93,"expected_value_paise":699000,"net_expected_value_paise":698800},
        {"action":"RETRY","probability":0.75,"expected_value_paise":562000,"net_expected_value_paise":561800},
        {"action":"CREATE_PAYMENT_LINK","probability":0.68,"expected_value_paise":510000,"net_expected_value_paise":509600},
        {"action":"NO_ACTION","probability":0.12,"expected_value_paise":93000,"net_expected_value_paise":93000},
    ],
    deterministic_choice="RETRY_DELAYED",
    policy_notes=["All policy checks passed."],
)

CANDIDATES = [
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-super-120b-a12b",
    "openai/gpt-oss-20b",
    "moonshotai/kimi-k3",
    "deepseek-ai/deepseek-v4-pro-0813",
    "mistralai/mistral-large-2-instruct",
]

def probe(model, mode):
    kwargs = dict(
        model=model,
        messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":USER}],
        max_tokens=1400, temperature=0.2,
    )
    if mode == "json_schema":
        kwargs["response_format"] = {"type":"json_schema",
            "json_schema":{"name":"AgentAnalysis","schema":SCHEMA,"strict":True}}
    elif mode == "json_object":
        kwargs["response_format"] = {"type":"json_object"}

    t=time.monotonic()
    r = client.chat.completions.create(**kwargs)
    ms = int((time.monotonic()-t)*1000)
    text = r.choices[0].message.content or ""
    return ms, text

def evaluate(text):
    """Did it produce our schema, and did it get the ambiguous call right?"""
    raw = text.strip()
    if "```" in raw:
        raw = raw.split("```")[1].removeprefix("json").strip()
    start = raw.find("{")
    if start > 0: raw = raw[start:]
    try:
        obj = json.loads(raw)
    except Exception as e:
        return None, f"not JSON ({str(e)[:40]})"
    try:
        parsed = AgentAnalysis.model_validate(obj)
    except Exception as e:
        return None, f"JSON but schema invalid ({str(e)[:60]})"
    return parsed, None

print(f"{'model':44} {'mode':12} {'ms':>7}  {'valid':6} {'category':22} {'action':20}")
print("-"*128)
results=[]
for model in CANDIDATES:
    for mode in ("json_schema","json_object"):
        try:
            ms, text = probe(model, mode)
        except Exception as e:
            msg=str(e); code = "429" if "429" in msg else ("404" if "404" in msg else msg[:46])
            print(f"{model:44} {mode:12} {'-':>7}  ERROR  {code}")
            continue
        parsed, err = evaluate(text)
        if parsed is None:
            print(f"{model:44} {mode:12} {ms:>7}  NO     {err}")
            continue
        ok_cat = parsed.diagnosis_category == "A_TEMPORARY_TECHNICAL"
        print(f"{model:44} {mode:12} {ms:>7}  YES    "
              f"{parsed.diagnosis_category:22} {parsed.recommended_action:20} {'CORRECT' if ok_cat else 'wrong-cat'}")
        results.append((model, mode, ms, ok_cat, parsed))
        break   # first working mode per model is enough

print()
print("=== correct + valid, fastest first ===")
for m, mode, ms, ok, p in sorted([r for r in results if r[3]], key=lambda r: r[2]):
    print(f"  {m:44} {ms:>6}ms  via {mode}")
    print(f"       cause={p.cause}  conf={p.confidence}  action={p.recommended_action} delay={p.delay_minutes}")
    print(f"       reason: {p.reason[:150]}")
