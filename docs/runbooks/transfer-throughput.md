# NAS to Spark transfer throughput

Copying a model from the NAS cache to Sparks is the slowest step of a profile
load. This runbook says what the ceiling is, what the code already does about
it, and how to find which part of the path is the limit when a copy runs far
below it. Measure first; none of the NAS settings below are applied by the
platform.

## The path and its ceiling

```
Spark agent  --mTLS, HTTP/1.1, one 64 MiB range per request-->  Caddy (NAS)
Caddy --authorization subrequest--> control-api (cert, assignment, object)
Caddy --file_server, TLS, from a read-only mount--> Spark agent
```

The Controller answers each range request only with the stored file's name.
Caddy then reads that file from the model-cache volume and writes it through
TLS, so Go cannot use `sendfile`: the bytes are read into Caddy's memory and
encrypted on the way out. That costs CPU, not a rate cap: one core encrypts
well over 1 GB/s on any current CPU.

On 2.5 GbE the payload ceiling is about 290 MB/s (2.5 Gbit/s, less Ethernet,
IP and TCP framing at MTU 1500, less TLS). **That is shared by every Spark
copying at the same time**; the Hugging Face download arrives on the same NIC
in the other direction and does not use the outbound half of a full-duplex
link. At that rate:

| Copy | Time at 285 MB/s | Time at 70 MB/s |
| --- | --- | --- |
| 159 GiB (one Spark) | about 10 minutes | about 40 minutes |
| 318 GiB (two Sparks) | about 20 minutes | about 80 minutes |

A copy that stays under about 200 MB/s total while the link is idle is not
network-bound.

## What the agent does

The Spark agent opens up to eight objects at once. A governor decides how many
of their 64 MiB range requests are in flight: it starts at four, which a lone
transfer is known to sustain, adds two streams while the agent's throughput
grows by at least 10% per 10 second window (up to eight), returns to the
previous count and holds for a minute when more streams did not help, and
halves the count after a transient Controller or network failure. The agent log
prints `model transfer streams now N` when it changes. Partial files, the
resume offset (the partial's length), the ETag and range checks and the
no-re-hash rule are unchanged; a waiting stream holds nothing but its partial.

The Controller authorizes an assignment once per 30 seconds and remembers where
an authorized object is stored for the same time, so a range request costs one
certificate check and one `lstat`; it no longer re-reads the cache manifest and
receipt per range.

More streams cannot help when the NAS is the limit. Use the measurements below
rather than raising a number.

## Measure

Run these from a root shell. Nothing in them changes the platform.

On the NAS (`scripts/measure-nas-serving`, standard tools only):

```bash
sudo scripts/measure-nas-serving static
sudo scripts/measure-nas-serving read --drop-caches
sudo scripts/measure-nas-serving sample 120
```

On a Spark that holds an assignment (the agent log names `PLAN_DIGEST` and
`OBJECT_SHA256` during a copy; the object must be at least 6 GiB). Run `iperf3 -s`
on the NAS first:

```bash
sudo scripts/measure-transfer PLAN_DIGEST OBJECT_SHA256 NAS_ADDRESS
```

Do three runs, so that the comparisons below are possible:

1. **Under the real load:** a profile load is copying and the Hugging Face
   download is running. Start `sample 120` on the NAS, and `measure-transfer`
   on a Spark that is not copying (or between loads).
2. **Ingest paused:** no Hugging Face download and no other copy. Repeat
   `read` and `measure-transfer`.
3. **Synthetic ingest:** in a second NAS terminal run
   `sudo scripts/measure-nas-serving write-load 120 120` while the `read` test
   and `measure-transfer` run. This reproduces the download's disk writes
   without the internet.

## Read the numbers

Hypotheses in the order the evidence supports them.

### 1. The ingest competes with the serving for the NAS disks

Evidence for it: the rate fell from about 270 MB/s (a lone load, 1 October) to
35 to 70 MB/s (2 and 3 October), which coincides with the Hugging Face
download running adaptively at about 120 MB/s. The suspected mechanism: parallel
ranges write into preallocated files, which on RAID5 can be partial-stripe
writes, each costing reads of old data and parity, so the ingest would take far
more than 120 MB/s of the array and queue the Sparks' reads behind it. The
measurements below confirm or reject this; it is a hypothesis, not a finding.

Decide with run 1 against run 2, and with run 3:

- `read` with 4 and 8 readers is much slower under `write-load` (or during
  ingest) than without it, or `measure-transfer` rises sharply when ingest is
  paused: **confirmed**.
- In `sample`, an `md*_raid5` thread near 100% of a core, or member disks with a
  write rate well above the md device's own write rate while members also read
  during a pure write, is the RAID5 read-modify-write. `static` shows
  `stripe_cache_size` and `group_thread_cnt`.
- `psi_io` high and `iowait` high while `nic_tx` is low: tasks wait on the disks.

What helps, in this order: finish or pause the download while a profile copies
(a Controller policy would need its own change); give the RAID5 more stripe
cache (`echo 8192 > /sys/block/mdX/md/stripe_cache_size`) and worker threads
(`echo 4 > /sys/block/mdX/md/group_thread_cnt`), which reset at reboot unless
the NAS boot scripts set them; keep the Hugging Face writes sequential.

### 2. The NAS CPU is saturated

`sample` lists the busiest processes with their container. Caddy (or
`control-api`, or Python in the worker hashing the download) at or above 100% of
one core, or `cpu%` near 100 across all threads: **confirmed**. Encryption in
Caddy at 285 MB/s is a fraction of a core on a current CPU; if it is not,
the CPU is weak or throttled and the fix is moving work (such as ingest hashing)
off the serving window, not TLS tuning.

### 3. The stored object reads slowly

`read --drop-caches` reads an object with no network at all. Compare it with
the 280 MB/s the network can take:

- One reader far below 280 MB/s: the file is the problem. `static` shows
  `compression` in the mount options, no `C` (no-copy-on-write) attribute on
  the sample object, and `compsize`/`filefrag` extents. Objects written before
  the Controller marked the directory no-copy-on-write keep their old
  attributes; a fragmented or compressed object reads slowly.
- One reader fast, four or eight slow: concurrent sequential readers lose to
  each other. Raise read-ahead on the array (`blockdev --setra`, or
  `read_ahead_kb`) and compare. The agent's governor will already have settled
  below the point where more streams hurt, so this shows as a low plateau.
- The direct-I/O line is the device itself; if it is fast, the filesystem or
  page cache is the layer to look at.

### 4. The Controller round trip per range

`measure-transfer` prints the time to first byte of one-byte ranges on a warm
connection. Tens of milliseconds are noise next to a 64 MiB range at 100 MB/s
(about 0.6 s). Several hundred milliseconds, or a first request that takes
seconds, mean `control-api` is starved (see 2) or slow to reach its database.
The range size block shows the same thing from the other side: 64 MiB ranges
should be at least as fast as 16 MiB ranges, and 1 MiB ranges much slower only
by the round trip.

### 5. The number of streams

The `parallel streams` block shows 1, 2, 4 and 8 streams of one Spark.
Throughput that keeps rising to eight streams means more parallelism helps and
the governor will find it; a flat line from one stream on means the limit is
elsewhere (1 to 3). One TCP stream normally fills 2.5 GbE.

### 6. The network

`iperf3` at one and at four streams should give about 2350 Mbit/s on 2.5 GbE.
Lower means the link negotiated a lower speed (`static` prints `Speed`), the
switch or cable is the limit, or another host shares the uplink. Retransmits
during the tests (printed by both scripts) mean loss. A copy that matches
`iperf3` is network-bound and nothing in the NAS or the agent will raise it.

### 7. Caddy and TLS specifics, and the Spark

Go cannot `sendfile` through TLS and HTTP/2 is available but not used (the
agent uses HTTP/1.1); `measure-transfer` prints one HTTP/2 stream for
comparison. The Spark side sends the bytes to `/dev/null` in the script, so a
fast script and a slow agent copy points at the Spark's disk writes, which are
preallocated and flushed behind the transfer (see the model-cache runbook).

## Expected outcome

- Ingest paused, both Sparks copying: about 280 to 290 MB/s total, split
  between the Sparks. This is the network ceiling.
- Ingest running: whatever the arrays leave after about 120 MB/s of ingest
  writes. If runs 1 and 2 differ strongly, that difference is the cost of
  serving during ingest, and it, not the agent or Caddy, is what to reduce.
