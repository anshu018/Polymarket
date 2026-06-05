# execution/GEMINI.md — Rules for Every Order in This Folder
# READ BEFORE WRITING ANY ORDER SUBMISSION CODE

## THE ONE RULE THAT GOVERNS THIS ENTIRE FOLDER

Every order submission must check idempotency first.
Always. Without exception. On every path. On every retry.

## EXACT SEQUENCE FOR EVERY ORDER — NO DEVIATIONS

Step 1: Generate UUID at trade decision time
Step 2: Write UUID to idempotency_log with status='pending'
        BEFORE the Polymarket API call goes out
Step 3: Submit order to Polymarket CLOB API
Step 4: On confirmation: update idempotency_log
        status = 'confirmed', confirmed_at = now()
Step 5: On any retry for any reason:
        Query idempotency_log for this UUID first
        If status = 'confirmed': STOP. Do not resubmit.
        If status = 'pending': resubmit is acceptable.

## IF SUPABASE IS UNAVAILABLE FOR THE IDEMPOTENCY CHECK

FAIL CLOSED.
Do not submit the order.
Alert Telegram immediately.
Log the failure with full context.
Stop.

"Supabase is probably fine" is not acceptable reasoning.
If the check cannot be confirmed: the order does not go out.
A missed trade costs nothing. A doubled position costs money.

## WHY THIS RULE EXISTS

Polygon network congestion causes HTTP timeouts after
order submission. The order may have landed or may not.
Without idempotency, a retry doubles the position,
violates Kelly sizing, and breaches risk limits —
silently, because risk_engine.py already ran and approved
one order. The idempotency layer is the only thing
preventing this failure mode.
