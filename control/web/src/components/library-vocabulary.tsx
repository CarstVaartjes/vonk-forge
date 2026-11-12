import { createContext, useContext, useMemo } from "react";
import type { ReactNode } from "react";
import type { LibraryVocabulary } from "../api/types";

/** The word shown where a Model or Recipe declares no capabilities. */
export const NOT_REPORTED = "Not reported";

const VocabularyContext = createContext<LibraryVocabulary | null>(null);

/** The words of the synced release: the Controller serves them, nothing here spells one. */
export function LibraryVocabularyProvider({
  children,
  vocabulary,
}: {
  children: ReactNode;
  vocabulary: LibraryVocabulary | null;
}) {
  return <VocabularyContext.Provider value={vocabulary}>{children}</VocabularyContext.Provider>;
}

/** A capability the release does not publish (a newer one) is shown by its name. */
export function useCapabilityWords() {
  const vocabulary = useContext(VocabularyContext);
  return useMemo(() => {
    const categories = new Map(vocabulary?.categories.map((item) => [item.id, item.label]));
    const capabilities = new Map(vocabulary?.capabilities.map((item) => [item.name, item]));
    return {
      label: (name: string) => capabilities.get(name)?.label ?? name,
      /** "Category: what it does", or undefined for a name the release does not publish. */
      title: (name: string) => {
        const item = capabilities.get(name);
        return item && `${categories.get(item.category) ?? item.category}: ${item.description}`;
      },
    };
  }, [vocabulary]);
}

/** What the release says a qualification status means. */
export function useStatusMeaning(): (status: string) => string | undefined {
  const vocabulary = useContext(VocabularyContext);
  return useMemo(() => {
    const meanings = new Map(vocabulary?.statuses.map((item) => [item.status, item.meaning]));
    return (status: string) => meanings.get(status);
  }, [vocabulary]);
}
