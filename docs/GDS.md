# Graph data science (ekg.gds)

All numbers from `reports/gds_summary.json` and `reports/gds_lift.json`, run 2026-10-17 on
Neo4j GDS 2.13.2 with a 1 GB heap.

## Communities of assets that fail together

Graph: 1,306 failed drives linked when they sit at the same site and failed within 14 days
of each other, weight 1 / (1 + days apart); 7,242 links. Louvain (weighted, undirected):
233 communities, modularity 0.978; WCC: 228 components, largest 36. 1,306 of the 1,522
failed drives share a community with at least one other; 216 failed alone at their site or
more than 14 days from any other failure there. 191 of 233 communities sit in one vault,
154 contain a single drive model.

Largest: community 535, 36 drives at SITE-00017 (vault 1124), three HGST/WDC 12-16 TB
models, failures from 2026-04-04 to 2026-05-30. Output: `reports/gds_communities.csv`,
`Drive.community` and `Drive.wcc` in the graph.

Honest reading: the generator places one Backblaze vault per site, so a community cannot
span sites by construction, which is where the 0.978 modularity comes from. What is real is
the vault-and-fortnight clustering of failures inside the telemetry; the site, product and
supplier labels attached to each community are generated. The useful fact for an operations
reader is the 191/233 single-vault share: failures cluster by physical vault, which is the
same thing Project 1's fleet-risk-by-vault table says.

## Critical parts by centrality

Graph: 10 products, 30 parts, 8 suppliers; Product-USES->Part weighted by dispatches that
consumed the part, Part-SUPPLIED_BY->Supplier; 458 undirected relationships. Degree,
dispatch-weighted degree and betweenness are written to each Part; 8 of 30 parts have a
single supplier. Top by betweenness: FAN-80MM (41.5; 8 products, 3 suppliers, 48 dispatches),
TPM-MOD (36.6; 9 products, 2 suppliers), NIC-25G (35.3; 66 dispatches), HBA-12G (35.2),
RAID-CTRL (27.1). Output: `reports/gds_part_centrality.csv`.

Honest reading: a 48-node graph. The ranking is correct for what it is, which is "parts
that many products use and that route to few suppliers", and Product-USES-Part is itself
derived from generated dispatches because the source has no bill of materials.

## FastRP embeddings

Projection: Drive, Asset, Site, Product, Account, Pod, Vault, DriveModel with SAME_AS,
HOSTS, IS_A, HAS, IN_POD, IN_VAULT, OF_MODEL, all undirected: 725,744 nodes, 3,604,810
relationships. No Case or Dispatch nodes (their timing follows the failure dates) and no
failure property, so the embedding encodes where a drive sits and what it is. FastRP, 32
dims, iteration weights [0, 1, 1, 1], seed 20261017: 359,101 drive embeddings in 16 s,
written to `reports/asset_embeddings.parquet` (46 MB, gitignored; `make gds` rebuilds it).

## Lift test: Project 1's failure model with and without the embedding

`ekg.gds lift` imports `sois.failure_model` from the sibling checkout and re-runs its own
feature builder, split (train 2026-04-08 to 04-24, test 05-25 to 05-31, 30-day horizon,
labels never shared), negative downsampling, drive hold-out, Platt calibration and LightGBM
parameters. Three arms, three seeds each, same rows (fit 479,083; test 2,469,958 drive-days,
2,123 positive). Embedding coverage of test rows: 100%.

| Arm | PR-AUC (mean of 3 seeds) | Recall in top 0.1% | Recall in top 1% | Brier |
|---|---:|---:|---:|---:|
| Baseline (Project 1 features) | 0.1002 | 0.204 | 0.501 | 0.000815 |
| Baseline + vault_id as a categorical | 0.0623 | 0.168 | 0.410 | 0.000836 |
| Baseline + 32-dim FastRP embedding | 0.0818 | 0.190 | 0.484 | 0.000822 |

Lift of the embedding: PR-AUC -0.018 (seed std at most 0.002), recall at 0.1% -0.014,
recall at 1% -0.017. Verdict written by the code: "embedding hurts PR-AUC beyond seed
noise". The baseline at seed 20261017 reproduces Project 1's reported 0.0985 exactly, so
the harness is the same model.

Why. The embedding is a smooth encoding of location identity (site, vault, pod) and product.
With 17 training dates and 404 failing test drives, location identity lets the trees
memorise which vaults failed in April, and that does not carry to late May; the plain
vault_id categorical does the same thing harder (-0.038). The graph structure does not add
signal the SMART attributes do not already carry, at least not at this horizon and with
this little training history. The result is reported as measured, and the README does not
claim graph features improve the model.

What would be a fair next try: an embedding over the co-failure graph with a time cutoff
(edges only among failures before the prediction date), or vault-level failure rates
computed causally, both of which are time-aware features rather than identity encodings.
Not done here.
