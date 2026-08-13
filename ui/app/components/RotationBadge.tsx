import type { PortunusReference } from "../types";

// Renders only when an agent's `portunus ask "rotate ..."` request flagged
// this reference (Registry.retag()'s tags.rotation_requested marker,
// portunus-agent-ops-federation). Self-clearing: the existing Rotate ->
// re-drop flow replaces the whole tags dict, so this derives straight from
// the fetched reference on every registry refresh -- no separate state to
// go stale.
export default function RotationBadge({
  reference,
  prominent = false,
}: {
  reference: PortunusReference;
  prominent?: boolean;
}) {
  if (reference.tags?.rotation_requested !== "true") return null;
  return (
    <span className={prominent ? "rotation-badge prominent" : "rotation-badge"}>
      ⟳ rotation requested
    </span>
  );
}
