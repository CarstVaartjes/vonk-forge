export function spark(name: string, memoryFree: number, diskFree: number, online = true) {
  return {
    id: `spk_${name.padEnd(32, "0")}`,
    display_name: name,
    hostname: `${name.toLowerCase()}.local`,
    labels: {},
    connection: {
      online_state: online ? "online" : "offline",
      offline_reason: online ? null : "stale",
      last_seen_at: null,
    },
    inventory: {
      disk_free_bytes: diskFree,
      disk_total_bytes: 1000,
      host_memory_free_bytes: memoryFree,
      host_memory_total_bytes: 100,
    },
    telemetry: null,
    installed: [],
    loaded: [],
    warnings: [],
  };
}
