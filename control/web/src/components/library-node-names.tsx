import {createContext, useCallback, useContext} from "react";
import type {ReactNode} from "react";

type NodeDisplayNames = Readonly<Record<string, string>>;

const NodeDisplayNamesContext = createContext<NodeDisplayNames>({});

export function LibraryNodeNamesProvider({children, names}: {children: ReactNode; names: NodeDisplayNames}) {
  return <NodeDisplayNamesContext.Provider value={names}>{children}</NodeDisplayNamesContext.Provider>;
}

export function useLibraryNodeName(): (nodeId: string) => string {
  const names = useContext(NodeDisplayNamesContext);
  return useCallback((nodeId: string) => {
    const displayName = names[nodeId]?.trim();
    return displayName && displayName !== nodeId ? displayName : "Spark node";
  }, [names]);
}
