# ADR 0006: Blank starter and owner-confirmed decision policy

- Status: accepted
- Date: 2026-10-19
- Supersedes: automatic active-playbook revision and suggestion adoption in ADRs 0001, 0004 and 0005; their other decisions remain in force.

## Context

The owner requested a blank trader starting point and private chat to mold its behavior, question recorded decisions and discuss suggestions. Changes to decision-making Markdown require confirmation. A conversational acknowledgment or nightly review must not silently alter the trader's strategy.

## Decision

Provide a versioned strategy-neutral identity/playbook starter with standing constraints and no inherited experience. New minds start paused with fresh lineage, empty working state and separate private account configuration. Keep Start blank, Clone experience and Recover original trader distinct. SQL remains runtime/audit authority; a Markdown template is not a second mutable live store.

Private owner chat answers from the original decision record, dated evidence and the policy then in force. It distinguishes observation, proposal, refusal and actual fill; later interpretation is labeled separately. Missing evidence is reported honestly. Questions never mutate policy.

Suggestions and nightly review propose exact changes; they cannot activate rules. Editable identity, playbook and reusable strategy guidance require explicit authenticated owner confirmation of a proposal bound to the trader, exact diff/hash and current base version. Add/trial/sharpen/retire, manual edit and rollback use this workflow. Immutable constraints and account/risk permissions remain independently protected. The controlled activation service is the only writer of active versions, committing the approval, before/after audit and monotonic version atomically. Stale, expired, rejected, replayed or conflicting requests cannot apply changes; failures leave current policy intact. In-flight decisions cannot commit against superseded policy.

Routine factual observations, journals and bounded working memory retain their audited automatic behavior. They cannot smuggle standing rules into the decision context. Pending drafts remain separate from active strategy. Source approval authority is not reusable by a clone, and recovery cannot promote pending proposals. Halt controls remain on the operational override channel; neither chat nor policy approval submits an order.

The [architecture](../architecture.md#private-owner-chat-and-policy-approval) defines the full contract and validation requirements. PIP-16 delivers the reusable starter and design. Actual private chat and SQL approval enforcement remain planned implementation, with no hosted change or paid calls in this story.

## Consequences

- The robot can propose improvements and respond to the owner while the owner controls policy activation.
- Existing deployed identity/playbook and historical records are untouched; M4 qualification continues independently.
- M5 owner controls must enforce approvals before trading-capable use; M7 review must test proposal generation and confirmed activation rather than autonomous policy mutation.
- Approval/audit records must be preserved by an explicitly supported future bundle schema. Imported policy never grants account, approval or trading authority.
