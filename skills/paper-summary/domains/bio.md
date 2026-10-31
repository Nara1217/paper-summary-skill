# Domain supplement — Life sciences / biomedical

Write a **도메인 보충: 생명과학 (Life sciences)** section covering the items
below that the paper addresses. Pull every number verbatim with a reference tag
(`Figure 2B`, `Table 1`, `Supplementary Fig. S3` — JATS ingests carry these
rendered labels, so use them). Omit items the paper does not cover, saying so
briefly rather than guessing.

Branch on `manifest.domain_subcategory`:

- `biorxiv:*` / `medrxiv:*` → **preprint, not peer-reviewed.** State this in the
  header line. If `manifest.published_doi` is set, say the paper has since been
  published and that the journal version may differ.
- `medrxiv:*`, or any clinical study → also fill **Clinical** below.
- otherwise → basic / translational research.

## Biological system & design
- **Model system** — organism/strain, cell lines (with identifiers), primary
  cells, organoids, patient samples; in vitro vs. in vivo vs. in silico.
- **Perturbation** — knockout/knockdown/overexpression, compound and dose,
  route, timing; how the perturbation was validated.
- **Controls** — vehicle, scrambled/non-targeting, isotype, sham, wild-type
  littermates; what is missing (mark as `(해석)`).

## Evidence & statistics
- **Sample size** — n per group, and whether n counts biological replicates
  (animals, donors, independent cultures) or technical replicates (wells,
  repeated measurements). Flag when this is unclear.
- **Statistics** — tests used, multiple-testing correction, effect sizes with
  CI/SEM/SD as reported. Copy p-values exactly; never round them.
- **Orthogonal validation** — is the key claim supported by more than one
  independent method (e.g. genetic + pharmacological, two antibodies)?
- **Blinding / randomization** — reported or not.

## Methods & data
- **Key techniques** — e.g. CRISPR screen, scRNA-seq, proteomics, structural
  method, imaging; critical reagents (antibody clones, sgRNAs).
- **Omics / computational** — dataset accessions (GEO, SRA, PRIDE, PDB),
  pipeline and thresholds, batch handling.
- **Availability** — data, code, materials statements.

## Clinical (only if applicable)
- **Design** — RCT / cohort / case-control / cross-sectional; registration
  (NCT id), population and inclusion criteria.
- **Endpoints** — primary vs. secondary; HR/OR/RR with 95% CI as reported.
- **Bias & generalisability** — confounding, loss to follow-up, single-centre.

## Translational relevance
- **Therapeutic implication** — target, modality, and how far the evidence is
  from a usable therapy (cell line → animal → human). Mark your own judgement
  `(해석)`.
- **Mechanism vs. correlation** — which claims are causal (shown by
  perturbation) and which are associative.
