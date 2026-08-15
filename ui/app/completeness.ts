import type { PortunusReference } from "./types";

// Pure derivation over fields already fetched with every PortunusReference --
// no new API call, no stored field, nothing to drift out of sync (design-
// discussion.md's own anti-second-source-of-truth reasoning, portunus-vault-
// trust-and-access Slice 2). "Extra metadata" = description/purpose
// (informational); "project tags" = org/project/tags (organizational) --
// directly the two things the user named ("a warning if we don't have extra
// metadata and some project tags").
export interface MetadataCompleteness {
  missingDescription: boolean;
  missingPurpose: boolean;
  missingProjectTags: boolean;
  isComplete: boolean;
}

export function checkMetadataCompleteness(ref: PortunusReference): MetadataCompleteness {
  const missingDescription = !ref.description;
  const missingPurpose = !ref.purpose;
  const hasAnyProjectTag = Boolean(ref.org || ref.project || Object.keys(ref.tags || {}).length > 0);
  const missingProjectTags = !hasAnyProjectTag;
  return {
    missingDescription,
    missingPurpose,
    missingProjectTags,
    isComplete: !missingDescription && !missingPurpose && !missingProjectTags,
  };
}
