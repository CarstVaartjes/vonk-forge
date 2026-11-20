import {
  CacheAssetDisposition,
  FleetRemovalDisposition,
  AssetAvailability,
} from "../api/vocabulary.generated";
import type { CacheRemovalReview } from "../api/types";

const DISPOSITION_TEXT: Record<CacheRemovalReview["assets"][number]["disposition"], string> = {
  [CacheAssetDisposition.REMOVE]: "Remove",
  [CacheAssetDisposition.RETAIN_SHARED]: "Retain shared",
  [CacheAssetDisposition.RETAIN_REFERENCED]: "Retain referenced",
  [CacheAssetDisposition.RETAIN_OBSERVATION_UNAVAILABLE]: "Retain (observation unavailable)",
};

/** A value this build does not know shows as sent, never as a wrong reason. */
function dispositionText(disposition: string): string {
  return (DISPOSITION_TEXT as Record<string, string | undefined>)[disposition] ?? disposition;
}

/** Render the Controller decision without deriving a second removal policy. */
export function CacheRemovalReviewDetails({ review }: { review: CacheRemovalReview }) {
  return (
    <div aria-label="Cache removal review">
      {review.fleet && (
        <section aria-label="Fleet removal effects">
          <h3>Controller cache</h3>
          <p>
            {review.fleet.nas_remove ? "Remove unused model files" : "Retain model files"}:{" "}
            {review.fleet.nas_detail}
          </p>
          {review.fleet.targets.map((target) => (
            <section key={target.node_id}>
              <h3>Spark {target.node_id}</h3>
              <p>
                {target.observation_available
                  ? `${target.reclaimable_bytes} bytes can be freed from the model store`
                  : target.installations.length === 0
                    ? "No complete model-store observation; this Spark has no installation of the model, so it is not checked and its copies age out through store retention"
                    : "Waiting for a complete model-store observation"}
              </p>
              {target.installations.some(
                (item) => item.disposition === FleetRemovalDisposition.REMOVE,
              ) && <p>Additional installation cleanup bytes: unknown; files may share links.</p>}
              <ul>
                {target.installations.map((item) => (
                  <li key={item.installation_id}>
                    {item.disposition}: installation {item.installation_id} — {item.detail}
                  </li>
                ))}
                {target.objects.map((item) => (
                  <li key={item.object.sha256}>
                    {item.disposition}: {item.object.sha256} ({item.object.bytes} bytes) —{" "}
                    {item.detail}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </section>
      )}
      {review.fleet && (
        <p>
          Controller bytes that can be freed:{" "}
          {review.nas_reclaimable_bytes === null || review.nas_reclaimable_bytes === undefined
            ? AssetAvailability.UNKNOWN
            : String(review.nas_reclaimable_bytes)}
        </p>
      )}
      <p>
        Remove cache for {review.selector} ({review.target_identity})
      </p>
      {review.with_model !== null && review.with_model !== undefined && (
        <p>{review.with_model ? "Include model cache" : "Keep model cache"}</p>
      )}
      {review.assets.length === 0 ? (
        <p>No cached assets selected.</p>
      ) : (
        <ul>
          {review.assets.map((asset) => (
            <li key={`${asset.kind}:${asset.sha256}`}>
              {dispositionText(asset.disposition)}: {asset.kind} {asset.sha256} (expected:{" "}
              {asset.expected_bytes === null || asset.expected_bytes === undefined
                ? AssetAvailability.UNKNOWN
                : `${asset.expected_bytes} bytes`}
              ; available:{" "}
              {asset.available_bytes === null || asset.available_bytes === undefined
                ? AssetAvailability.UNKNOWN
                : `${asset.available_bytes} bytes`}
              ; {asset.availability})
            </li>
          ))}
        </ul>
      )}
      {review.references.map((reference, index) => (
        <p key={`reference:${index}`}>
          Reference: {reference.owner_kind} {reference.owner_id} ({reference.state}) for{" "}
          {reference.asset_kind} {reference.asset_sha256} — {reference.detail}
        </p>
      ))}
      {review.active_work.map((work, index) => (
        <p key={`work:${index}`}>
          Active work: {work.owner_kind} {work.owner_id} ({work.state}) for {work.asset_kind}{" "}
          {work.asset_sha256} — {work.detail}
        </p>
      ))}
      {review.blockers.map((blocker, index) => (
        <p role="alert" key={`blocker:${index}`}>
          {blocker.code}: {blocker.detail}
          {(blocker.recovery_actions?.length ?? 0) > 0 && (
            <> — Next action: {blocker.recovery_actions?.join(", ")}</>
          )}
        </p>
      ))}
    </div>
  );
}
