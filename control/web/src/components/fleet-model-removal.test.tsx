import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import {
  LifecycleState,
  FleetRemovalDisposition,
  FleetRemovalReason,
} from "../api/vocabulary.generated";
import type { ControlApi } from "../api/types";
import { cacheRemovalReview } from "../test-fixtures/cache-removal";
import { LibraryCacheAction } from "./library-cache-action";

test.each([LifecycleState.QUEUED, LifecycleState.SUCCEEDED])(
  "fleet removal binds its review and preserves the receipt (%s)",
  async (state) => {
    const review = cacheRemovalReview({
      resource_kind: "model",
      selector: "example/model",
      target_identity: "e".repeat(64),
      with_model: null,
      nas_reclaimable_bytes: 0,
      fleet: {
        nas_remove: false,
        nas_reason: FleetRemovalReason.REFERENCED,
        nas_detail: "Saved profile 20 retains this model",
        targets: [
          {
            node_id: "spk_1",
            observation_available: true,
            reclaimable_bytes: 42,
            installations: [],
            objects: [
              {
                object: { sha256: "a".repeat(64), bytes: 42 },
                disposition: FleetRemovalDisposition.REMOVE,
                reason: FleetRemovalReason.UNUSED,
                detail: "Unused model object",
              },
            ],
          },
        ],
      },
    });
    const modelRemovalReview = vi.fn().mockResolvedValue(review);
    const removeModelCache = vi.fn().mockImplementation(async (selector, request_key) => ({
      action: review.action,
      selector,
      request_key,
      operation_id: "fleet-removal",
      review_digest: review.review_digest,
      model_content_sha256: review.target_identity,
      state,
      fleet: {
        effects: review.fleet,
        deadline_at: "2026-10-20T12:00:00Z",
        completed_installations: [],
        confirmed_nodes: [],
      },
    }));
    const api = { modelRemovalReview, removeModelCache } as unknown as ControlApi;
    render(
      <LibraryCacheAction
        fleetOnly
        api={api}
        selector={review.selector}
        modelContentSha256={review.target_identity}
        state="not_cached"
        onPrepared={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove model from fleet" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Fleet removal effects")).toHaveTextContent(
        "Saved profile 20 retains this model",
      ),
    );
    expect(screen.getByLabelText("Fleet removal effects")).toHaveTextContent("42 bytes");
    expect(removeModelCache).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm remove" }));
    await waitFor(() =>
      expect(removeModelCache).toHaveBeenCalledWith(
        review.selector,
        expect.any(String),
        expect.anything(),
        review.review_digest,
      ),
    );
    expect(modelRemovalReview).toHaveBeenCalledWith(review.selector, expect.anything(), true);
    expect(await screen.findByLabelText("Cache removal progress")).toHaveTextContent(
      "fleet-removal",
    );
  },
);
