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
    'VONK_NAS_MANAGEMENT_IP=192.0.2.231' \
    'VONK_NODE_MANAGEMENT_IP=192.0.2.211' \
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
/usr/sbin/ip address add 192.0.2.211/24 dev vonk-mgmt
/usr/sbin/ip address add 192.168.100.10/24 dev vonk-fabric
/usr/sbin/ip link set vonk-mgmt up
/usr/sbin/ip link set vonk-fabric up
$helper --config "$config" apply
$helper --config "$config" check
$helper --config "$config" check-host-port 8888
$helper --config "$config" check-endpoint-port 8000
$helper --config "$config" check-endpoint-port 8101
# A published port outside the authorised set would run unreachable behind the
# managed chain's final DROP, so the refusal must say which port and which set.
# Exit status 3 is a positive refusal; the helper fails the start only on it.
status=0
refusal=$($helper --config "$config" check-endpoint-port 30000 2>&1) || status=$?
test "$status" = 3
case "$refusal" in
    *"published endpoint host port 30000 is not authorized"*"authorized endpoint host ports: 8000,8101"*) ;;
    *) echo "endpoint port refusal does not name the port: $refusal" >&2; exit 1 ;;
esac
status=0
$helper --config "$config" check-endpoint-port 8888 >/dev/null 2>&1 || status=$?
test "$status" = 3
test "$($helper --config "$config" check-fabric-run 192.168.100.10 192.168.100.10 29500 8888)" = vonk-fabric
test "$($helper --config "$config" check-fabric-run 192.168.100.10 192.168.100.11 29500 none)" = vonk-fabric
# A multi-rank job's rendezvous owner serves no endpoint port and passes 0.
test "$($helper --config "$config" check-fabric-run 192.168.100.10 192.168.100.10 29500 0)" = vonk-fabric
for invalid in \
    '192.168.100.10 192.168.100.10 29500 none' \
    '192.168.100.10 192.168.100.11 29500 0' \
    '192.0.2.211 192.168.100.10 29500 8888' \
    '192.168.100.10 192.0.2.231 29500 none' \
    '192.168.100.10 192.168.100.10 29501 8888' \
    '192.168.100.10 192.168.100.10 29500 8000' \
    '192.168.100.10 192.168.100.11 29500 8888'; do
    # Each literal is a deliberately invalid fixed request, never shell code.
    # shellcheck disable=SC2086
    if $helper --config "$config" check-fabric-run $invalid >/dev/null 2>&1; then
        echo "native fabric admitted an unauthorized placement" >&2
        exit 1
    fi
done
# The refusal must name the refused argument and what the node authorises, so
# an operator can read the cause from the helper journal.
refusal=$($helper --config "$config" check-fabric-run \
    192.168.100.10 192.168.100.10 29500 8000 2>&1 || true)
case "$refusal" in
    *"host endpoint port 8000 is not authorized"*"authorized host endpoint ports: 8888"*) ;;
    *) echo "host endpoint refusal does not name the port: $refusal" >&2; exit 1 ;;
esac
refusal=$($helper --config "$config" check-fabric-run \
    192.168.100.10 192.168.100.10 29501 8888 2>&1 || true)
case "$refusal" in
    *"rendezvous port 29501 is not authorized"*"29500"*) ;;
    *) echo "rendezvous refusal does not name the port: $refusal" >&2; exit 1 ;;
esac
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
$iptables -C VONK-FORGE-HOST -i vonk-mgmt -p tcp -s 192.0.2.231 \
    --dport 8888 -j RETURN
$iptables -C VONK-FORGE-HOST -p tcp --dport 8888 -j DROP
$iptables -C VONK-FORGE-HOST -i vonk-fabric -s 192.168.100.11 \
    -d 192.168.100.10 -p tcp -j RETURN
$iptables -C VONK-FORGE-HOST -i vonk-fabric -s 192.168.100.11 \
    -d 192.168.100.10 -p udp -j RETURN
$iptables -C VONK-FORGE-HOST -d 192.168.100.10 -p tcp -j DROP
$iptables -C VONK-FORGE-HOST -d 192.168.100.10 -p udp -j DROP
peer_tcp_position=$($iptables -S VONK-FORGE-HOST | awk \
    '/-i vonk-fabric/ && /-s 192\.168\.100\.11/ && /-p tcp/ && /-j RETURN/ { print NR }')
endpoint_drop_position=$($iptables -S VONK-FORGE-HOST | awk \
    '/-p tcp/ && /--dport 8888/ && /-j DROP/ { print NR }')
test -n "$peer_tcp_position"
test -n "$endpoint_drop_position"
test "$peer_tcp_position" -lt "$endpoint_drop_position"
$iptables -D VONK-FORGE-HOST -i vonk-fabric -s 192.168.100.11 \
    -d 192.168.100.10 -p tcp -j RETURN
$iptables -A VONK-FORGE-HOST -i vonk-fabric -s 192.168.100.11 \
    -d 192.168.100.10 -p tcp -j RETURN
if $helper --config "$config" check >/dev/null 2>&1; then
    echo "host endpoint drop shadowed peer traffic without detection" >&2
    exit 1
fi
$helper --config "$config" apply
$helper --config "$config" check
$iptables -C VONK-FORGE -i vonk-mgmt -p tcp -s 192.0.2.231 \
    -m conntrack --ctorigdst 192.0.2.211 --ctorigdstport 8000 -j RETURN
$iptables -C VONK-FORGE -i vonk-fabric -p tcp -s 192.168.100.11 \
    -m conntrack --ctorigdst 192.168.100.10 --ctorigdstport 29500 -j RETURN
$iptables -C VONK-FORGE -p tcp -m conntrack \
    --ctorigdst 192.0.2.211 -j DROP
$iptables -C VONK-FORGE -p tcp -m conntrack \
    --ctorigdst 192.168.100.10 -j DROP

# Replies of the workload uid leave the management interface only toward the
# NAS endpoint ports: one rule set per family, IPv6 without a NAS rule.
ip6tables=/usr/sbin/ip6tables
test "$($iptables -S OUTPUT | sed -n '/^-A OUTPUT /{p;q;}')" = '-A OUTPUT -j VONK-FORGE-OUT'
test "$($ip6tables -S OUTPUT | sed -n '/^-A OUTPUT /{p;q;}')" = '-A OUTPUT -j VONK-FORGE-OUT'
test "$($iptables -S VONK-FORGE-OUT | awk '$1 == "-A" { count++ } END { print count+0 }')" = 6
test "$($ip6tables -S VONK-FORGE-OUT | awk '$1 == "-A" { count++ } END { print count+0 }')" = 3
for family in "$iptables" "$ip6tables"; do
    $family -C VONK-FORGE-OUT -m conntrack --ctdir ORIGINAL -j RETURN
    $family -C VONK-FORGE-OUT -o vonk-mgmt -m owner --uid-owner 10001 -j DROP
done
for port in 8000 8101 8888; do
    $iptables -C VONK-FORGE-OUT -o vonk-mgmt -m owner --uid-owner 10001 -p tcp \
        -d 192.0.2.231 --sport "$port" -j RETURN
done
if $ip6tables -S VONK-FORGE-OUT | grep -q -e '--sport'; then
    echo "the IPv6 output chain names a NAS port" >&2
    exit 1
fi
# A node upgraded from a release without the output chain is refused until the
# firewall is applied again, and one apply converges it.
for family in "$iptables" "$ip6tables"; do
    $family -D OUTPUT -j VONK-FORGE-OUT
    $family -F VONK-FORGE-OUT
    $family -X VONK-FORGE-OUT
done
if $helper --config "$config" check >/dev/null 2>&1; then
    echo "a node without the output chain was accepted" >&2
    exit 1
fi
$helper --config "$config" apply
$helper --config "$config" check
$ip6tables -I VONK-FORGE-OUT 1 -j RETURN
if $helper --config "$config" check >/dev/null 2>&1; then
    echo "a drifted IPv6 output chain was accepted" >&2
    exit 1
fi
$helper --config "$config" apply
$helper --config "$config" check

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


# Behaviour on a real kernel: a client on the "LAN" across a veth cannot talk to
# a workload-uid listener on any address, the NAS address still can, host
# services and the workload's own connections are untouched.
for tool in nsenter setpriv python3; do
    command -v "$tool" >/dev/null || { echo "$tool is unavailable" >&2; exit 77; }
done
cat > "$temporary/probe.py" <<'PY'
import socket
import sys

mode, family, address, port = sys.argv[1:5]
kind = socket.AF_INET6 if family == "6" else socket.AF_INET
sock = socket.socket(kind, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
if mode == "listen":
    sock.bind((address, int(port)))
    sock.listen(8)
    print("ready", flush=True)
    while True:
        peer, _ = sock.accept()
        peer.sendall(b"ok")
        peer.close()
else:
    sock.settimeout(2)
    if len(sys.argv) > 5:
        sock.bind((sys.argv[5], 0))
    sock.connect((address, int(port)))
    assert sock.recv(2) == b"ok"
PY
chmod 0755 "$temporary" "$temporary/probe.py"
unshare --net -- /bin/sh -seu -- "$helper" "$config" "$temporary/probe.py" <<'EOF'
helper=$1
config=$2
probe=$3
ip=/usr/sbin/ip
$ip link set lo up
unshare --net sleep 600 &
lan=$!
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
until [ "$(readlink "/proc/$lan/ns/net")" != "$(readlink /proc/self/ns/net)" ]; do sleep 0.1; done
$ip link add vonk-mgmt type veth peer name lan0 netns "$lan"
$ip link add vonk-fabric type dummy
$ip address add 192.0.2.211/24 dev vonk-mgmt
$ip address add fd00:2::211/64 dev vonk-mgmt nodad
$ip address add 192.168.100.10/24 dev vonk-fabric
$ip link set vonk-mgmt up
$ip link set vonk-fabric up
inlan() { nsenter -t "$lan" -n "$@"; }
inlan $ip link set lo up
inlan $ip address add 192.0.2.50/24 dev lan0
inlan $ip address add 192.0.2.231/24 dev lan0
inlan $ip address add fd00:2::50/64 dev lan0 nodad
inlan $ip link set lan0 up
/usr/sbin/iptables -N DOCKER-USER
$helper --config "$config" apply
$helper --config "$config" check

workload() { setpriv --reuid=10001 --regid=10001 --clear-groups python3 -I "$probe" "$@"; }
for name in 4 6 host lan; do : > "$probe.$name"; done
workload listen 4 0.0.0.0 8000 > "$probe.4" &
workload listen 6 :: 8001 > "$probe.6" &
python3 -I "$probe" listen 4 0.0.0.0 9000 > "$probe.host" &
inlan python3 -I "$probe" listen 4 192.0.2.50 7000 > "$probe.lan" &
until grep -q ready "$probe.4" && grep -q ready "$probe.6" \
    && grep -q ready "$probe.host" && grep -q ready "$probe.lan"; do sleep 0.1; done

# A LAN client gets no answer from the workload on IPv4 or IPv6.
if inlan python3 -I "$probe" connect 4 192.0.2.211 8000 2>/dev/null; then
    echo "a LAN client reached the workload over IPv4" >&2
    exit 1
fi
if inlan python3 -I "$probe" connect 6 fd00:2::211 8001 2>/dev/null; then
    echo "a LAN client reached the workload over IPv6" >&2
    exit 1
fi
# The NAS reaches its endpoint port.
inlan python3 -I "$probe" connect 4 192.0.2.211 8000 192.0.2.231
# A host service of another uid is untouched.
inlan python3 -I "$probe" connect 4 192.0.2.211 9000
# The workload's own connection to the LAN is not a reply.
workload connect 4 192.0.2.50 7000
EOF
echo "Docker firewall namespace acceptance: passed"
