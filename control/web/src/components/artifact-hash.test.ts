import {hashArtifactBlob} from "./artifact-hash";
import type {HashWorker, HashWorkerFactory} from "./artifact-hash";
import {IncrementalSha256} from "./artifact-sha256";

function workerFactory(chunks: number[], delay = 0): {factory: HashWorkerFactory; workers: Array<{terminated: boolean}>} {
  const workers: Array<{terminated: boolean}> = [];
  return {
    workers,
    factory: () => {
      const state = {terminated: false};
      workers.push(state);
      const worker: HashWorker = {
        onmessage: null,
        onerror: null,
        terminate() { state.terminated = true; },
        postMessage(value) {
          const request = value as {id: string; blob: Blob; chunkSize: number};
          void (async () => {
            const hash = new IncrementalSha256();
            for (let offset = 0; offset < request.blob.size && !state.terminated; offset += request.chunkSize) {
              if (delay) await new Promise(resolve => setTimeout(resolve, delay));
              if (state.terminated) return;
              const part = request.blob.slice(offset, Math.min(offset + request.chunkSize, request.blob.size));
              chunks.push(part.size);
              hash.update(new Uint8Array(await part.arrayBuffer()));
              worker.onmessage?.(new MessageEvent("message", {data: {id: request.id, kind: "progress", loaded: Math.min(offset + request.chunkSize, request.blob.size), total: request.blob.size}}));
            }
            if (!state.terminated) worker.onmessage?.(new MessageEvent("message", {data: {id: request.id, kind: "result", sha256: hash.hexDigest()}}));
          })().catch(error => worker.onerror?.(new ErrorEvent("error", {message: String(error)})));
        },
      };
      return worker;
    },
  };
}

test("incremental SHA-256 matches the standard vector", () => {
  const hash = new IncrementalSha256();
  hash.update(new TextEncoder().encode("a"));
  hash.update(new TextEncoder().encode("bc"));
  expect(hash.hexDigest()).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
});
test("hashes a synthetic large blob through bounded worker chunks", async () => {
  const chunks: number[] = [];
  const harness = workerFactory(chunks);
  const blob = new Blob(Array.from({length: 18}, (_, index) => new Uint8Array(1024 * 1024).fill(index)));
  const progress: number[] = [];
  const digest = await hashArtifactBlob(blob, {
    chunkSize: 1024 * 1024,
    onProgress: value => progress.push(value.loaded),
    signal: new AbortController().signal,
    workerFactory: harness.factory,
  });

  expect(digest).toMatch(/^[0-9a-f]{64}$/);
  expect(chunks).toHaveLength(18);
  expect(Math.max(...chunks)).toBe(1024 * 1024);
  expect(progress.at(-1)).toBe(blob.size);
  expect(harness.workers[0].terminated).toBe(true);
});

test("terminates hashing and releases the large blob when the operator cancels", async () => {
  const chunks: number[] = [];
  const harness = workerFactory(chunks, 2);
  const controller = new AbortController();
  const blob = new Blob(Array.from({length: 20}, () => new Uint8Array(1024 * 1024)));
  const pending = hashArtifactBlob(blob, {
    chunkSize: 1024 * 1024,
    signal: controller.signal,
    workerFactory: harness.factory,
    onProgress: () => controller.abort(),
  });

  await expect(pending).rejects.toMatchObject({name: "AbortError"});
  expect(chunks.length).toBeLessThan(20);
  expect(harness.workers[0].terminated).toBe(true);
});
