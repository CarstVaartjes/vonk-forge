#!/bin/sh
# The acceptance device answers the same queries as a healthy GB10 driver.
set -eu
case "$*" in
    -q)
        printf '%s\n' 'GPU 0000000F:01:00.0' \
            '    Product Name : NVIDIA GB10' \
            '    Performance State : P8' \
            '    GPU Recovery Action : None'
        ;;
    '--query-gpu=name,memory.total,memory.free,driver_version --format=csv,noheader,nounits')
        printf '%s\n' 'NVIDIA GB10, [N/A], [N/A], 590.44'
        ;;
    '--query-gpu=compute_cap,driver_version --format=csv,noheader,nounits')
        printf '%s\n' '12.1, 590.44'
        ;;
    '--query-gpu=name,utilization.gpu,memory.total,memory.free,temperature.gpu --format=csv,noheader,nounits')
        printf '%s\n' 'NVIDIA GB10, 0, [N/A], [N/A], 40'
        ;;
    *)
        printf '%s\n' "Unsupported synthetic NVIDIA query: $*" >&2
        exit 1
        ;;
esac
