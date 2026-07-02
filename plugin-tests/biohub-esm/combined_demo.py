#!/usr/bin/env python3
"""Live combined demonstration: fold, features, Atlas, align, and viewer."""

from __future__ import annotations

import argparse
import json
import os
import sys
from html import escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm"
sys.path.insert(0, str(ROOT / "scripts"))

from biohub_esm_lib.atlas import AtlasClient
from biohub_esm_lib.errors import APIError, BiohubESMError, ValidationError
from biohub_esm_lib.provenance import (
    artifact_record,
    build_provenance,
    input_digest,
    utc_now,
    verify_installed_vcs_revision,
    write_json_atomic,
)
from biohub_esm_lib.constants import ESM_GIT_REVISION
from biohub_esm_lib.security import redact

# Three substitutions from the benign ubiquitin control make a deterministic
# novel-sequence candidate while preserving a tractable homologous neighborhood.
BASE_SEQUENCE = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
NOVEL_CANDIDATES = (
    "MQVFVKTATGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGA",
    "MQVFVKTATGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLKLRGA",
    "MQVFVKTATGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVIKLRGA",
)


def global_alignment_pairs(query: str, target: str) -> list[tuple[int, int]]:
    """Needleman-Wunsch index pairs for matched/mismatched nongap residues."""

    rows, columns = len(query) + 1, len(target) + 1
    score = [[0] * columns for _ in range(rows)]
    trace = [[""] * columns for _ in range(rows)]
    for i in range(1, rows):
        score[i][0] = -i
        trace[i][0] = "U"
    for j in range(1, columns):
        score[0][j] = -j
        trace[0][j] = "L"
    for i in range(1, rows):
        for j in range(1, columns):
            options = (
                (score[i - 1][j - 1] + (1 if query[i - 1] == target[j - 1] else -1), "D"),
                (score[i - 1][j] - 1, "U"),
                (score[i][j - 1] - 1, "L"),
            )
            score[i][j], trace[i][j] = max(options, key=lambda item: item[0])
    pairs: list[tuple[int, int]] = []
    i, j = len(query), len(target)
    while i or j:
        direction = trace[i][j]
        if direction == "D":
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif direction == "U":
            i -= 1
        else:
            j -= 1
    return list(reversed(pairs))


def parse_ca_coordinates(pdb: str) -> list[tuple[float, float, float]]:
    coordinates = []
    seen: set[tuple[str, str, str]] = set()
    for line in pdb.splitlines():
        if not line.startswith(("ATOM  ", "HETATM")) or line[12:16].strip() != "CA":
            continue
        key = (line[21:22], line[22:26], line[26:27])
        if key in seen:
            continue
        seen.add(key)
        coordinates.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
    return coordinates


def align_pdb(
    query_pdb: str,
    hit_pdb: str,
    query_sequence: str,
    hit_sequence: str,
) -> tuple[str, float, int]:
    try:
        import numpy as np
    except ImportError as exc:
        raise ValidationError("combined demo alignment requires numpy from the pinned ESM environment") from exc
    query_ca = parse_ca_coordinates(query_pdb)
    hit_ca = parse_ca_coordinates(hit_pdb)
    pairs = [
        (query_index, hit_index)
        for query_index, hit_index in global_alignment_pairs(query_sequence, hit_sequence)
        if query_index < len(query_ca) and hit_index < len(hit_ca)
    ]
    if len(pairs) < 3:
        raise ValidationError("fewer than three aligned C-alpha positions are available")
    target = np.asarray([query_ca[i] for i, _ in pairs], dtype=float)
    moving = np.asarray([hit_ca[j] for _, j in pairs], dtype=float)
    target_center = target.mean(axis=0)
    moving_center = moving.mean(axis=0)
    target_zero = target - target_center
    moving_zero = moving - moving_center
    u, _, vt = np.linalg.svd(moving_zero.T @ target_zero)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u[:, -1] *= -1
        rotation = u @ vt
    transformed_pairs = moving_zero @ rotation + target_center
    rmsd = float(np.sqrt(np.mean(np.sum((transformed_pairs - target) ** 2, axis=1))))

    output_lines = []
    for line in hit_pdb.splitlines():
        if line.startswith(("ATOM  ", "HETATM")) and len(line) >= 54:
            xyz = np.asarray([float(line[30:38]), float(line[38:46]), float(line[46:54])])
            moved = (xyz - moving_center) @ rotation + target_center
            line = f"{line[:30]}{moved[0]:8.3f}{moved[1]:8.3f}{moved[2]:8.3f}{line[54:]}"
        output_lines.append(line)
    return "\n".join(output_lines) + "\n", rmsd, len(pairs)


def viewer_html(summary: dict[str, Any], aligned_files: list[str]) -> str:
    rows = "".join(
        f"<tr><td>{index + 1}</td><td>{escape(str(hit['accession']))}</td>"
        f"<td>{hit['similarity']:.3f}</td>"
        f"<td>{hit['rmsd']:.2f} Å</td><td>{hit['aligned_positions']}</td></tr>"
        for index, hit in enumerate(summary["hits"])
    )
    feature_rows = "".join(
        f"<li><b>Feature {item['feature_index']}</b> — {escape(str(item['label']))}: "
        f"{escape(str(item['summary']))}</li>"
        for item in summary["features"]
    )
    files = json.dumps(["query.pdb", *aligned_files])
    colors = json.dumps(["#55d6be", "#ff8f70", "#b58cff"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Biohub ESM combined demonstration</title>
<script src="https://unpkg.com/ngl@2.0.0-dev.39/dist/ngl.js"></script>
<style>
:root{{--bg:#071a1c;--panel:#10292a;--ink:#effffb;--muted:#9fc7c2;--line:#28504d;--accent:#55d6be}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 Inter,system-ui,sans-serif}}
header{{height:76px;padding:18px 26px;border-bottom:1px solid var(--line);display:flex}}
header{{justify-content:space-between;align-items:center}}
h1{{font-size:22px;margin:0}}
.tag{{color:var(--accent);font-weight:700;letter-spacing:.08em;text-transform:uppercase;font-size:11px}}
main{{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(360px,.85fr);height:calc(100vh - 76px)}}
#viewport{{min-height:620px;position:relative}}
aside{{background:var(--panel);padding:22px;overflow:auto;border-left:1px solid var(--line)}}
.metric-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:14px 0}}
.metric{{background:#173536;padding:12px;border-radius:9px}}
.metric b{{display:block;font-size:20px}} .metric span{{color:var(--muted);font-size:11px;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left}}
h2{{font-size:14px;margin:22px 0 8px}} ul{{padding-left:20px}}
li{{margin:8px 0;color:var(--muted)}} li b{{color:var(--ink)}}
.legend{{position:absolute;z-index:2;left:18px;bottom:18px;background:#071a1cdd}}
.legend{{padding:10px 13px;border:1px solid var(--line);border-radius:8px}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}}
.warning{{color:#f5d66d;font-size:12px}}
</style></head><body>
<header><div><div class="tag">Live evidence · Biohub ESM</div>
<h1>Fold → functional features → Atlas neighborhood → structural alignment</h1></div>
<div>{escape(str(summary['timestamp']))}</div></header>
<main><section id="viewport"><div class="legend">
<div><span class="dot" style="background:#55d6be"></span>Novel query</div>
<div><span class="dot" style="background:#ff8f70"></span>Atlas hit 1</div>
<div><span class="dot" style="background:#b58cff"></span>Atlas hit 2</div></div></section>
<aside><div class="tag">Structure Viewer</div><h2>Managed ESMFold2-Fast prediction</h2>
<div class="metric-grid">
<div class="metric"><b>{summary['fold']['mean_plddt']:.3f}</b><span>mean pLDDT</span></div>
<div class="metric"><b>{summary['fold']['ptm']:.3f}</b><span>pTM</span></div>
<div class="metric"><b>{escape(str(summary['fold']['model']))}</b><span>model</span></div>
</div>
<p class="warning">Static model hypothesis; experimental validation required.</p>
<h2>Atlas structural comparison</h2><table><thead><tr>
<th>#</th><th>Accession</th><th>Similarity</th><th>Cα RMSD</th><th>Aligned</th>
</tr></thead><tbody>{rows}</tbody></table>
<h2>Learned functional-neighborhood features</h2><ul>{feature_rows}</ul>
<h2>Provenance</h2><p>Model, route, input digest, parameters, timestamps, raw responses,
and artifact checksums are preserved alongside this viewer.</p></aside></main>
<script>
const files={files}, colors={colors}; const stage=new NGL.Stage('viewport',{{backgroundColor:'#071a1c'}});
Promise.all(files.map((file,i)=>stage.loadFile(file).then(c=>{{
  c.addRepresentation('cartoon',{{color:colors[i],opacity:i?0.72:1}}); return c;
}}))).then(()=>{{stage.autoView(); document.body.dataset.ready='true';}})
  .catch(e=>{{document.body.dataset.ready='error'; document.body.dataset.error=String(e);}});
window.addEventListener('resize',()=>stage.handleResize());
</script></body></html>"""


def artifact_media_type(path: Path) -> str:
    return {
        ".cif": "chemical/x-mmcif",
        ".html": "text/html",
        ".json": "application/json",
        ".pdb": "chemical/x-pdb",
    }[path.suffix.lower()]


def run(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValidationError(
            "combined demo requires an empty output directory to prevent stale evidence"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("ESM_API_KEY", "").strip():
        raise APIError(status=None, kind="missing-credentials", message="ESM_API_KEY is missing")
    try:
        from esm.sdk import esmfold2_client
        from esm.sdk.api import ESMProteinError, FoldingConfig
        from esm.utils.structure.input_builder import ProteinInput, StructurePredictionInput
    except ImportError as exc:
        raise ValidationError("pinned Biohub esm SDK is not installed") from exc
    esm_revision = verify_installed_vcs_revision("esm", ESM_GIT_REVISION)

    started = utc_now()
    atlas = AtlasClient(timeout=240)
    provider_calls: list[dict[str, Any]] = []
    selected_sequence = None
    search = None
    for candidate in NOVEL_CANDIDATES:
        call_started = utc_now()
        candidate_search = atlas.search(
            candidate, topk_results=3, topk_features=5, include_cluster_info=True
        )
        provider_calls.append(
            {
                "method": "GET",
                "endpoint": (
                    "https://biohub.ai/esm/protein/api/v1alpha1/"
                    "similarity-search"
                ),
                "started_at": call_started,
                "finished_at": utc_now(),
                "parameters": {
                    "input_sha256": input_digest(candidate),
                    "topk_results": 3,
                    "topk_features": 5,
                    "include_cluster_info": True,
                },
            }
        )
        if candidate_search.get("protein_hash") is None:
            selected_sequence, search = candidate, candidate_search
            break
    if selected_sequence is None or search is None:
        raise ValidationError("all deterministic demo candidates already exist in Atlas")
    if len(search["similar_proteins"]) < 2:
        raise ValidationError("combined demo requires at least two Atlas hits")

    model_id = "esmfold2-fast-2026-05"
    client = esmfold2_client(
        model=model_id,
        url="https://biohub.ai",
        token=os.environ["ESM_API_KEY"],
        request_timeout=180,
    )
    fold_input = StructurePredictionInput(
        sequences=[ProteinInput(id="A", sequence=selected_sequence)]
    )
    config = FoldingConfig(num_loops=3, num_sampling_steps=20, include_pae=True)
    call_started = utc_now()
    fold = client.fold_all_atom(fold_input, config=config)
    provider_calls.append(
        {
            "method": "POST",
            "endpoint": "https://biohub.ai/api/v1/fold_all_atom",
            "started_at": call_started,
            "finished_at": utc_now(),
            "parameters": {
                "model": model_id,
                "input_sha256": input_digest(selected_sequence),
                "num_loops": 3,
                "num_sampling_steps": 20,
                "include_pae": True,
            },
        }
    )
    if isinstance(fold, ESMProteinError):
        raise APIError(status=None, kind="provider", message="managed ESMFold2 returned a model error")
    query_cif = output_dir / "query.cif"
    query_pdb = output_dir / "query.pdb"
    query_cif.write_text(fold.complex.to_mmcif(), encoding="utf-8")
    query_pdb_text = fold.complex.to_protein_complex().to_pdb_string()
    query_pdb.write_text(query_pdb_text, encoding="utf-8")
    write_json_atomic(output_dir / "atlas-search-raw.json", search)

    feature_details = []
    for feature in search.get("top_features_across_results", [])[:3]:
        feature_index = int(feature["feature_index"])
        call_started = utc_now()
        detail = atlas.feature(feature_index)
        provider_calls.append(
            {
                "method": "GET",
                "endpoint": (
                    "https://biohub.ai/esm/protein/api/v1alpha1/features/"
                    f"{feature_index}"
                ),
                "started_at": call_started,
                "finished_at": utc_now(),
                "parameters": {},
            }
        )
        feature_details.append(
            {
                "feature_index": detail["feature_index"],
                "label": detail["label"],
                "summary": detail.get("summary") or detail.get("description", "")[:180],
            }
        )
    if not feature_details:
        raise ValidationError("Atlas search returned no functional features for the demo")
    write_json_atomic(output_dir / "atlas-feature-details.json", feature_details)

    aligned_files: list[str] = []
    hit_summaries: list[dict[str, Any]] = []
    raw_proteins: list[dict[str, Any]] = []
    for index, hit in enumerate(search["similar_proteins"][:2], start=1):
        protein_hash = hit["protein_hash"]
        call_started = utc_now()
        protein = atlas.protein(protein_hash, topk_features=5, fold_on_miss=False)
        provider_calls.append(
            {
                "method": "GET",
                "endpoint": (
                    "https://biohub.ai/esm/protein/api/v1alpha1/proteins/"
                    f"{protein_hash}"
                ),
                "started_at": call_started,
                "finished_at": utc_now(),
                "parameters": {"topk_features": 5, "fold_on_miss": False},
            }
        )
        raw_proteins.append(protein)
        hit_pdb = protein.get("pdb") or hit.get("pdb")
        hit_sequence = protein.get("sequence")
        if not isinstance(hit_pdb, str) or not isinstance(hit_sequence, str):
            raise ValidationError(f"Atlas hit {index} lacks sequence or PDB structure")
        raw_path = output_dir / f"hit-{index}-raw.pdb"
        raw_path.write_text(hit_pdb, encoding="utf-8")
        aligned, rmsd, aligned_positions = align_pdb(
            query_pdb_text, hit_pdb, selected_sequence, hit_sequence
        )
        aligned_path = output_dir / f"hit-{index}-aligned.pdb"
        aligned_path.write_text(aligned, encoding="utf-8")
        aligned_files.append(aligned_path.name)
        hit_summaries.append(
            {
                "protein_hash": hit["protein_hash"],
                "accession": hit.get("protein_accession", "unknown"),
                "similarity": float(hit["similarity_score"]),
                "rmsd": rmsd,
                "aligned_positions": aligned_positions,
            }
        )
    write_json_atomic(output_dir / "atlas-proteins-raw.json", raw_proteins)
    first_rep = raw_proteins[0].get("cluster_rep_protein_hash") or hit_summaries[0]["protein_hash"]
    call_started = utc_now()
    cluster = atlas.cluster(first_rep, topk_features=5)
    provider_calls.append(
        {
            "method": "GET",
            "endpoint": (
                "https://biohub.ai/esm/protein/api/v1alpha1/clusters/"
                f"{first_rep}"
            ),
            "started_at": call_started,
            "finished_at": utc_now(),
            "parameters": {"topk_features": 5},
        }
    )
    write_json_atomic(output_dir / "atlas-cluster.json", cluster)

    summary = {
        "timestamp": utc_now(),
        "novel_query_confirmed_by_atlas_miss": True,
        "input_sha256": input_digest(selected_sequence),
        "fold": {
            "model": model_id,
            "mean_plddt": float(fold.plddt.float().mean()),
            "ptm": float(fold.ptm),
            "iptm": float(fold.iptm) if fold.iptm is not None else None,
        },
        "features": feature_details,
        "hits": hit_summaries,
        "cluster": {
            "representative_hash": first_rep,
            "size": cluster.get("cluster_size"),
            "pct_characterized": cluster.get("cluster_pct_characterized"),
        },
        "limitations": [
            "Static model hypothesis; not dynamics or experimental truth.",
            "Atlas and SAE interpretations are alpha/learned hypotheses.",
            "C-alpha RMSD uses global sequence alignment and rigid-body Kabsch superposition.",
        ],
    }
    summary_path = output_dir / "workflow-summary.json"
    write_json_atomic(summary_path, summary)
    viewer_path = output_dir / "viewer.html"
    viewer_path.write_text(viewer_html(summary, aligned_files), encoding="utf-8")

    artifact_paths = [
        query_cif,
        query_pdb,
        output_dir / "atlas-search-raw.json",
        output_dir / "atlas-feature-details.json",
        output_dir / "atlas-proteins-raw.json",
        output_dir / "atlas-cluster.json",
        summary_path,
        viewer_path,
        *[output_dir / name for name in aligned_files],
        output_dir / "hit-1-raw.pdb",
        output_dir / "hit-2-raw.pdb",
    ]
    artifacts = [
        artifact_record(path, media_type=artifact_media_type(path))
        for path in artifact_paths
    ]
    provenance = build_provenance(
        route="biohub+atlas-api",
        endpoint="https://biohub.ai/api/v1/fold_all_atom; https://biohub.ai/esm/protein/api/v1alpha1",
        model_id=model_id,
        model_revision=model_id,
        inputs=None,
        input_sha256=input_digest(selected_sequence),
        parameters={
            "fold": {"num_loops": 3, "num_sampling_steps": 20, "include_pae": True},
            "atlas": {"topk_results": 3, "topk_features": 5, "include_cluster_info": True},
            "alignment": "global-sequence + C-alpha Kabsch",
        },
        seed=None,
        started_at=started,
        artifacts=artifacts,
        confidence_metrics={
            "mean_plddt": summary["fold"]["mean_plddt"],
            "ptm": summary["fold"]["ptm"],
            "atlas_similarity": [hit["similarity"] for hit in hit_summaries],
            "ca_rmsd_angstrom": [hit["rmsd"] for hit in hit_summaries],
        },
        provider_calls=provider_calls,
        esm_git_revision=esm_revision,
    )
    provenance_path = output_dir / "provenance.json"
    write_json_atomic(provenance_path, provenance)
    return redact(
        {
            "status": "passed",
            "viewer": str(viewer_path),
            "summary": str(summary_path),
            "provenance": str(provenance_path),
            "novel_sequence_sha256": input_digest(selected_sequence),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                run(Path(args.output_dir).resolve()),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        )
        return 0
    except Exception as exc:
        error = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
        print(json.dumps(redact(error), indent=2, allow_nan=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
