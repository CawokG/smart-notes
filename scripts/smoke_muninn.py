#!/usr/bin/env python3
"""Characterize pinned MuninnDB using only a fresh, disposable local instance.

No product adapter, existing server URL, existing data directory, or retry of a
write is supported. A successful run may report an UNSUPPORTED capability.
The report is synthetic and redacted; credentials and the database are removed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from typing import Callable
from urllib.parse import urlencode
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
VERSION = "v0.9.0"
SHA256 = "4da4c959a6482c7608809a75501cd3c746875c1dcd9c7caf7e6d3052dbdefa5b"
DOWNLOAD = f"https://github.com/scrypster/muninndb/releases/download/{VERSION}/muninn-linux-amd64"
MARKER = "ki_op:stage3-exact"
VAULT = "ki-spike"
OTHER_VAULT = "ki-other"
ACTIVE_COUNT = 205
DELETED_COUNT = 101


class SmokeFailure(RuntimeError):
    """An assertion or transport failure invalidated this characterization."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def redact(value, secret_values=()):
    """Remove credential fields and exact secrets, including nested error text."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.casefold() in {
                "token", "password", "authorization", "cookie", "set-cookie", "storage_hash"
            } else redact(item, secret_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secret_values) for item in value]
    if isinstance(value, str):
        for secret in secret_values:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def shape(value):
    """Keep real JSON field names/types without dumping all synthetic rows."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return {"type": "array", "length": len(value), "item": shape(value[0]) if value else None}
    return type(value).__name__


def collect_tag_pages(fetch_page: Callable, tags: list[str], page_size: int = 200):
    """Validate one quiescent list query, including a final empty page.

    This is a smoke assertion, NOT an exhaustive recovery implementation. In
    particular, default /api/engrams excludes archived and soft-deleted rows.
    Reject duplicates, ignored filters, changing totals and stalled/truncated
    pages instead of mistaking them for a complete result.
    """
    require(1 <= page_size <= 200, "invalid page size")
    rows, pages, seen = [], [], set()
    total = None
    offset = 0
    for _ in range(10000):
        page = fetch_page(offset, page_size)
        require(isinstance(page, dict), "list response must be an object")
        require(all(type(page.get(key)) is int for key in ("total", "offset", "limit")), "invalid list metadata")
        require(page["offset"] == offset and 0 < page["limit"] <= page_size, "incorrect pagination metadata")
        require(page["total"] >= 0, "negative total")
        if total is None:
            total = page["total"]
        require(page["total"] == total, "total changed during pagination")
        items = page.get("engrams")
        require(isinstance(items, list) and len(items) <= page["limit"], "invalid list page")
        pages.append({"offset": offset, "limit": page["limit"], "returned": len(items), "total": total})
        require(offset + len(items) <= total, "page exceeds reported total")
        if not items:
            require(offset == total, "list ended before reported total")
            return rows, pages
        require(len(items) == page["limit"] or offset + len(items) == total, "unexpected short page")
        for item in items:
            require(isinstance(item, dict) and isinstance(item.get("id"), str), "missing list ID")
            require(item["id"] not in seen, "duplicate ID across pages")
            require(isinstance(item.get("tags"), list) and set(tags) <= set(item["tags"]), "tag filter was not exact AND")
            seen.add(item["id"])
            rows.append(item)
        offset += len(items)
    raise SmokeFailure("pagination exceeded smoke budget")


def verify_binary(binary: Path) -> None:
    with binary.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    require(digest == SHA256, "binary SHA-256 does not match pinned Linux x86-64 release")


def isolated_environment(data: Path, password: str, mcp_token: str):
    # v0.9.0 unconditionally reads ~/.muninn/muninn.env before dispatch. Do not
    # rewrite HOME, copy user config or let this file supply remote providers.
    # Refuse it instead. The explicit MCP token prevents ~/.muninn/mcp.token use.
    require(not (Path.home() / ".muninn/muninn.env").is_symlink()
            and not (Path.home() / ".muninn/muninn.env").exists(),
            "ambient ~/.muninn/muninn.env exists; use a clean OS account/container for this smoke")
    env = {key: value for key, value in os.environ.items() if not key.startswith("MUNINN")}
    env.update(MUNINN_LOCAL_EMBED="0", MUNINN_ENRICH_URL="", MUNINN_ADMIN_PASSWORD=password,
               MUNINN_MCP_TOKEN=mcp_token, MUNINNDB_DATA=str(data))
    return env


class Probe:
    def __init__(self, ports, report, password, mcp_token):
        self.ports = ports
        self.report = report
        self.password = password
        self.tokens = {"static_mcp": mcp_token}
        self.cookie = ""
        self.calls = Counter()
        self.examples = {}
        self.last_request = 0.0

    def secrets(self):
        return [self.password, self.cookie, self.cookie.partition("=")[2], *self.tokens.values()]

    def request(self, method, path, body=None, role=None, service="rest", admin=False):
        # Stay below the release's default 100 requests/s per-IP limiter.
        time.sleep(max(0.0, 0.012 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        headers = {"Content-Type": "application/json"}
        if role:
            headers["Authorization"] = "Bearer " + self.tokens[role]
        if admin:
            headers["Cookie"] = self.cookie
        connection = http.client.HTTPConnection("127.0.0.1", self.ports[service], timeout=10)
        route = path.split("?", 1)[0]
        # http.client uses only the selected loopback port, with no proxy,
        # redirects, ambient cookie jar or access to an existing backend URL.
        try:
            connection.request(method, path, None if body is None else json.dumps(body, ensure_ascii=False).encode(), headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
            cookie = response.getheader("Set-Cookie")
            if cookie:
                self.cookie = cookie.split(";", 1)[0]
            data = json.loads(raw) if raw else None
        except (OSError, ValueError, http.client.HTTPException) as error:
            self.calls[f"{service} {method} {route} transport_error"] += 1
            # No network exception is retried, especially not a POST. A lost
            # response does not establish that the synthetic write failed.
            raise SmokeFailure(f"{method} {route}: {type(error).__name__}; no retry; unknown writes require needs_manual_repair") from None
        finally:
            connection.close()
        key = f"{service} {method} {route} {status} {role or ('admin' if admin else 'none')}"
        self.calls[key] += 1
        # First response shape per route/status/auth is sufficient for adapter
        # field discovery and does not retain hundreds of fixture bodies.
        self.examples.setdefault(key, shape(redact(data, self.secrets())))
        return status, data

    def expect(self, method, path, body=None, *, status=200, **kwargs):
        actual, data = self.request(method, path, body, **kwargs)
        require(actual == status, f"{method} {path.split('?', 1)[0]} expected {status}, got {actual}")
        return data

    def list_tags(self, tags, state=None, page_size=200):
        def fetch(offset, limit):
            query = {"vault": VAULT, "tags": ",".join(tags), "sort": "created", "offset": offset, "limit": limit}
            if state:
                query["state"] = state
            return self.expect("GET", "/api/engrams?" + urlencode(query), role="observe")
        return collect_tag_pages(fetch, tags, page_size)

    def create(self, concept, content, tags, entities=None, role="full", vault=VAULT):
        payload = {"vault": vault, "concept": concept, "content": content, "tags": tags, "confidence": 0.75}
        if entities:
            payload["entities"] = entities
        data = self.expect("POST", "/api/engrams?" + urlencode({"vault": vault}), payload, role=role, status=201)
        require(isinstance(data.get("id"), str) and len(data["id"]) == 26, "write response missing ULID")
        return data["id"], payload

    def read(self, engram_id, role="observe"):
        return self.expect("GET", f"/api/engrams/{engram_id}?vault={VAULT}", role=role)

    def rpc(self, method, params=None, role="observe", status=200):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        return self.expect("POST", "/mcp", body, role=role, service="mcp", status=status)

    def tool(self, name, arguments):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments})
        require("error" not in response and not response.get("result", {}).get("isError"), f"MCP {name} failed")
        content = response.get("result", {}).get("content", [])
        require(len(content) == 1 and content[0].get("type") == "text", "unexpected MCP result envelope")
        return json.loads(content[0]["text"])


def characterize(probe: Probe, report):
    p = probe
    require(p.expect("GET", "/mcp/health", service="mcp") == {"status": "ok"}, "MCP health changed")
    report["health"] = p.expect("GET", "/api/health")
    p.expect("GET", "/api/admin/keys", status=401)
    p.expect("POST", "/api/auth/login", {"username": "root", "password": p.password}, service="ui")
    require(p.cookie.startswith("muninn_session="), "admin cookie not issued")
    for vault in (VAULT, OTHER_VAULT):
        p.expect("PUT", "/api/admin/vaults/config", {"name": vault, "public": False}, admin=True)
    for role, mode, vault in (("observe", "observe", VAULT), ("full", "full", VAULT), ("other_full", "full", OTHER_VAULT)):
        key = p.expect("POST", "/api/admin/keys", {"vault": vault, "label": "synthetic-spike-" + role, "mode": mode}, admin=True, status=201)
        require(key["key"]["mode"] == mode and key["key"]["vault"] == vault, "key mode/vault differs")
        p.tokens[role] = key["token"]
    p.expect("GET", f"/api/engrams?vault={VAULT}", status=401)
    p.tokens["invalid"] = "mk_synthetic_invalid"
    p.expect("GET", f"/api/engrams?vault={VAULT}", role="invalid", status=401)
    p.expect("GET", f"/api/engrams?vault={OTHER_VAULT}", role="observe", status=401)
    p.expect("POST", f"/api/engrams?vault={VAULT}", {"concept": "forbidden", "content": "Synthetic forbidden write"}, role="observe", status=403)
    empty = p.expect("GET", f"/api/engrams?vault={VAULT}", role="observe")
    require(empty["total"] == 0, "observe denial wrote a row")
    first_id, first_payload = p.create("Synthetic quasar 000", "Synthetic quasar 000. Алексей работает над Quasar Engine.", ["ki_v1", MARKER],
                                       [{"name": "Алексей", "type": "person"}, {"name": "Quasar Engine", "type": "project"}])
    first = p.read(first_id)
    require(all(first.get(key) == first_payload[key] for key in ("concept", "content", "tags", "confidence")), "read-back differs from acknowledged write")
    require(p.expect("GET", f"/api/engrams/{first_id}", role="observe")["id"] == first_id, "key-bound GET without vault failed")
    p.expect("GET", f"/api/engrams/{'0' * 26}?vault={VAULT}", role="observe", status=404)
    p.expect("POST", f"/api/engrams?vault={VAULT}", {"vault": OTHER_VAULT, "concept": "mismatch", "content": "Synthetic rejected vault mismatch"}, role="full", status=400)
    report["auth_and_readback"] = {"status": "PASS", "private_no_token": 401, "invalid_token": 401, "query_vault_mismatch": 401,
                                      "body_vault_mismatch": 400, "observe_write": 403, "full_write": 201, "readback": 200,
                                      "readback_example": first, "write_response_fields": ["id", "created_at"]}
    activation = {"vault": VAULT, "context": ["Synthetic quasar 000"], "max_results": 5, "threshold": 0.0, "disable_hops": True}
    counter_results = {}
    for mode in ("observe", "full"):
        before = p.read(first_id)
        for _ in range(3):
            p.read(first_id, role=mode)
        query_ids = []
        for _ in range(3):
            response = p.expect("POST", f"/api/activate?vault={VAULT}", activation, role=mode)
            require(first_id in {item["id"] for item in response["activations"]}, "ACTIVATE did not return counter fixture")
            query_ids.append(response["query_id"])
        # Bounded observation window: do not infer anything about unexposed
        # learning metadata from stable engram counters alone.
        deadline = time.monotonic() + 2
        samples = []
        while time.monotonic() < deadline:
            samples.append(p.read(first_id)["access_count"])
            time.sleep(0.2)
        after = p.read(first_id)
        counter_results[mode] = {"get_calls": 3, "activate_calls": 3, "window_seconds": 2,
                                 "access_count_before": before["access_count"], "access_count_after": after["access_count"],
                                 "sample_counts": samples, "last_access_unchanged": before["last_access"] == after["last_access"], "query_ids": query_ids}
        if mode == "observe":
            require(after["access_count"] == before["access_count"] and all(n == before["access_count"] for n in samples), "observe changed access_count")
    duplicate = p.expect("POST", f"/api/engrams?vault={VAULT}", first_payload, role="full", status=201)
    require(duplicate["id"] == first_id, "controlled duplicate-content write changed ID")
    duplicate_count = p.read(first_id)["access_count"]
    require(duplicate_count > first["access_count"], "counter exposure positive control did not increase")
    report["observe_counters"] = {"status": "PASS_observed_counters_only", "runs": counter_results,
                                    "controlled_duplicate_content_count": duplicate_count,
                                    "limitation": "Full GET/ACTIVATE may also leave access_count unchanged; no claim of total cognitive side-effect freedom. Duplicate POST is an explicit acknowledged control, never an unknown-outcome retry."}
    for index in range(1, ACTIVE_COUNT):
        tags = [MARKER] if index == ACTIVE_COUNT - 1 else ["ki_v1", MARKER]
        entities = [{"name": "Алексей", "type": "person"}, {"name": "Quasar Engines", "type": "project"}] if index == 1 else None
        memory_id, _ = p.create(f"Synthetic marker {index:03}", f"Synthetic marker fixture {index:03}: person {index} is distinct; {MARKER}.", tags, entities)
        report.setdefault("fixture_marker_ids", []).append(memory_id)
    expected_ids = {first_id, *report["fixture_marker_ids"]}
    # All decoys mention marker text. Only complete, case-sensitive tag equality
    # may match; one real marker lacks ki_v1 to distinguish AND from OR.
    for index, tags in enumerate((["ki_v1", MARKER + "-suffix"], ["ki_v1", "prefix-" + MARKER], ["ki_v1", MARKER.upper()], ["ki_v1"])):
        p.create(f"Synthetic decoy {index}", f"Synthetic decoy {index}, text contains {MARKER} but has different tags.", tags)
    other_id, _ = p.create("Synthetic other vault", "Synthetic different vault marker control.", ["ki_v1", MARKER], role="other_full", vault=OTHER_VAULT)
    rows, pages = p.list_tags([MARKER])
    require({row["id"] for row in rows} == expected_ids and other_id not in expected_ids, "marker list omitted or added active fixture IDs")
    small_rows, small_pages = p.list_tags([MARKER], page_size=7)
    require({row["id"] for row in small_rows} == expected_ids, "small-page marker scan incomplete")
    and_rows, _ = p.list_tags(["ki_v1", MARKER])
    require(len(and_rows) == ACTIVE_COUNT - 1, "tags query is not AND")
    require(not p.list_tags([MARKER[:-1]])[0], "tag prefix matched")
    require(not p.list_tags(["ki_op:absent"])[0], "missing tag matched")
    require(len(p.list_tags([MARKER.upper()])[0]) == 1, "tag case control differs")
    clamped = p.expect("GET", "/api/engrams?" + urlencode({"vault": VAULT, "tags": MARKER, "limit": 999, "offset": 0}), role="observe")
    require(clamped["limit"] == 200 and len(clamped["engrams"]) == 200 and clamped["total"] == ACTIVE_COUNT, "list cap differs")
    report["active_marker_lookup"] = {"status": "PASS_quiescent_active_fixture", "tag": MARKER, "expected": ACTIVE_COUNT, "found": len(rows),
                                      "pages": pages, "small_page_count_including_empty": len(small_pages), "and_count": len(and_rows),
                                      "case_sensitive": True, "prefix_suffix_content_decoys_excluded": True, "cross_vault_excluded": True,
                                      "limit_clamped_to": clamped["limit"], "absence_proof_for_all_lifecycles": False}
    tools = p.rpc("tools/list")["result"]["tools"]
    names = {tool["name"] for tool in tools}
    required_tools = {"muninn_entity", "muninn_find_by_entity", "muninn_similar_entities"}
    require(required_tools <= names, "candidate tools unavailable")
    aggregate = p.tool("muninn_entity", {"vault": VAULT, "name": "Алексей", "limit": 20})
    by_entity = p.tool("muninn_find_by_entity", {"vault": VAULT, "entity_name": "Алексей", "limit": 20})
    similar = p.tool("muninn_similar_entities", {"vault": VAULT, "threshold": 0.5, "top_n": 20})
    require(len(aggregate["engrams"]) == 2 and by_entity["count"] == 2, "same-name native aggregate did not combine fixtures")
    require(similar["count"] > 0, "similar-entities positive fixture missing")
    blocked_tool = p.rpc("tools/call", {"name": "muninn_remember", "arguments": {"vault": VAULT, "content": "Synthetic forbidden MCP write"}})
    require(blocked_tool.get("error", {}).get("code") == -32001, "observe MCP write was not denied")
    mismatch = p.rpc("tools/call", {"name": "muninn_entity", "arguments": {"vault": OTHER_VAULT, "name": "Алексей"}})
    require(mismatch.get("error", {}).get("code") == -32602, "MCP key vault mismatch was not denied")
    report["native_candidates"] = {"status": "PASS_shortlist_only", "schemas": {tool["name"]: tool["inputSchema"] for tool in tools if tool["name"] in required_tools},
                                    "entity": aggregate, "find_by_entity": by_entity, "similar_entities": similar,
                                    "same_name_distinct_memories_share_native_entity": True, "observe_write_error_code": -32001, "vault_mismatch_error_code": -32602}
    archived_id, _ = p.create("Synthetic archived marker", "Synthetic archived marker lifecycle control.", ["ki_v1", MARKER])
    p.expect("PUT", f"/api/engrams/{archived_id}/state?vault={VAULT}", {"state": "archived"}, role="full")
    require(len(p.list_tags([MARKER])[0]) == ACTIVE_COUNT, "default list unexpectedly includes archived fixture")
    archived, _ = p.list_tags([MARKER], state="archived")
    require({row["id"] for row in archived} == {archived_id}, "explicit archived list missed fixture")
    deleted_ids = set()
    for index in range(DELETED_COUNT):
        deleted_id, _ = p.create(f"Synthetic deleted {index:03}", f"Synthetic deleted marker control {index:03}.", ["ki_v1", MARKER])
        p.expect("DELETE", f"/api/engrams/{deleted_id}?vault={VAULT}", role="full")
        deleted_ids.add(deleted_id)
    deleted = p.expect("GET", f"/api/deleted?vault={VAULT}&limit=9999", role="observe")
    deleted_offset = p.expect("GET", f"/api/deleted?vault={VAULT}&limit=9999&offset=100", role="observe")
    require(len(deleted["deleted"]) == 100 and deleted["count"] == 100, "deleted endpoint cap differs")
    returned_deleted = {row["id"] for row in deleted["deleted"]}
    require(returned_deleted < deleted_ids, "deleted cap did not omit known fixture")
    require(returned_deleted == {row["id"] for row in deleted_offset["deleted"]}, "deleted offset behavior changed")
    # v0.9.0 silently ignores the unrecognized soft_deleted state value.
    invalid_state_rows, _ = p.list_tags([MARKER], state="soft_deleted")
    require({row["id"] for row in invalid_state_rows} == expected_ids, "soft_deleted list semantics changed")
    hidden_id = next(iter(deleted_ids - returned_deleted))
    hidden = p.read(hidden_id)
    require(hidden["state"] == 127 and MARKER in hidden["tags"], "hidden tombstone not readable by known ID")
    report["exact_exhaustive_marker_lookup"] = {
        "status": "UNSUPPORTED", "active": ACTIVE_COUNT, "archived": 1, "known_soft_deleted": DELETED_COUNT,
        "deleted_list_returned": len(deleted["deleted"]), "deleted_count_is_returned_count": deleted["count"], "deleted_offset_ignored": True,
        "soft_deleted_filter_ignored": True, "hidden_tombstone_known_id_readable": True,
        "reason": "Default list excludes archived/deleted; archived filter works, soft_deleted filter is ignored. Deleted list caps at 100, ignores offset and has no complete total/cursor.",
        "adapter_capability": False, "unknown_write_recovery": "needs_manual_repair", "blind_post_retry": False,
        "concurrent_snapshot_completeness": "not established; offset pages have no snapshot token"}
    report.pop("fixture_marker_ids", None)
    report["plugins"] = p.expect("GET", "/api/admin/plugins", admin=True)
    require(report["plugins"] == [], "unexpected enrichment/embedding plugins")


def run(binary: Path, work_dir: Path, report):
    verify_binary(binary)
    # Check ambient config BEFORE --version too: main() loads it unconditionally.
    password, static_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="instance-", dir=work_dir) as directory:
        temp = Path(directory)
        env = isolated_environment(temp / "data", password, static_token)
        version = subprocess.run([str(binary), "--version"], env=env, check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        require(version == VERSION, "binary --version differs from pin")
        report["binary"] = {"version": version, "sha256": SHA256, "platform": "linux/amd64", "download": DOWNLOAD}
        report["isolation"] = {"fresh_temporary_storage": True, "loopback_only": True, "ambient_muninn_env": "absent_required",
                                "inherited_muninn_variables_removed": True, "local_embeddings": False, "enrichment": False,
                                "credentials": "random transient admin/static MCP/observe/full; never persisted in report", "post_retries": 0}
        holders, ports = [], {}
        try:
            for name in ("rest", "mcp", "ui", "mbp", "grpc"):
                holder = socket.socket()
                holder.bind(("127.0.0.1", 0))
                holders.append(holder)
                ports[name] = holder.getsockname()[1]
            command = [str(binary), "--daemon", "--data", str(temp / "data"), "--listen-host", "127.0.0.1"]
            for name, port in ports.items():
                command.extend([f"--{name}-addr", f"127.0.0.1:{port}"])
        finally:
            for holder in holders:
                holder.close()
        log_path = temp / "server.log"
        probe = Probe(ports, report, password, static_token)
        with log_path.open("wb") as log:
            process = subprocess.Popen(command, env=env, cwd=temp, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    require(process.poll() is None, "disposable daemon exited during startup")
                    logs = log_path.read_text(errors="replace")
                    # Confirm this child's successful bind messages before any
                    # authenticated requests; do not use an existing listener.
                    messages = {"rest": "rest: listening", "mcp": "mcp listening", "ui": "UI server listening", "mbp": "mbp: listening", "grpc": "grpc: listening"}
                    if all(f'msg="{message}" addr=127.0.0.1:{ports[name]}' in logs for name, message in messages.items()):
                        break
                    time.sleep(0.1)
                else:
                    raise SmokeFailure("disposable daemon did not bind its selected ports within 30 seconds")
                report["startup_disabled_plugins_log"] = {
                    "no_embedder": "no embedder configured, semantic similarity disabled" in logs,
                    "no_enricher": "no enrich plugin configured, LLM enrichment disabled" in logs}
                require(all(report["startup_disabled_plugins_log"].values()), "providers were not disabled")
                characterize(probe, report)
                require(process.poll() is None, "daemon exited before checks completed")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                report["daemon_stopped"] = process.poll() is not None
                report["http_calls"] = dict(sorted(probe.calls.items()))
                report["response_shapes"] = probe.examples
                report["log_tail"] = redact(log_path.read_text(errors="replace").splitlines()[-12:], probe.secrets())
    report["temporary_storage_removed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="existing pinned Linux x86-64 binary; default downloads into work/")
    parser.add_argument("--output", type=Path, default=ROOT / "work/muninn-spike/report.json")
    args = parser.parse_args()
    report = {"report_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "characterization": "FAIL"}
    exit_code = 1
    try:
        require(platform.system() == "Linux" and platform.machine().lower() in ("x86_64", "amd64"), "only Linux x86-64 is characterized")
        work_dir = ROOT / "work/muninn-spike"
        work_dir.mkdir(parents=True, exist_ok=True)
        binary = (args.binary or work_dir / "muninn-linux-amd64").resolve()
        if args.binary is None and not binary.exists():
            # Use inherited HTTP(S) proxy/CA settings with TLS verification.
            with tempfile.NamedTemporaryFile(dir=work_dir, delete=False) as target:
                downloaded = Path(target.name)
                try:
                    with urllib.request.urlopen(DOWNLOAD, timeout=120) as response:
                        while block := response.read(1024 * 1024):
                            target.write(block)
                except BaseException:
                    downloaded.unlink(missing_ok=True)
                    raise
            try:
                verify_binary(downloaded)
                downloaded.chmod(0o755)
                os.replace(downloaded, binary)
            finally:
                downloaded.unlink(missing_ok=True)
        run(binary, work_dir, report)
        report["characterization"] = "PASS_with_documented_limitations"
        exit_code = 0
    except (SmokeFailure, OSError, subprocess.SubprocessError) as error:
        # Messages from our assertions contain no credentials; do not serialize
        # arbitrary HTTP response bodies or child environment on failure.
        report["error"] = str(error) if isinstance(error, SmokeFailure) else type(error).__name__
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"characterization": report["characterization"], "exact_exhaustive_marker_lookup": report.get("exact_exhaustive_marker_lookup", {}).get("status", "UNVERIFIED"),
                      "unknown_write_recovery": "needs_manual_repair", "report": str(args.output)}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
