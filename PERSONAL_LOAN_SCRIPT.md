# Ramya personal-loan agent — fixed script & implementation map

All English/Telugu dialogue strings are defined in
`components/personal_loan_flow.py` under `QUESTIONS` and `RESPONSES`.
These are the *spoken* English lines; brackets below represent controller
fields or conditional branches, not text to read literally.

## Opening and shared qualification

**Ramya:** "Hi, I’m Ramya, calling from HDFC Bank regarding personal loans,
Are you looking for a personal loan, or would you like to combine your existing
loans and credit card balances into one EMI?"

Wait for the answer. If the user says only yes, ask which option instead of
guessing. If no, close courteously.

**Ramya:** "With a few basic details, I can give an initial eligibility
estimate in about five minutes. Shall we check?"

If permitted, **Ramya:** "Are you currently working as a salaried employee?"
If not salaried, thank the customer and end the call immediately.

## New personal loan

1. "Approximately how much would you like to borrow?"
2. "How much salary gets credited to your account each month?"
3. "Roughly how much do you pay each month towards existing loan EMIs and
   credit card repayments combined?"
4. Read back all three amounts and ask if correct. If no, correct the
   selected field before proceeding.
5. If an explicitly enabled, correctly configured illustrative calculator
   provides a valid result, explain it as a preliminary, nonbinding estimate
   subject to HDFC Bank's review. Otherwise explain that an advisor needs
   to review eligibility.
6. "Would you like an advisor to call and explain your options, EMI, interest
   rate and charges?"

## Combining existing debts

1. "How much salary gets credited to your account each month?"
2. "Which loans or credit card balances would you like to combine?"
3. "Approximately how much is outstanding on those accounts altogether?"
4. "How much do you pay towards them each month?"
5. "Do you have any other monthly loan repayments that you would keep separate?"
   If yes: "Approximately how much do those other monthly EMIs total?"
6. "An advisor can compare your current payments with a proposed single EMI
   and the total cost, including charges. Would you like a callback?"

## Callbacks and decline

When the customer agrees to a callback or is busy:
"What day and time would suit you for a callback?"

If day/time is incomplete, ask only for the missing part. The actual
controller first saves the request to the database and **only after success**
returns: "Thank you. I’ve recorded your callback request for [day_and_time]."

When they decline, thank and end. Stop-calling requests are written to the
DNC table and the call ends. Do not promise lowered payments, guaranteed
approval or a rate without authorised verification.

## Small AI-only prompt

The separate `components/prompts.py` includes rules for: output only spoken
words; one question per message; 5–20 word natural replies; no fabricated
approvals, bank association or callbacks; language locked after the first
substantive response, switched only on explicit request; no sensitive
credentials; business questions owned strictly by the Python flow.
