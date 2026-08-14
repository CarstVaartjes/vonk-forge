#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "test_docker_firewall.sh must run as root" >&2
    exit 77
fi

repository=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
helper=${1:-$repository/packaging/bin/vonk-forge-docker-firewall}
[ -x "$helper" ] || { echo "firewall helper is unavailable" >&2; exit 1; }
temporary=$(mktemp -d /tmp/vonk-firewall-test.XXXXXX)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
config=$temporary/docker-firewall.conf
install -o root -g root -m 0600 /dev/null "$config"
printf '%s\n' \
    'VONK_NAS_MANAGEMENT_IP=192.168.1.231' \
    'VONK_NODE_MANAGEMENT_IP=192.168.1.211' \
    'VONK_NODE_FABRIC_IP=192.168.100.10' \
    'VONK_PEER_FABRIC_IP=192.168.100.11' \
    'VONK_ENDPOINT_HOST_PORTS=8000,8101' \
    'VONK_HOST_ENDPOINT_PORTS=8888' \
    'VONK_RENDEZVOUS_PORT=29500' > "$config"

unshare --net -- /bin/sh -seu -- "$helper" "$config" <<'EOF'
helper=$1
config=$2
iptables=/usr/sbin/iptables

$iptables -N DOCKER-USER
if $helper --config "$config" apply >/dev/null 2>&1; then
    echo "policy accepted node addresses absent from the host" >&2
    exit 1
fi
/usr/sbin/ip link add vonk-mgmt type dummy
/usr/sbin/ip link add vonk-fabric type dummy
/usr/sbin/ip address add 192.168.1.211/24 dev vonk-mgmt
/usr/sbin/ip address add 192.168.100.10/24 dev vonk-fabric
/usr/sbin/ip link set vonk-mgmt up
/usr/sbin/ip link set vonk-fabric up
$helper --config "$config" apply
$helper --config "$config" check
$helper --config "$config" check-host-port 8888
$helper --config "$config" apply
$helper --config "$config" check
test "$($iptables -S DOCKER-USER | sed -n '/^-A DOCKER-USER /{p;q;}')" = \
    '-A DOCKER-USER -j VONK-FORGE'
test "$($iptables -S VONK-FORGE | awk '$1 == "-A" { count++ } END { print count+0 }')" = 12
test "$($iptables -S INPUT | sed -n '/^-A INPUT /{p;q;}')" = \
    '-A INPUT -j VONK-FORGE-HOST'
test "$($iptables -S VONK-FORGE-HOST | awk '$1 == "-A" { count++ } END { print count+0 }')" = 11
$iptables -C VONK-FORGE-HOST -i lo -p tcp --dport 8888 -j RETURN
$iptables -C VONK-FORGE-HOST -i lo -s 192.168.100.10 \
    -d 192.168.100.10 -p tcp -j RETURN
$iptables -C VONK-FORGE-HOST -i lo -s 192.168.100.10 \
    -d 192.168.100.10 -p udp -j RETURN
$iptables -C VONK-FORGE-HOST -i vonk-mgmt -p tcp -s 192.168.1.231 \
    --dport 8888 -j RETURN
$iptables -C VONK-FORGE-HOST -p tcp --dport 8888 -j DROP
$iptables -C VONK-FORGE-HOST -i vonk-fabric -s 192.168.100.11 \
    -d 192.168.100.10 -p tcp -j RETURN
$iptables -C VONK-FORGE-HOST -i vonk-fabric -s 192.168.100.11 \
    -d 192.168.100.10 -p udp -j RETURN
$iptables -C VONK-FORGE-HOST -d 192.168.100.10 -p tcp -j DROP
$iptables -C VONK-FORGE-HOST -d 192.168.100.10 -p udp -j DROP
$iptables -C VONK-FORGE -i vonk-mgmt -p tcp -s 192.168.1.231 \
    -m conntrack --ctorigdst 192.168.1.211 --ctorigdstport 8000 -j RETURN
$iptables -C VONK-FORGE -i vonk-fabric -p tcp -s 192.168.100.11 \
    -m conntrack --ctorigdst 192.168.100.10 --ctorigdstport 29500 -j RETURN
$iptables -C VONK-FORGE -p tcp -m conntrack \
    --ctorigdst 192.168.1.211 -j DROP
$iptables -C VONK-FORGE -p tcp -m conntrack \
    --ctorigdst 192.168.100.10 -j DROP

$iptables -I VONK-FORGE 1 -j RETURN
if $helper --config "$config" check >/dev/null 2>&1; then
    echo "drifted managed chain was accepted" >&2
    exit 1
fi
$helper --config "$config" apply
$helper --config "$config" check

$iptables -D DOCKER-USER -j VONK-FORGE
$iptables -F VONK-FORGE
$iptables -X VONK-FORGE
$iptables -N VONK-FORGE
$iptables -A VONK-FORGE -j RETURN
if $helper --config "$config" apply >/dev/null 2>&1; then
    echo "foreign managed-chain name was accepted" >&2
    exit 1
fi
EOF

echo "Docker firewall namespace acceptance: passed"
