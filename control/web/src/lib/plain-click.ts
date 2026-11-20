import type { MouseEvent } from "react";

/**
 * True, after preventing the browser's default, only for a plain primary click
 * that nothing else handled. A modified click (new tab, new window, download)
 * and a middle click keep the browser's own link behaviour.
 */
export function interceptPlainClick(event: MouseEvent<HTMLAnchorElement>): boolean {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  )
    return false;
  event.preventDefault();
  return true;
}
