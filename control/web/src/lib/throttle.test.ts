import { throttleReasons } from "./throttle";

test("only a thermal or hardware slowdown is throttling; idle and a power cap are the clock working as designed", () => {
  expect(throttleReasons(1).mood).toBe("clear");
  expect(throttleReasons(4).mood).toBe("clear");
  expect(throttleReasons(32).mood).toBe("throttled");
  expect(throttleReasons(512).mood).toBe("unrecognised");
});
