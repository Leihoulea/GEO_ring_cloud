# Geo Ring Cloud Presentation Tools

This directory owns non-stage presentation generation for the Geo Ring Cloud
project.

- `geo_ring_cloud_epic_group_meeting.ps1`: English EPIC group-meeting deck.
- `geo_ring_cloud_epic_group_meeting_cn.ps1`: Chinese EPIC group-meeting deck.
- `geo_ring_cloud_epic_group_meeting_slides_cn.json`: Chinese slide specification.
- `geo_ring_cloud_presentation_group_meeting_builder.mjs`: Artifact-tool builder for
  the Chinese 30-minute GEO-ring Cloud progress deck. It is a cross-stage
  component related to `stage_00d`, `stage_09d`, `stage_09e`, `stage_09f`, and
  `stage_10`; its run output is accompanied by a manifest, evidence ledger,
  terminology ledger, asset manifest, QA report, and speaker script.
- `geo_ring_cloud_presentation_manifest.ps1`: shared component-lineage writer.

The generators have `component_role=presentation_builder` and relate to the
their declared canonical stages without inventing another canonical stage.
Historical scripts at the code root remain thin executable wrappers.
