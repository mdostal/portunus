import type { ApprovalStatus, PortunusReference } from "../types";
import { describeApproval } from "../actions";

// Row-level gate marker (GH #257): whether a reference needs a human
// approval to resolve, and where that approval stands.
export default function GateBadge({
  reference,
  status,
}: {
  reference: PortunusReference;
  status?: ApprovalStatus;
}) {
  if (reference.approval !== "required") return null;
  if (status?.pending) {
    return (
      <span className="gate-badge gate-pending" title="an access is waiting on your approval">
        ⏳ approval pending
      </span>
    );
  }
  if (status?.approved) {
    return (
      <span className="gate-badge gate-approved" title={describeApproval(status)}>
        🔓 approved
      </span>
    );
  }
  return (
    <span className="gate-badge" title="gated: needs a human approval before it resolves">
      🔒 gated
    </span>
  );
}
