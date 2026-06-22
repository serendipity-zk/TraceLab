# output_attribution_schematic

**A conceptual illustration (not a data plot) of how a prior agent step's *output* tokens are
accounted in the *next* step's prompt: folded into the cached prefix, or re-sent as new billed
input.**

## What it shows

Each step is one horizontal stacked bar `[prefix | new input | output]`. Two cases are drawn with
fixed, representative segment lengths:

- **(a) Output cached as prefix (ideal reuse).** The next step's cached prefix equals the prior
  step's *entire* composition: `10 + 2 + 4 = 16`. The next step still has its own new input
  and output: `16 | 1 | 2`.
- **(b) Output re-sent as new input (observed).** The next step's cached prefix stops *before* the
  prior output (dashed line aligns with the prior `prefix + new input` boundary, `10 + 2 = 12`).
  The prior output is therefore re-sent as part of the next step's new, billed input: `4 + 1 = 5`,
  followed by the next step's output: `12 | 5 | 2`.

This figure motivates the data-driven companion analysis in `../output_append_assignment`, which
measures how often each case actually occurs across adjacent steps in the trace.

## Reproduce

```bash
uv run python plot.py            # one-LaTeX-column (compact) PNG + PDF
uv run python plot.py --full     # larger size for slides/inspection
```

Outputs `output_attribution_schematic.{png,pdf}` in this directory. The paper embeds the PDF
(copied to `paper/figures/output_attribution.pdf`, wired in via
`paper/figure-tex/fig_output_attribution.tex`).

## Notes / assumptions

- **No trace data is read.** Segment lengths are illustrative units chosen for clarity, not measured
  token counts. The only requirement the drawing encodes is the qualitative relationship: in (b) the
  re-sent output makes the next step's new input longer than the prior output.
- Palette uses the shared paper colors: dark blue = cached prefix, light blue = newly billed
  input, orange = output.

## SyFI result analysis

### output_attribution_schematic.png

The schematic (the paper's `fig:output_attribution`) lays out the two ways a prior step's output can
be accounted for in the next step's prompt. In **(a) output-cached**, the serving system keeps the
KV entries it produced during decode, so the prior step's entire composition — prefix, append, and
output, `10 + 2 + 4 = 16` units — folds into the next step's cached prefix, which then adds only its
own fresh append and output. In **(b) output-resend**, only the prior prefix and append are cached
(`10 + 2 = 12`); the prior output is dropped from the cache and re-sent as part of the next step's
billed append, so with one unit of genuinely new input that append becomes `4 + 1 = 5`. The
qualitative tell is in (b): the re-sent prior output makes the next step's new input longer than that
output. This drawing is purely illustrative (segment lengths are chosen units, not measured tokens);
the companion `output_append_assignment` experiment measures how often each case actually occurs.
