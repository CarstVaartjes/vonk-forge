import type { MouseEvent } from "react";
import { interceptPlainClick } from "./plain-click";

function click(init: Partial<MouseEvent<HTMLAnchorElement>> = {}) {
  let prevented = false;
  const event = {
    defaultPrevented: false,
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    preventDefault: () => {
      prevented = true;
    },
    ...init,
  } as MouseEvent<HTMLAnchorElement>;
  return { event, prevented: () => prevented };
}

test("a plain primary click is intercepted", () => {
  const { event, prevented } = click();
  expect(interceptPlainClick(event)).toBe(true);
  expect(prevented()).toBe(true);
});

test.each([
  { metaKey: true },
  { ctrlKey: true },
  { shiftKey: true },
  { altKey: true },
  { button: 1 },
  { defaultPrevented: true },
])("a click with %o keeps the browser default", (init) => {
  const { event, prevented } = click(init);
  expect(interceptPlainClick(event)).toBe(false);
  expect(prevented()).toBe(false);
});
