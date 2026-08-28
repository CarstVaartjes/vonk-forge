export type ArtifactHashProgress = {loaded: number; total: number};
type WorkerMessage = {id: string; kind: "progress"; loaded: number; total: number} | {id: string; kind: "result"; sha256: string} | {id: string; kind: "error"; message: string};
export type HashWorker = {
  onmessage: ((event: MessageEvent<WorkerMessage>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
  postMessage(value: unknown): void;
  terminate(): void;
};
export type HashWorkerFactory = () => HashWorker;

const defaultWorkerFactory: HashWorkerFactory = () => new Worker(new URL("./artifact-hash.worker.ts", import.meta.url), {type: "module"}) as unknown as HashWorker;

export function hashArtifactBlob(blob: Blob, options: {signal: AbortSignal; onProgress?(progress: ArtifactHashProgress): void; workerFactory?: HashWorkerFactory; chunkSize?: number}): Promise<string> {
  if (options.signal.aborted) return Promise.reject(new DOMException("Artifact hashing cancelled", "AbortError"));
  const worker = (options.workerFactory ?? defaultWorkerFactory)();
  const id = crypto.randomUUID();
  return new Promise((resolve, reject) => {
    const finish = () => {
      options.signal.removeEventListener("abort", abort);
      worker.terminate();
    };
    const abort = () => {
      finish();
      reject(new DOMException("Artifact hashing cancelled", "AbortError"));
    };
    options.signal.addEventListener("abort", abort, {once: true});
    worker.onerror = event => {
      finish();
      reject(new Error(event.message || "Artifact hashing worker failed"));
    };
    worker.onmessage = event => {
      const message = event.data;
      if (message.id !== id) return;
      if (message.kind === "progress") { options.onProgress?.({loaded: message.loaded, total: message.total}); return; }
      finish();
      message.kind === "result" ? resolve(message.sha256) : reject(new Error(message.message));
    };
    worker.postMessage({id, blob, chunkSize: options.chunkSize ?? 4 * 1024 * 1024});
  });
}
