/**
 * Says on the root element how the last input arrived (`data-input="keyboard" | "pointer"`), so a
 * programmatic focus target (a page heading) draws its ring only for a keyboard user, never after a tap or click.
 */
export function trackInputModality(root: HTMLElement = document.documentElement): () => void {
  const set = (value: "keyboard" | "pointer") => () => {
    root.dataset.input = value;
  };
  const keyboard = set("keyboard");
  const pointer = set("pointer");
  document.addEventListener("keydown", keyboard, true);
  document.addEventListener("pointerdown", pointer, true);
  return () => {
    document.removeEventListener("keydown", keyboard, true);
    document.removeEventListener("pointerdown", pointer, true);
  };
}
