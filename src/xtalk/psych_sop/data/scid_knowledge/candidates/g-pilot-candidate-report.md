# G Pilot Candidate Generation Report

## Status

- Candidate bundle: `scid5-zh-g-pilot-candidates`
- Status: `candidate_only_not_published`
- Source SHA-256: `681d666c27eec13923e06b8f12317c2c27736c96ef0f0651d45bb65dc5cf799e`
- This report is a review handoff, not a released or runtime-executable bundle.

## Summary

- Candidate nodes: 11
- Candidate transitions: 25
- Natural-language expression candidates: 6
- Source confidence: {"engineering_only": 4, "high": 4, "medium": 3}
- Candidate-specific review items: 18

## Candidate review items

| Candidate node | Kind | Why it needs review |
| --- | --- | --- |
| `S9` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `S9.PROBE.OCCURRENCE` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `S9.PROBE.OCCURRENCE` | `derived_clarification` | The clarification prompt is derived content and must retain non-leading, time-window-faithful wording. |
| `G3` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G3` | `related_form_anchor` | The source region is linked through a related form anchor, not an exact AcroForm field; preserve this distinction. |
| `G6` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G7` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G.PILOT.OBSESSION_SUMMARY` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G.PILOT.OBSESSION_SUMMARY` | `engineering_scaffold` | This is an engineering control node, not source clinical content; verify it remains non-diagnostic and non-user-visible where required. |
| `G.PILOT.OBSESSION_RETURN` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G.PILOT.OBSESSION_RETURN` | `engineering_scaffold` | This is an engineering control node, not source clinical content; verify it remains non-diagnostic and non-user-visible where required. |
| `S12` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G11` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G11` | `related_form_anchor` | The source region is linked through a related form anchor, not an exact AcroForm field; preserve this distinction. |
| `G.PILOT.COMPULSION_SUMMARY` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G.PILOT.COMPULSION_SUMMARY` | `engineering_scaffold` | This is an engineering control node, not source clinical content; verify it remains non-diagnostic and non-user-visible where required. |
| `G.PILOT.COMPULSION_RETURN` | `candidate_content_review` | Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release. |
| `G.PILOT.COMPULSION_RETURN` | `engineering_scaffold` | This is an engineering control node, not source clinical content; verify it remains non-diagnostic and non-user-visible where required. |

## Required review decision

For every candidate, confirm source evidence, semantic intent, evidence slots, score requirements, transition conditions, and any natural-language expression. Approval of a candidate does not publish it; release remains a later compiler and governance decision.
