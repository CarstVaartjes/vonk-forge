import type {CacheRemovalReview} from "../api/types";

/** Render the Controller decision without deriving a second removal policy. */
export function CacheRemovalReviewDetails({review}: {review: CacheRemovalReview}) {
  return <div aria-label="Cache removal review">
    <p>Remove cache for {review.selector} ({review.target_identity})</p>
    {review.with_model !== null && review.with_model !== undefined && <p>{review.with_model ? "Include model cache" : "Keep model cache"}</p>}
    {review.assets.length === 0 ? <p>No cached assets selected.</p> : <ul>
      {review.assets.map(asset => <li key={`${asset.kind}:${asset.sha256}`}>
        {asset.disposition === "remove" ? "Remove" : "Retain shared"}: {asset.kind} {asset.sha256} (expected: {asset.expected_bytes === null || asset.expected_bytes === undefined ? "unknown" : `${asset.expected_bytes} bytes`}; available: {asset.available_bytes === null || asset.available_bytes === undefined ? "unknown" : `${asset.available_bytes} bytes`}; {asset.availability})
      </li>)}
    </ul>}
    {review.references.map((reference, index) => <p key={`reference:${index}`}>
      Reference: {reference.owner_kind} {reference.owner_id} ({reference.state}) for {reference.asset_kind} {reference.asset_sha256} — {reference.detail}
    </p>)}
    {review.active_work.map((work, index) => <p key={`work:${index}`}>
      Active work: {work.owner_kind} {work.owner_id} ({work.state}) for {work.asset_kind} {work.asset_sha256} — {work.detail}
    </p>)}
    {review.blockers.map((blocker, index) => <p role="alert" key={`blocker:${index}`}>{blocker.code}: {blocker.detail}{(blocker.recovery_actions?.length ?? 0) > 0 && <> — Next action: {blocker.recovery_actions?.join(", ")}</>}</p>)}
  </div>;
}
