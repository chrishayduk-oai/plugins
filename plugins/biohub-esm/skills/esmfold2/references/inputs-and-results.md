# ESMFold2 inputs and results

## Serialized input entities

```json
{
  "sequences": [
    {"type": "protein", "id": "A", "sequence": "MKT...", "msa": null},
    {
      "type": "dna",
      "id": "B",
      "sequence": "GATAGCGCTATC",
      "modifications": [{"position": 5, "ccd": "C36"}]
    },
    {"type": "rna", "id": "R", "sequence": "ACGU"},
    {"type": "ligand", "id": "L", "ccd": ["SAH"]}
  ]
}
```

- IDs are unique strings or homomer ID lists.
- Modification positions are zero-based and must lie inside the entity.
- Ligands use exactly one of SMILES or a non-empty CCD list.
- Protein and RNA SDK types can carry an MSA; Fast ignores MSA, so reject that
  combination. For protein MSAs, query sequence/length must match the chain.
- The full model can run without an MSA. Do not invent one or claim it was used.
- The pinned SDK also serializes optional `pocket`, `distogram_conditioning`,
  and `covalent_bonds` root fields. Validate chain references, zero-based
  residue/atom indices, and exact square distogram dimensions before calling
  the provider. Reject unknown root/entity fields rather than silently passing
  typos through as valid requests.

## Artifact selection

- mmCIF: preferred all-atom output, especially for modified residues, ligands,
  nucleic acids, covalent bonds, or large chain sets.
- PDB: useful for compatibility but can lose identifiers/chemistry. If exported,
  retain the mmCIF source and conversion provenance.
- Confidence arrays and optional tensors: save as structured numeric files with
  shape/dtype metadata and checksums.

## Confidence interpretation

- pLDDT: local confidence, not experimental B factor, dynamics, or correctness.
- pAE: predicted aligned error between positions; inspect domains/interfaces.
- pTM: global topology confidence.
- iPTM and pair-chain iPTM: interface-level confidence for complexes; not
  binding affinity or functional validation.
- Distogram/embeddings: optional model outputs, not direct experimental claims.

Report the actual emitted scale. Current pinned ESMFold2 examples use values in
0–1, while many readers expect 0–100 pLDDT. Label any conversion explicitly.

Structure prediction returns one or more static conformations under model
assumptions. It does not establish kinetics, conformational ensembles,
thermodynamic stability, binding affinity, catalysis, or clinical utility.
