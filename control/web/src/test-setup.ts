import "@testing-library/jest-dom/vitest";

class MemoryStorage implements Storage {
  readonly #items = new Map<string, string>();

  get length(): number {
    return this.#items.size;
  }

  clear(): void {
    this.#items.clear();
  }

  getItem(key: string): string | null {
    return this.#items.get(String(key)) ?? null;
  }

  key(index: number): string | null {
    return Array.from(this.#items.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.#items.delete(String(key));
  }

  setItem(key: string, value: string): void {
    this.#items.set(String(key), String(value));
  }
}

const localStorageDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
const jsdomProvidesLocalStorage = localStorageDescriptor
  ? "value" in localStorageDescriptor
    ? localStorageDescriptor.value !== undefined
    : localStorageDescriptor.enumerable
  : false;

if (!jsdomProvidesLocalStorage) {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: new MemoryStorage(),
  });
}
