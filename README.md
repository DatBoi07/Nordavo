# Roosterly Support Ticket Agent

A safe, explainable support agent for Roosterly (Dutch shift-planning SaaS) that answers customer tickets covered by the help centre (`data/faq.md`) and escalates everything else to a human with a concise English handoff note.

---

## How to run

```bash
# 1. Clone and set up
git clone https://github.com/DatBoi07/Nordavo.git
cd Nordavo
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 2. Install
pip install -e .

# 3. Configure
cp .env.example .env
# Fill HELPDESK_API_KEY, HELPDESK_EMAIL, and GEMINI_API_KEY

# 4. Run tests (34 offline unit tests, ~1s)
pytest -v

# 5. Evaluate (10 official gold examples + 10 synthetic edge cases)
python -m agent eval

# 6. Dry-run over all open tickets (reads and classifies, does not POST)
python -m agent run --dry-run

# 7. Final scored run (resets practice tickets, then handles all 30 live)
python -m agent reset-and-run
```

---

## Decisions

1. **Safety Lives in Code, Not Prompts.** The LLM extracts structured flags and metadata (`prompts.py`). Deterministic Python code (`policy.py`) applies the client's rules and decides every action, priority, and category. The LLM has no tools and cannot trigger actions. Customer text is wrapped in `<ticket_data>` tags and treated as untrusted data; prompt injection attempts (e.g. T030) are flagged and handed off, never obeyed.

2. **Direct Dutch Processing with English Auditability.** Tickets are processed in the customer's language, preserving exact Dutch menu names (e.g. `Team → Medewerker toevoegen`). An English audit trail is generated at every stage: `summary_en` and `reason_en` from the classifier, plus an independent English back-translation (`reply_en`) from the verifier model. The report (`reports/review.md`) lets a non-Dutch reviewer verify every decision without reading Dutch. Cost of this approach: one extra verifier LLM call per replied ticket; limitation: the verifier is also an LLM and shares blind spots with the drafter.

3. **Fail Toward a Human.** Whenever uncertainty arises the ticket becomes a `handoff`: classifier confidence below 0.75, verifier score below 0.75, regex and LLM flag disagreement, FAQ coverage gaps, LLM errors, or schema validation failures. An unsure escalation is never punished; a wrong reply to a customer is.

4. **Assumptions Adopted:**
   - *Mixed questions* (FAQ how-to + must-escalate topic, e.g. a question plus a refund request): entire ticket is a handoff; the note mentions the FAQ part so the human can answer both.
   - *Partially covered questions*: handoff (client rule: if the answer is not fully in the help centre, a human handles it).
   - *Bug reports & outages*: handoff. P1 if many users or a security issue, P2 if one customer is blocked, P3 if a workaround exists. Only a pure "where do I check for planned maintenance?" gets a reply pointing to F11.
   - *Feature requests and ideas*: reply with F10 (public roadmap, voting, no delivery promises), P4.
   - *GDPR/AVG requests*: always handoff, P2 (legal obligation, one customer, no outage).
   - *Security incident or legal threat*: handoff, P1.
   - *Refund, disputed charge, cancellation, contract change, threat to leave*: handoff, P2 minimum.
   - *Spam or empty*: `close` only if body is empty/whitespace, or classifier flags spam with confidence ≥ 0.9 and no must-escalate signal is present. Otherwise handoff. Closing a real customer is worse than a wasted human minute.
   - *Prompt injection in a ticket*: treated as data, flagged, and handed off with a note. Never obeyed, never closed silently.
   - *Customer plan awareness*: used in drafting. A Basis customer asking for Nmbrs/Loket.nl/AFAS exports or multiple locations is told these are Plus features and how to upgrade (F04, F05, F07). No upsell when the question does not need it.
   - *Tone and formality*: mirror the customer's register (informal `je/jij` by default for Dutch, `u/uw` if the customer writes formally). Sign off `Team Roosterly Support`.
   - *Menu names in replies*: kept exactly as in the FAQ (Dutch, as customers see the app), with the English meaning in brackets, even in English or German replies.
   - *Language field for spam or empty tickets*: best guess from the text; default `nl` if empty.
   - *Concurrency*: sequential processing. Simpler to explain; 30 tickets is small.
   - *Confidence value sent*: `min(classifier confidence, verifier score)` for replies; classifier confidence for handoffs.

5. **AI Tooling Transparency.** Built with Google Antigravity and Gemini models (`gemini-2.5-flash` for classification and drafting, `gemini-3.5-flash-lite` for independent verification). Every module was written to be explainable in a live review session.

---

## Reliability

- **503 & Network Drops:** retried with exponential backoff and jitter (0.5s base, up to 6 attempts). Nothing is saved on a 503, so retry is safe.
- **429 Rate Limiting:** client-side token bucket limiter at 4 req/s (under the 5 req/s ceiling); 429 responses parse and respect `Retry-After`.
- **409 Duplicate Prevention:** `already_handled` is treated as a terminal success state, reconciled in the local database, and never retried. A customer never gets two answers.
- **Crash Recovery & Idempotency:** SQLite WAL database (`state/agent.db`) records state transitions (`pending` → `decided` → `sending` → `sent`). On restart, `decided` tickets dispatch stored decisions without new LLM calls; `sending` tickets are reconciled via `GET /tickets/{id}` before resending.
- **Data Privacy:** IBANs (mod-97 validated) and credit card numbers (Luhn validated) are redacted to `[REDACTED_IBAN]` / `[REDACTED_CARD]` before LLM processing, in logs, and in draft responses. Secrets live only in `.env` (gitignored).
- **Injection Defence:** ticket text wrapped in `<ticket_data>` tags with closing-tag lookalikes escaped; outputs are schema-validated; drafts are checked against FAQ allowlists; `prompt_injection_attempt` forces handoff.

---

## Eval and fix

### Baseline vs. Post-Fix Performance

Evaluated against 10 official gold examples from `GET /examples` and 10 synthetic edge cases (`evals/data/extra_cases.json`):

| Metric | Baseline (Official) | After Fix (Official) | Synthetic Extra Cases |
|---|---|---|---|
| **Action Accuracy** | **100.0%** (10/10) | **100.0%** (10/10) | **100.0%** (10/10) |
| **Priority Accuracy** | 60.0% (6/10) | **100.0%** (10/10) | 90.0% (9/10) |
| **Category Accuracy** | 100.0% (10/10) | 90.0% (9/10) | 100.0% (10/10) |
| **Language Accuracy** | 100.0% (10/10) | 100.0% (10/10) | 100.0% (10/10) |
| **Safety Violations** | **0** (0/10) | **0** (0/10) | **0** (0/10) |
| **Escalation Recall** | 100.0% | 100.0% | 100.0% |
| **Over-escalation Rate** | 0.0% | 0.0% | 0.0% |
| **Exact Match (All 4)** | 60.0% (6/10) | **90.0%** (9/10) | **90.0%** (9/10) |

**Problem diagnosed:** Standard how-to questions (E02, E04, E05) were assigned P3 instead of P4 because routine UI workflows were misclassified as "workarounds" (`has_workaround = True`). Device requirement bugs (E01) were escalated to P2 because device crashes triggered an unconditional blocker floor.

**Systematic fix:** (1) added explicit `suggested_priority` field in the classifier schema with rigid P1–P4 definitions; (2) added policy logic ensuring pure FAQ replies default to P4 (or P3 for device-requirement bugs), while customer-blocked bugs without workarounds enforce P2; (3) bumped prompt version to `v2`. Priority accuracy reached 100% on the official examples.

### Cost per 100 Tickets

Based on the 30-ticket dry run with `gemini-2.5-flash` (input: \$0.10/Mtok, output: \$0.40/Mtok):

- **Input:** 65,161 tokens (avg 2,172/ticket) · **Output:** 20,087 tokens (avg 670/ticket)
- Cost per ticket = (2,172 / 10⁶ × 0.10 + 670 / 10⁶ × 0.40) × 0.92 = **€0.000446**
- **Cost per 100 tickets = €0.045**

Total cost for all 30 practice tickets: **~€0.013**, well under the €1 budget ceiling.

---

## What I would improve

1. **Native Dutch Speaker Review.** The dual-LLM back-translation and grounding checks verify facts and menu paths, but subtle Dutch tone nuances (e.g. regional Flemish idioms) would benefit from periodic native speaker spot-checks.
2. **Larger Labelled Evaluation Set.** With only 10 gold examples, confidence intervals are wide. A set of 50–100 labelled tickets would enable statistically meaningful regression testing and reduce overfitting risk.
3. **Combined Reply + Handoff Action.** The API forces a single decision per ticket. For suspected outages (e.g. T006), the ideal response is to immediately acknowledge the customer with the status page link (reply) while concurrently escalating to engineering (handoff).
4. **Human Feedback Loop.** Use outcomes of handoff tickets (what the human actually did) as training signal to calibrate the confidence threshold and improve classification over time.
5. **Calibrated Confidence Thresholding.** Replace the static 0.75 threshold with conformal prediction calibration based on historical outcomes.
