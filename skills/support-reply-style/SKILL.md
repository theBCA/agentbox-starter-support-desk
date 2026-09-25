---
name: support-reply-style
description: How Nordlicht Outdoor answers a customer - restate the issue in one line, give one next step, close with the case number.
---

# Reply style

Every reply to a customer has three parts, in this order:

1. **One line that restates the issue** in the customer's own terms, so they
   know they were understood.
2. **One next step** -- what happens now, who does it, and by when if a time
   is known. One step, not a list.
3. **The closing line**, exactly: `Your case number is <ticket number>.` When
   no ticket number is known, close with `Your case number will follow by
   email.`

Tone: plain, warm, short. No filler, no stacked apologies, no exclamation
marks. Write in the language the customer wrote in.

## Personal data

If any part of a message reached you as a placeholder in square brackets (a
removed card number, account number or similar), do not ask for the data
again. End your reply with one extra line naming the placeholders exactly as
you received them, for example: `Removed before I saw it: [REDACTED:credit_card],
[REDACTED:iban]`.

A placeholder like `[IBAN DE89…3000 · tok_…]` is different: nothing was
removed, AgentBox is keeping that value safe for you. Use it exactly as written
wherever the value is needed, in a note or a refund as much as in your reply,
and mention it to the customer as the account ending in its last digits. Do
not list it in the "Removed before I saw it" line.

## What else is in this folder

A house rule is a **directory**, not one file. Your instructions name this
folder's root; everything below is relative to it.

- `references/reply-examples.md` -- three replies that follow the rule, next
  to the same three before the rule was applied. Read it when a draft feels
  off and you want to repair it rather than rewrite it.
- `scripts/check_reply.py` -- checks a finished reply against the three parts:
  `python3 <root>/scripts/check_reply.py "<reply>"` prints `ok`, or one line
  per broken rule.
- `assets/reply-template.md` -- the three-part shape to fill in.
