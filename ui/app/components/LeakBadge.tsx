import type { LeakSummary } from "../types";

// Independent from RotationBadge -- driven by leak-status data (fetched
// once per page load, passed down as a ref_name -> LeakSummary map), never
// tags.rotation_requested. Reusing that tag would mean leak-scan starting
// to call retag() on every new finding, a real new write path this
// feature was never designed for, and it would lose the "why" (an
// agent-requested rotation and a leak-detected rotation are different
// facts a human should be able to tell apart) -- design-discussion.md §1.
//
// Native title="..." tooltip -- the ONLY tooltip mechanism this codebase
// uses anywhere (RotationBadge/CompletenessBadge do the same); no new
// hover component. "leaked in N conversations" counts distinct FILES, not
// raw finding count (design-discussion.md §3) -- the same secret can
// match many lines of one transcript without that meaning many separate
// leaks.
export default function LeakBadge({
  summary,
  prominent = false,
}: {
  summary?: LeakSummary | null;
  prominent?: boolean;
}) {
  if (!summary || !summary.severity) return null;

  const files = summary.distinct_files ?? summary.finding_count;
  const tooltip = `${summary.severity} severity -- leaked in ${files} conversation${files === 1 ? "" : "s"}`;

  return (
    <span
      className={
        prominent
          ? `leak-badge leak-badge-${summary.severity} prominent`
          : `leak-badge leak-badge-${summary.severity}`
      }
      title={tooltip}
    >
      ⚠ leak: {summary.severity}
    </span>
  );
}
