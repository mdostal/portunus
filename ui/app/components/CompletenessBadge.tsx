import type { PortunusReference } from "../types";
import { checkMetadataCompleteness } from "../completeness";

// Renders only when metadata is genuinely incomplete -- derived fresh from
// the reference on every render, same self-clearing precedent RotationBadge
// already established (no separate state to go stale; fill in the field,
// the badge disappears on the next fetch, nothing to reset by hand).
export default function CompletenessBadge({
  reference,
  prominent = false,
}: {
  reference: PortunusReference;
  prominent?: boolean;
}) {
  const completeness = checkMetadataCompleteness(reference);
  if (completeness.isComplete) return null;

  const missing: string[] = [];
  if (completeness.missingDescription) missing.push("description");
  if (completeness.missingPurpose) missing.push("purpose");
  if (completeness.missingProjectTags) missing.push("org/project/tags");

  return (
    <span
      className={prominent ? "completeness-badge prominent" : "completeness-badge"}
      title={`missing: ${missing.join(", ")}`}
    >
      ⚠ missing metadata
    </span>
  );
}
