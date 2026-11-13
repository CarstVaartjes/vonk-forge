import { sparkPath, sparkRouteId } from "./spark-route";

test("a Spark path round-trips its id and other paths name no Spark", () => {
  expect(sparkPath("spk_1/a")).toBe("/sparks/spk_1%2Fa");
  expect(sparkRouteId("/sparks/spk_1%2Fa")).toBe("spk_1/a");
  expect(sparkRouteId("/sparks")).toBeNull();
  expect(sparkRouteId("/sparks/a/b")).toBeNull();
  expect(sparkRouteId("/sparks/%E0%A4%A")).toBeNull();
});
