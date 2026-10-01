"""
How this server is reached from other devices: its addresses on the home network and on Tailscale, the
``syntropyhealth.local`` name it announces with Bonjour (mDNS), and, in the Mac and Windows apps, switching between
"only this computer" and "this computer and other devices".

``syntropy-health serve`` and the desktop apps set ``SYNTROPY_LISTEN_HOST`` and ``SYNTROPY_LISTEN_PORT``; plain
``uvicorn app.main:app`` (Docker, development) leaves them unset and nothing here is announced.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

from app.core import config

log = logging.getLogger("syntropy.network")
# zeroconf logs a traceback for each interface it can't send on yet (at startup, or before macOS allows local
# network access); the name is announced on the others regardless.
logging.getLogger("zeroconf").setLevel(logging.ERROR)

LOCAL_NAME = "syntropyhealth"           # announced as syntropyhealth.local
SERVICE_TYPE = "_http._tcp.local."
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
# Virtual machines, containers and VPN bridges: their addresses mean nothing to a phone on the home network.
VIRTUAL_PREFIXES = ("docker", "br-", "veth", "vmnet", "vboxnet", "virbr", "bridge", "cni", "flannel", "tailscale",
                    "zt", "feth", "utun", "wg", "tun", "tap", "lxc", "podman", "vEthernet")

# Set by the Mac and Windows apps: called with True or False to listen on the network or only on this computer.
_controller: Optional[Callable[[bool], None]] = None


def set_controller(fn: Optional[Callable[[bool], None]]) -> None:
    global _controller
    _controller = fn


def managed() -> bool:
    """True in the Mac and Windows apps, which can switch network access on and off themselves."""
    return _controller is not None


def listen_host() -> str:
    return config.env("SYNTROPY_LISTEN_HOST")


def listen_port() -> Optional[int]:
    raw = config.env("SYNTROPY_LISTEN_PORT")
    return int(raw) if raw.isdigit() else None


def on_network() -> bool:
    return listen_host() in ("0.0.0.0", "::")


def set_on_network(enabled: bool) -> None:
    if not _controller:
        raise RuntimeError("This server's network access is set by how it is started.")
    _controller(enabled)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

def primary_address() -> Optional[str]:
    """The address of the interface with the default route: the one other devices at home use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))      # no packet is sent; this only picks the interface
            addr = s.getsockname()[0]
    except OSError:
        return None
    ip = ipaddress.ip_address(addr)
    return None if ip.is_loopback or ip in TAILSCALE_NET else addr


def _interfaces() -> list[tuple[str, str]]:
    """(interface, IPv4 address) for every interface that's up."""
    try:
        import ifaddr
    except ImportError:
        return []
    found = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str):
                found.append((adapter.nice_name or adapter.name, ip.ip))
    return found


def home_addresses() -> list[str]:
    """Home-network addresses, the default route's first; Tailscale and virtual interfaces left out."""
    primary = primary_address()
    found = [primary] if primary else []
    for name, addr in _interfaces():
        ip = ipaddress.ip_address(addr)
        if (addr in found or ip.is_loopback or ip.is_link_local or ip in TAILSCALE_NET or not ip.is_private
                or name.lower().startswith(tuple(p.lower() for p in VIRTUAL_PREFIXES))):
            continue
        found.append(addr)
    return found


def tailscale_addresses() -> list[str]:
    return [addr for _, addr in _interfaces() if ipaddress.ip_address(addr) in TAILSCALE_NET]


_ts_cache: dict[str, Any] = {"at": 0.0, "name": None}


def _tailscale_cli() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    for path in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                 r"C:\Program Files\Tailscale\tailscale.exe"):
        if os.path.exists(path):
            return path
    return None


def tailscale_name() -> Optional[str]:
    """This computer's MagicDNS name (e.g. mac-mini.tail1234.ts.net), when Tailscale and MagicDNS are on."""
    if time.time() - _ts_cache["at"] < 60:
        return _ts_cache["name"]
    name = None
    cli = _tailscale_cli()
    if cli and tailscale_addresses():
        try:
            out = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=4,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            status = json.loads(out.stdout or "{}")
            if status.get("CurrentTailnet", {}).get("MagicDNSEnabled", True):
                name = (status.get("Self", {}).get("DNSName") or "").rstrip(".") or None
        except (OSError, ValueError, subprocess.SubprocessError):
            name = None
    _ts_cache.update(at=time.time(), name=name)
    return name


def describe(port: Optional[int] = None) -> dict[str, Any]:
    """Where this server can be opened, for Settings, onboarding and the iPhone pairing screen."""
    port = port or listen_port() or 8000
    reach = on_network()
    urls: list[dict[str, str]] = []
    if reach:
        name = advertiser.name
        if name:
            urls.append({"kind": "name", "label": "On your home network", "url": f"http://{name}:{port}"})
        for addr in home_addresses():
            urls.append({"kind": "home", "label": "Home network address", "url": f"http://{addr}:{port}"})
        ts_name = tailscale_name()
        if ts_name:
            urls.append({"kind": "tailscale", "label": "Tailscale", "url": f"http://{ts_name}:{port}"})
        for addr in tailscale_addresses():
            urls.append({"kind": "tailscale", "label": "Tailscale address", "url": f"http://{addr}:{port}"})
    return {"managed": managed(), "on_network": reach, "port": port, "local_url": f"http://localhost:{port}",
            "local_name": advertiser.name if reach else None, "addresses": urls,
            "tailscale": bool(tailscale_addresses())}


def phone_address(port: Optional[int]) -> Optional[str]:
    """The address the iPhone app should use at home, when this server listens on the network."""
    if not on_network():
        return None
    home = home_addresses()
    return home[0] if home else None


LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localhost", ".ts.net")


def is_local_name(host: str) -> bool:
    """Whether a Host header names this computer, the home network or Tailscale, judged from the name alone.

    Deliberately never resolved: a web page using DNS rebinding points its own domain at 127.0.0.1, and would pass
    any check that looks up the address. ``SYNTROPY_ALLOWED_HOSTS`` (comma-separated) adds names such as a reverse
    proxy's domain."""
    host = (host or "").lower()
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.rstrip(".")
    if not host:
        return False
    extra = {h.strip().lower() for h in config.env("SYNTROPY_ALLOWED_HOSTS").split(",") if h.strip()}
    if host in extra or host == "localhost" or "." not in host and ":" not in host or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in TAILSCALE_NET


# ---------------------------------------------------------------------------
# Bonjour: syntropyhealth.local
# ---------------------------------------------------------------------------

def _resolves_elsewhere(host: str, mine: set[str]) -> bool:
    """Whether another machine already answers to ``host`` (checked through the system's own resolver)."""
    result: list[set[str]] = []

    def look() -> None:
        try:
            result.append({i[4][0] for i in socket.getaddrinfo(host, None, socket.AF_INET)})
        except OSError:
            result.append(set())

    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(3)
    return bool(result and result[0] and not result[0] <= mine)


class Advertiser:
    """Announces ``syntropyhealth.local`` (or ``syntropyhealth-<computer>.local`` if that name is taken) and an
    ``_http._tcp`` service, and follows this computer to a new address when the network changes."""

    def __init__(self) -> None:
        self.zc = None
        self.info = None
        self.name: Optional[str] = None
        self.address: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self, port: int) -> None:
        if self._thread or config.env("SYNTROPY_MDNS").lower() in ("0", "false", "off"):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(port,), daemon=True, name="mdns")
        self._thread.start()

    def _run(self, port: int) -> None:
        try:
            from zeroconf import IPVersion, Zeroconf
        except ImportError:
            log.info("zeroconf isn't installed; %s.local won't be announced", LOCAL_NAME)
            return
        while not self._stop.is_set():
            addr = primary_address()
            if addr != self.address:
                with self._lock:
                    self._unregister()
                    if addr:
                        try:
                            if self.zc is None:
                                self.zc = Zeroconf(ip_version=IPVersion.V4Only)
                            self._register(addr, port)
                        except Exception:  # noqa: BLE001 - never take the server down over a name
                            log.exception("Couldn't announce %s.local", LOCAL_NAME)
                    self.address = addr
            self._stop.wait(30)

    def _register(self, addr: str, port: int) -> None:
        from zeroconf import ServiceInfo

        name = f"{LOCAL_NAME}.local"
        if _resolves_elsewhere(name, {addr, "127.0.0.1"}):
            computer = socket.gethostname().split(".")[0].lower().replace(" ", "-")
            name = f"{LOCAL_NAME}-{computer}.local"
        self.info = ServiceInfo(
            SERVICE_TYPE, f"Syntropy Health.{SERVICE_TYPE}", port=port, server=f"{name}.",
            addresses=[socket.inet_aton(addr)],
            properties={"path": "/", "service": "syntropy-health", "version": config.APP_VERSION,
                        "computer": computer_name()})
        self.zc.register_service(self.info, allow_name_change=True)
        self.name = name
        log.info("Announced http://%s:%s (%s)", name, port, addr)

    def _unregister(self) -> None:
        if self.zc and self.info:
            try:
                self.zc.unregister_service(self.info)
            except Exception:  # noqa: BLE001
                pass
        self.info, self.name = None, None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
        with self._lock:
            self._unregister()
            if self.zc:
                self.zc.close()
            self.zc, self.address, self._thread = None, None, None


advertiser = Advertiser()


def computer_name() -> str:
    """This computer's name as people know it ("Enrique's MacBook Pro"), for other Macs choosing a server to join."""
    if sys.platform == "darwin":
        try:
            name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=2).stdout.strip()
            if name:
                return name[:80]
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname().split(".")[0].replace("-", " ")[:80]


def discover(seconds: float = 2.5) -> list[dict[str, Any]]:
    """Syntropy Health servers announcing themselves on the home network (for a Mac joining one instead of running
    its own): [{name, url, address, version}]. Empty when zeroconf isn't installed or nothing answers."""
    try:
        from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return []
    found: dict[str, dict[str, Any]] = {}
    own = {primary_address(), "127.0.0.1"}
    own_port = listen_port() if on_network() else None

    class Listener(ServiceListener):
        def add_service(self, zc: Any, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=1500)
            if not info:
                return
            props = {k.decode(errors="ignore"): (v or b"").decode(errors="ignore") for k, v in info.properties.items()}
            if props.get("service") != "syntropy-health":
                return
            addresses = info.parsed_addresses(IPVersion.V4Only)
            if own_port and info.port == own_port and set(addresses) & own:
                return      # this computer's own server
            host = (info.server or "").rstrip(".")
            address = f"http://{addresses[0]}:{info.port}" if addresses else None
            # Servers before 1.1 don't announce their computer's name.
            fallback = f"Syntropy Health at {addresses[0]}" if addresses else host or name.split(".")[0]
            found[name] = {"name": props.get("computer") or fallback,
                           "url": f"http://{host}:{info.port}" if host else address, "address": address,
                           "version": props.get("version")}

        update_service = add_service

        def remove_service(self, zc: Any, type_: str, name: str) -> None:
            pass

    zc = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        ServiceBrowser(zc, SERVICE_TYPE, Listener())
        time.sleep(seconds)
    finally:
        zc.close()
    return sorted(found.values(), key=lambda s: s["name"].lower())


def start_announcing() -> None:
    """On server start: announce the name when listening on the network (not in Docker, whose network is private)."""
    from app.services.ai_local import in_docker
    port = listen_port()
    if on_network() and port and not in_docker():
        advertiser.start(port)


def stop_announcing() -> None:
    advertiser.stop()
