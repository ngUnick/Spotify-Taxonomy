# Supervised Codex batch contract

The operator manually provides one validated outbound batch to Codex and manually receives a structured result.
Use only the supplied batch, its semantic specification IDs, evidence, characteristics, and proposals. Return
only the strict result object below, with exact values copied from the batch:

```json
{
  "result_schema_version": "codex-result.v1",
  "batch_id": "<exact batch_id>",
  "outbound_batch_digest": "<exact outbound digest>",
  "taxonomy_version": "<exact taxonomy version>",
  "specification_bundle_digest": "<exact bundle digest>",
  "items": [{
    "item_id": "<exact outbound item_id>",
    "classifications": [{
      "semantic_specification_id": "<semantic ID supplied in specifications>",
      "decision": "include | exclude | abstain",
      "justification_summary": "<concise evidence-based fact, maximum 280 characters>",
      "evidence_ids": ["<exported evidence ID>"],
      "characteristic_ids": ["<exported characteristic ID>"]
    }]
  }]
}
```

Do not invent evidence or references, include chain-of-thought/reasoning, user decisions/overrides, Spotify,
account, playlist, desired-state, synchronization, taxonomy-edit, or output-target fields. Do not perform
Spotify operations or modify specifications. The application never invokes Codex or an OpenAI API.
