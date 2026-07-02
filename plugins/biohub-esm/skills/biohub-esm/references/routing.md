# Routing details

The product distinctions come from the [Biohub protein world model](https://biohub.ai/esm/protein),
[get-started guide](https://biohub.ai/esm/protein/get-started), and
[Biohub/esm source](https://github.com/Biohub/esm).

## Decision order

1. If the goal is lookup/discovery across existing proteins, SAE features, or
   cluster context, choose Atlas. Use anonymous S3 only for bulk datasets.
2. If the goal is binder design, exclude Biohub managed inference. Choose Modal
   unless privacy/customization/owned infrastructure points to self-hosting.
3. If privacy, offline execution, data residency, custom heads, fine-tuning, or
   sustained ownership is required, choose pinned Hugging Face weights on
   user-owned compute. Public weights need no `HF_TOKEN`.
4. If work is many independent calls or long running, choose Modal. Public
   weights need Modal auth but no `ESM_API_KEY`.
5. Otherwise, choose Biohub managed inference for modest interactive ESMC or
   ESMFold2 and use `ESM_API_KEY`.

## ESMFold2 variant

- Full: accuracy-priority targets, difficult complexes, optional MSA.
- Fast: single-sequence latency and throughput; never promise MSA conditioning.

## Examples

| Request | Result |
| --- | --- |
| "Embed one protein and score mutations" | Biohub `esmc-600m-2024-12` by default |
| "Search for proteins with similar SAE signatures" | public Atlas API |
| "Fold this protein–DNA–ligand complex" | managed ESMFold2; full if accuracy/MSA needed |
| "Fold 500 designs" | Modal with pinned `biohub/ESMFold2-Fast` unless accuracy requires full |
| "Design minibinders" | Modal or self-hosted only |
| "My sequence cannot leave our VPC" | pinned self-hosted weights |

The `item_count > 32` code branch is a conservative orchestration heuristic to
surface scale-out early. It is not an account quota, credit limit, or provider
guarantee. Confirm actual capacity and cost before launch.
