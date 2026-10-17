# Glossary (rendered from ontology/enterprise.ttl; do not edit by hand)

One definition per term. `Source` names the Project 1 table, column or file the term is read from;
`Rule` says how a derived term is computed. The same text is loaded into Neo4j as `:Term` nodes,
and every entity node links to its term with `INSTANCE_OF`.

## Entities

### Account
Neo4j label: `Account`  
A customer company. One Account per real company, whatever spelling or account_id the CRM rows carry; the CRM rows themselves are AccountRecord nodes linked to their Account by a scored SAME_AS edge.  
Source: account.csv (CRM), truth/account_resolution.csv for the canonical id  
Also called: client, company, customer  

### Account Record
Neo4j label: `AccountRecord`  
One row of the CRM account table as the source system holds it: account_row_id, account_id, account_name, source_system. Several records can describe one Account (MESS_RULES A1 to A3).  
Source: account.csv  

### Case
Neo4j label: `Case`  
A support ticket about one installed asset: opened_at, closed_at, category, severity, description, resolution_code, failure_reported, source_system. Timestamps come in two formats by source system (MESS_RULES D1); the graph stores them parsed to ISO plus the original string.  
Source: case.csv (support)  
Also called: incident, support case, ticket  

### Dispatch
Neo4j label: `Dispatch`  
A technician visit scheduled for a case: scheduled_at, visit_number, outcome (resolved, parts_not_available, no_fault_found, incomplete), and the parts used.  
Source: dispatch.csv, dispatch_part.csv  
Also called: field service visit, truck roll, visit  

### Drive (telemetry record)
Neo4j label: `Drive`  
The telemetry's view of one physical drive: serial_number, model, datacenter, vault, pod, first and last day seen, failure date, last SMART 5/187/197 raw values and Project 1's calibrated 30-day failure probability. Daily telemetry rows stay out of the graph; only this per-drive summary is loaded.  
Source: telemetry_drive_summary (Postgres), data/parquet/telemetry_daily for the last SMART values, models/test_scores.parquet for p_fail_30d  

### Installed Asset
Neo4j label: `Asset`  
A unit of hardware the ERP says is installed at a Site: asset_id, serial_number, product, install_date, warranty_end, status. Every installed asset is one real drive from the telemetry; the ERP serial may carry a vendor prefix or lowercase (MESS_RULES S1, S2), so the telemetry Drive record is linked to the Asset by a scored SAME_AS edge.  
Source: installed_asset.csv (ERP); telemetry_drive_summary (Postgres) for the drive side  
Also called: asset, drive, installed base, unit  

### Part
Neo4j label: `Part`  
A field-replaceable component (part_id, part_number, category, unit cost) that a dispatch can consume: drive sleds by capacity, PSUs, fans, DIMMs, controllers, backplanes, NICs, cables, boards, chassis.  
Source: part.csv, dispatch_part.csv  
Also called: FRU, component, spare  

### Product
Neo4j label: `Product`  
A sellable SKU (product_id, name, family, drive bays, list price, capacity class). Ten generic SKUs in five families; none is a real product.  
Source: product.csv (ERP)  
Also called: SKU, model, product family  

### Runbook
Neo4j label: `Runbook`  
A written procedure (title, product family, category, symptoms, numbered steps) that covers one product family. A case whose symptoms match a runbook may be deflectable.  
Source: runbook.csv  
Also called: KB article, knowledge article, procedure  

### Site
Neo4j label: `Site`  
A customer location (address, city, region) that belongs to one Account and hosts installed assets. Drives that share a Backblaze (datacenter, vault) are placed at one site, so a site is also the physical failure-clustering unit.  
Source: site.csv  
Also called: customer site, data center, location  

### Supplier
Neo4j label: `Supplier`  
A company that supplies parts. Each part has one to three suppliers with a lead time and a unit cost; the first is marked preferred.  
Source: supplier.csv, part_supplier.csv  
Also called: vendor  

### Technician
Neo4j label: `Technician`  
A field engineer with a home region, a skill list (storage, server, network, firmware, hci) and a hire date, who performs dispatches in their region.  
Source: technician.csv  
Also called: engineer, field engineer, tech  

## Measures (derived terms)

### Deflectable Case
A case the customer could have closed from a matching runbook without a technician: how_to, config or firmware questions at severity 3 or 4 with a covering runbook, and false alarms. The graph marks a case deflectable only from ground truth (truth/case_truth.csv.deflectable); predicting it is Project 3's job.  
Source: truth/case_truth.csv.deflectable, deflect_reason, matching_runbook_id  
Rule: ground truth flag; generator draws it with probability 0.85 for how_to/config/firmware at severity 3-4 with a matched runbook, 0.6 for false_alarm, 0.03 otherwise  
Also called: avoidable case, self-service case  

### Fleet Health
For a site, account or the whole fleet on the scoring date: the expected number of drive failures in the next 30 days, the sum of Project 1's calibrated p_fail_30d over the drives, together with the count the Backblaze SMART rule flags. Lower expected failures per drive is healthier.  
Source: reports/fleet_risk_sites.csv (expected_failures_30d, heuristic_flagged); Drive.p_fail_30d in the graph  
Rule: sum(p_fail_30d) over the drives of the unit  
Also called: expected failures, fleet risk, site health  

### Parts Lead Time
For a part and a supplier: part_supplier.lead_time_days, the days from order to receipt at the depot. A part's lead time with no supplier named is that of its preferred supplier.  
Source: part_supplier.lead_time_days, part_supplier.preferred  
Rule: lead_time_days of the (part, supplier) pair; default the preferred supplier  
Also called: lead time, supplier lead time  

### Repeat Dispatch
A dispatch that is either visit 2 or 3 on a case that already had a truck (same_case: dispatch.visit_number > 1), or the first visit of a case on an asset whose previous dispatched case closed within the 30 days before this visit was scheduled (cross_case). Definition from Project 1 sois.repeat_dispatch.  
Source: dispatch.visit_number, case.closed_at; truth/dispatch_truth.csv.is_repeat to check  
Rule: visit_number > 1, or first visit within 30 days of a closed dispatched case on the same asset  
Also called: repeat truck roll, repeat visit, second visit  

### Risky Pod
A pod (datacenter, vault_id, pod_id) whose drives' summed p_fail_30d on the scoring date ranks in the top 5% of pods with at least 10 scored drives. Stored as Pod.risky in the graph with the threshold that produced it.  
Source: Drive.p_fail_30d aggregated by Pod by ekg.load  
Rule: sum(p_fail_30d) per pod >= 95th percentile over pods with >= 10 scored drives  
Also called: high-risk pod, hot pod  

### Service Failure (support)
A case with failure_reported = 1, set when the case ended in resolution_code = hardware_replaced. It overlaps the telemetry failure but does not match it: some part swaps happen with no telemetry failure (proactive or mis-diagnosed), about 15% of telemetry failures never raise a case, and a telemetry failure can close as no_fault_found (MESS_RULES F1).  
Source: case.failure_reported  
Rule: case.failure_reported = 1, i.e. resolution_code = hardware_replaced  
Also called: hardware replaced, reported failure, support failure  
Related: TelemetryFailure  

### Failure (telemetry)
A drive-day with Backblaze failure = 1: the last day the drive reported before it was pulled from service. One failure row per drive. This is the label the Project 1 failure model predicts.  
Source: telemetry_daily.failure; telemetry_drive_summary.failed and failure_date  
Rule: failure = 1 on a drive-day  
Also called: drive failure, failed drive, pulled drive, telemetry failure  
Related: ServiceFailure  

### Warranty Status
For an installed asset on a date d: in_warranty when warranty_end >= d, expired when warranty_end < d, unknown when warranty_end is null. Null means unknown, not expired (MESS_RULES W1: 10% of assets).  
Source: installed_asset.warranty_end  
Rule: compare warranty_end to the date asked about; null is unknown  
Also called: out of warranty, under warranty, warranty  

## Metadata (the data about the data)

### Column
Neo4j label: `Column`  
A named, typed field of a table. A column may MEAN a glossary term, which is how the semantic layer attaches a definition to a number.  
Source: Postgres information_schema.columns  

### Data Product
Neo4j label: `DataProduct`  
A named, owned bundle of tables with a freshness SLA, served to consumers as one unit (fleet_health, parts_demand, dispatch_quality).  
Source: data_product.csv, trust/slas.yaml  

### Job
Neo4j label: `Job`  
A scheduled or on-demand program that reads tables and produces tables or report files (sois.load_telemetry, sois.generate_enterprise, sois.load_postgres, sois.failure_model, ...).  
Source: job.csv plus the real Project 1 and Project 2 entry points  

### Query
Neo4j label: `Query`  
One logged SQL statement: who ran it, when, how long, how many rows, and which tables it read. Query history tells the graph which tables matter to which roles.  
Source: query_log.csv  
Also called: query history, query log  

### Table
Neo4j label: `Table`  
A relational table or a file-backed dataset (Postgres table, Parquet directory, CSV, or a report file under reports/). Has columns, is produced by a job, and is read by queries and jobs.  
Source: Postgres information_schema, lineage.csv, Project 1 pipeline  

## Relationships

| Relationship | From | To | Meaning |
|---|---|---|---|
| `ABOUT` | Case | InstalledAsset | A case concerns an asset. |
| `BELONGS_TO` | Column | Table | A column is a field of a table. |
| `COMPOSED_OF` | DataProduct | Table | A data product bundles tables. |
| `COVERS` | Runbook | Product | A runbook applies to the products of a family. |
| `HAS` | Account | Site | An account owns a site. |
| `HOSTS` | Site | InstalledAsset | An asset is installed at a site. |
| `INSTANCE_OF` | any | Concept | Every entity node links to the Term that defines its label. |
| `IS_A` | InstalledAsset | Product | An asset is a unit of a product. |
| `MEANS` | Column | Concept | A column carries the meaning of a glossary term; this is the semantic layer's hook into the schema. |
| `OPENED_BY` | Case | AccountRecord | The CRM account record the case cites (the messy id, MESS_RULES A4); the Account is reached through SAME_AS. |
| `PERFORMS` | Technician | Dispatch | A technician performs a dispatch. |
| `PRODUCED_BY` | Table | Job | A job writes a table or report. |
| `READS` | any | Table | A query or a job reads a table. |
| `RESOLVES` | Dispatch | Case | A dispatch was sent to resolve a case (whatever its outcome). |
| `SAME_AS` | any | any | An entity-resolution link from a source record (AccountRecord, Drive) to its canonical entity (Account, Asset), with score, method and band on the edge. |
| `SUPPLIED_BY` | Part | Supplier | A supplier supplies a part; lead_time_days, unit_cost_usd and preferred live on the edge. |
| `USED` | Dispatch | Part | A dispatch consumed a part (quantity on the edge). |
| `USES` | Product | Part | A product uses a part. Derived from observed dispatches: parts consumed on cases about assets of that product, with the count on the edge. No bill of materials exists in the source. |
