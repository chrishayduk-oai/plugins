# ESMC analysis guidance

The [current world-model paper](https://www.biorxiv.org/content/10.64898/2026.06.03.729735v1)
and [Biohub ESMC tutorials/model page](https://biohub.ai/models/esmc) describe
representation, zero-shot mutation, layer-sweep, and SAE workflows.

## Embeddings and hidden states

- Record whether the tensor is per-residue, mean-pooled, final-layer, or a
  selected hidden layer. These are not interchangeable.
- Exclude BOS/EOS and padding consistently before pooling.
- Compare proteins only when preprocessing, layer, model, and revision match.
- An embedding can drive a fitted downstream model, clustering, or retrieval;
  it is not itself a function label or phenotype.

## Entropy and mutations

For position `i`, mask the wild-type residue, obtain amino-acid logits, apply
log-softmax, and retain the exact vocabulary mapping. Then:

- entropy: `-sum_a P(a) log P(a)` over the intended amino-acid tokens
- mutation log-likelihood ratio: `log P(mut) - log P(wt)` in the same masked
  context

Low entropy or a negative mutation score can suggest constraint, but neither is
experimental fitness. Avoid comparing raw scores across models/revisions without
calibration. Preserve 1-based user residue numbering and 0-based tensor indices
explicitly.

## SAE features

SAE activations are learned, sparse directions with generated/curated biological
descriptions. Report feature model/layer, normalization, activation magnitude,
and residue regions. Treat labels as interpretive hypotheses and corroborate
with sequence, structure, Pfam/taxonomy, or experiments.

## Downstream heads and fine-tuning

An `AutoModelForSequenceClassification` head can be randomly initialized when
no fitted checkpoint exists. Never call its logits a biological prediction.
Fit on documented labels; split by homology/family where leakage matters;
calibrate and evaluate on held-out data; report class balance, uncertainty, and
domain shift. Fine-tuning belongs on self-hosted/private or controlled compute,
not the managed inference endpoint.
