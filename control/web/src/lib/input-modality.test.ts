import { trackInputModality } from "./input-modality";

test("the root says keyboard after a key and pointer after a press, and stops when cleaned up", () => {
  const stop = trackInputModality();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab" }));
  expect(document.documentElement.dataset.input).toBe("keyboard");
  document.dispatchEvent(new Event("pointerdown"));
  expect(document.documentElement.dataset.input).toBe("pointer");
  stop();
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab" }));
  expect(document.documentElement.dataset.input).toBe("pointer");
});
