"""Prompts used by vidchat.

The initial summary is deliberately designed for financial-strategy research:
it must separate what the video *claims* from what is verified, and call out
what is missing to reproduce or evaluate the strategy.
"""

SUMMARY_PROMPT = """\
You are analysing a YouTube video about a trading / investing strategy.
Answer strictly from the video source. Do not use outside knowledge except to
name what is missing. Wherever the integration exposes timestamps or source
citations, include them with the relevant claim.

For every performance figure, label it explicitly as "claimed in the video"
— never present it as a verified result. Distinguish what the speaker states
explicitly from what you infer, and say clearly what you could not establish.

Produce the following sections, in order, using Markdown headings:

# Strategy summary

## 1. Executive summary
Central thesis of the video and why the author believes the strategy works.

## 2. Strategy specification
Instruments, timeframe, entry rules, exit rules, filters, position sizing
and risk management. Quote rules exactly where possible.

## 3. Performance claims
Returns, drawdown, win rate, sample period, trade count and benchmark —
exactly as reported in the video. Label each figure as a claim, with the
sample period and benchmark when given. If a figure is not given, write
"not stated".

## 4. Evidence quality
Backtest vs live results, out-of-sample testing, transaction costs,
slippage, survivorship / sample-selection issues. Say what the video shows
and what it does not show.

## 5. Potential failure modes
Overfitting, regime dependence, tail risk, liquidity and execution
assumptions that could break the strategy.

## 6. Missing information
Everything that would be needed to reproduce or independently evaluate the
strategy and is not in the video.

## 7. Research opportunities
Concrete tests that could confirm or falsify the claimed edge, ordered by
importance.

End with a one-paragraph bottom line: what a sceptical researcher should
take away.
"""

# Reminder prepended to follow-up questions is intentionally *not* applied:
# NotebookLM answers are already source-grounded and the conversation keeps
# context server-side. Follow-up questions are sent verbatim.
