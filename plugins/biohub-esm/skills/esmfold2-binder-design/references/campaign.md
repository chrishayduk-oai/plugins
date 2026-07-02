# Binder campaign design and interpretation

The [world-model paper](https://www.biorxiv.org/content/10.64898/2026.06.03.729735v1)
and [Biohub release](https://biohub.ai/esm/protein) report experimentally
validated binder-design results. Reproducing useful hit rates requires the
released protocol, selection, and wet-lab screening—not a single generated
sequence.

## Plan

1. Define target construct/species/domain boundaries and experimental assay.
2. Select minibinder or scFv modality, framework/template set, and sequence
   constraints. Preserve exact starting sequences and digests.
3. Establish negative/off-target controls and any liabilities to filter.
4. Choose seeds/templates to yield hundreds and often around 1,000 candidates.
5. Estimate image build/cold start, GPU hours, persistent storage, candidate
   count, and cost. Obtain explicit confirmation.
6. Run deterministic, pinned jobs and preserve failed/partial outputs.
7. Rank with multiple signals: fold/interface confidence, critic evidence,
   structural plausibility, diversity, sequence liabilities, and controls.
8. Select a diverse experimental panel. Model confidence is not affinity,
   specificity, expression, stability, or safety.

## Smoke versus campaign

One seed with batch size one proves only that image build, weight load,
inference, persistence, and result retrieval connect correctly. Label it
`non-representative integration smoke`. Do not use its outcome to estimate hit
rate or claim the workflow is scientifically validated.

## Boundaries

Follow Biohub AUP and institutional biosafety review. Do not circumvent managed
sequence safeguards or assume open weights authorize prohibited work. Treat
designed sequences as hypotheses requiring synthesis, biochemical/biophysical
assays, specificity controls, and any additional validation appropriate to the
intended use.
