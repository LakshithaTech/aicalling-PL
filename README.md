# Ramya — Personal Loan AI Voice Agent (Plivo + Sarvam)

This is an adaptation of the supplied HomeLoan_Hybrid_Agent source package.
It preserves the same WebSocket/HTTP, login, database, Sarvam STT and TTS, Plivo
calling, and English/Telugu language handling, while changing the *business
logic* to the provided Ramya personal-loan script.

## Where to edit

- `components/personal_loan_flow.py`: ALL fixed English/Telugu dialogue,
  question sequence, branching, amount extraction, summaries, optional estimate,
  callback, opt-out, and end-of-call logic. **Edit this file to change questions.**
- `components/prompts.py`: SHORT voice behavior / safety rules, instructions for
  unexpected customer questions, and JSON extraction. **No fixed flow here.**
- `main.py`: same Plivo/Sarvam API with personal-loan engine imported, loan type
  set to `personal_loan`, plus callback/opt-out DB confirmation.
- `components/questionnaire.py`: language helpers and opening-text integration.
- `components/db.py`, `database.py`: safely filter, delete and save only
  personal-loan records in corresponding operations.
- `tests/test_personal_loan_flow.py`: offline tests. `PERSONAL_LOAN_SCRIPT.md`:
  exact path map and human-readable prompt.

## Conversation

Opening (Ramya + real calling company + HDFC Bank options) -> new personal loan
or consolidate debts -> permission for ~5 minute initial check -> **salaried
employee only**. Unsalaried/business/unemployed customers exit politely.

- NEW LOAN: requested amount -> monthly credited salary -> existing loan EMI +
  card payments -> read back all 3 values -> confirm/correct -> optional
  calculator or advisor review -> ask to connect to advisor -> preferred
  callback day and time -> save callback request -> speak confirmation **only
  when persistence succeeds**.
- COMBINE DEBTS: credited salary -> accounts to combine -> total outstanding ->
  monthly combined repayments -> other separate EMIs (if yes, amount) ->
  advisor comparison (current versus possible single EMI and TOTAL cost,
  including charges) -> preferred callback day/time -> save and confirm.
- Every call: one question at a time; real stop-calling requests, refusals,
  busy customers, incorrect number and explicit language changes override the
  pending business question.

## Setup (Windows PowerShell)

```powershell
cd PersonalLoan_Hybrid_Agent
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Fill .env with your OWN working keys, number, company, DB and public HTTPS URL.
python -m unittest discover -s tests -v
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Start your existing ngrok tunnel separately for port 8000; set
`PUBLIC_BASE_URL` to its **current HTTPS** URL. Use the existing authorised
login, then POST a request like `{"to_number":"+91YOUR_TEST_NUMBER",
"customer_name":"Test Customer"}` to `/call`. Only call consented test
numbers. The application blocks outbound calls if it cannot verify the DNC
list, so use `schema.sql` to create `public.ai_agent_do_not_call` first.
Your pre-existing `public.ai_agent_calls` table must already be available.

## Calculator: important

The default, `ENABLE_PERSONAL_LOAN_ESTIMATE=0`, is correct for a deployment
WITHOUT an approved lender eligibility API. Ramya says that an advisor needs
to review the figures instead of inventing an HDFC approval or amount.

Optional *illustrative* affordability math can be turned on only after
setting ALL of: `ENABLE_PERSONAL_LOAN_ESTIMATE=1`,
`PERSONAL_LOAN_INTEREST_RATE`, `PERSONAL_LOAN_FOIR_RATIO`, and
`PERSONAL_LOAN_TENURE_MONTHS`. The calculated ceiling uses monthly credited
salary, existing monthly payments, a configured FOIR, tenure and rate. It is
NOT real HDFC Bank eligibility. Production should integrate an authorised
bank calculator rather than relying on this approximation. No rate, bank
approval, EMI saving, or appointment is promised by this project.

## Security and operational notes

The source ZIP supplied to ChatGPT contained an `.env`, customer recordings,
bytecode and an entire Windows virtual environment. **None of those are in
this output package.** Keep your real `.env` private; rotate any credentials
that have been shared inadvertently. Some database fields contain customer
financial data; restrict access, use customer consent, retention controls and
your legal requirements. Call HDFC Bank *only* as an approved partner, not as
a bank employee without verified authorisation. Ensure appropriate
telemarketing consents and DND/DLT compliance before a campaign.

The original telephony implementation ignores caller barge-in while AI is
speaking. This adaptation does not change its audio/STT latency or implement
an advisor-calendar appointment scheduler. A stored callback is a request
for your team to follow up, not a confirmed bank appointment. The tests are
local logic tests only, NOT a live Plivo/Sarvam/bank/DB end-to-end run.
