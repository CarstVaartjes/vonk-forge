# Display reserved memory on a Spark

The README's [Add a Spark](../../README.md#3-add-a-spark) step recommends a
fixed 0.5 GB display reservation on a headless Spark. This is how Vonk
measures it.

Vonk's root helper reads `/proc/iomem` during each inventory (unprivileged reads
hide the addresses). The measured reserved block ending at `0x3237fffff` includes
`0x23800000` bytes of other firmware memory: the measured `0xA3800000` and
`0x43800000` blocks therefore identify 2 GB and 0.5 GB of display memory.
Anything else remains **unknown**, including masked addresses and MemTotal alone.
Fleet advice is non-blocking and appears only above 0.5 GB on a headless Spark;
Vonk never changes firmware or UEFI. A minimum reservation does not distinguish
fixed 0.5 GB from Auto booted headless.
