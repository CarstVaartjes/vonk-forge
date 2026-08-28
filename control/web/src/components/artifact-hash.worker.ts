import {IncrementalSha256} from "./artifact-sha256";

type HashRequest = {id: string; blob: Blob; chunkSize: number};
type WorkerScope = {onmessage: ((event: MessageEvent<HashRequest>) => void) | null; postMessage(value: unknown): void};
const scope = self as unknown as WorkerScope;

scope.onmessage = event => {
  const {blob, chunkSize, id} = event.data;
  void (async () => {
    try {
      const hash = new IncrementalSha256();
      for (let offset = 0; offset < blob.size; offset += chunkSize) {
        const end = Math.min(offset + chunkSize, blob.size);
        hash.update(new Uint8Array(await blob.slice(offset, end).arrayBuffer()));
        scope.postMessage({id, kind: "progress", loaded: end, total: blob.size});
      }
      scope.postMessage({id, kind: "result", sha256: hash.hexDigest()});
    } catch (error) {
      scope.postMessage({id, kind: "error", message: error instanceof Error ? error.message : "Unable to hash artifact input"});
    }
  })();
};
