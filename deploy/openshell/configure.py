"""Check or update the local callback address from the resolved Compose model."""

import argparse
import ipaddress
import json
import re
import subprocess
import tomllib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="update only grpc_endpoint")
    args = parser.parse_args()
    compose = Path(__file__).with_name("compose.yaml")
    model = json.loads(subprocess.check_output(
        ["docker", "compose", "-f", str(compose), "config", "--format", "json"], text=True))
    service = model["services"]["gateway"]
    address = service["networks"]["gateway"]["ipv4_address"]
    subnet = ipaddress.ip_network(model["networks"]["gateway"]["ipam"]["config"][0]["subnet"])
    if ipaddress.ip_address(address) not in subnet:
        raise SystemExit("Gateway address is outside the Compose subnet")
    networks = [json.loads(line) for line in subprocess.check_output(
        ["docker", "network", "ls", "--format", "json"], text=True).splitlines()]
    for network in networks:
        if network["Name"] == model["networks"]["gateway"]["name"]:
            continue
        detail = json.loads(subprocess.check_output(
            ["docker", "network", "inspect", network["ID"]], text=True))[0]
        for item in detail["IPAM"].get("Config") or []:
            if item.get("Subnet") and ipaddress.ip_network(item["Subnet"]).version == 4:
                if subnet.overlaps(ipaddress.ip_network(item["Subnet"])):
                    raise SystemExit(f"Subnet overlaps Docker network {network['Name']}")
    routes = json.loads(subprocess.check_output(["ip", "-j", "-4", "route"], text=True))
    for route in routes:
        destination = route.get("dst", "default")
        if destination == "default" or route.get("dev", "").startswith(("br-", "docker")):
            continue
        if subnet.overlaps(ipaddress.ip_network(destination, strict=False)):
            raise SystemExit(f"Subnet overlaps host route {destination}")
    config = Path(next(v["source"] for v in service["volumes"]
                       if v["target"] == "/etc/openshell/gateway.toml"))
    source = config.read_text()
    endpoint = f"http://{address}:18080"
    current = tomllib.loads(source)["openshell"]["drivers"]["docker"]["grpc_endpoint"]
    if current != endpoint:
        if not args.apply:
            raise SystemExit(f"Callback differs from Compose; run {Path(__file__)} --apply before recreation")
        updated, count = re.subn(r'^grpc_endpoint\s*=.*$', f'grpc_endpoint = "{endpoint}"', source, flags=re.M)
        if count != 1:
            raise SystemExit("Expected exactly one grpc_endpoint; no changes written")
        backup = config.with_suffix(".toml.before-fixed-network")
        if not backup.exists():
            backup.write_bytes(config.read_bytes())
            backup.chmod(config.stat().st_mode & 0o777)
        config.write_text(updated)
    print(f"Callback verified: {endpoint}; subnet {subnet}")


if __name__ == "__main__":
    main()