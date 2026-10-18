#!/usr/bin/env python3
"""
zwave_mgr.py - Z-Wave JS Controller CLI Manager for Zero.
Handles inclusion, exclusion, and node status queries directly over the Z-Wave JS WebSocket API.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
import aiohttp

STATUS_FILE = Path("/workspace/data/zwave_status.json")
import nas_config
WS_URL = os.environ.get("ZWAVE_WS_URL", f"ws://{nas_config.HOST_1_IP}:3000")

def update_status(data: dict):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE) as f:
                current = json.load(f)
        except Exception:
            pass
    current.update(data)
    current["updated_at"] = time.strftime("%Y-%m-%d %I:%M:%S %p PT")
    tmp = STATUS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(current, f, indent=2)
    tmp.replace(STATUS_FILE)

async def run_exclusion(timeout_sec: int = 300):
    print(f"[ZWaveMgr] Connecting to {WS_URL} to begin exclusion (timeout {timeout_sec}s)...")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            await ws.receive_json() # version
            await ws.send_json({"messageId": "init", "command": "start_listening"})
            await ws.receive_json()

            # Begin exclusion
            await ws.send_json({"messageId": "excl", "command": "controller.begin_exclusion"})
            res = await ws.receive_json()
            if not res.get("success"):
                print(f"[ZWaveMgr] Error starting exclusion: {res}")
                update_status({"mode": "idle", "last_error": str(res)})
                return False

            print("[ZWaveMgr] ✅ Exclusion Mode is ACTIVE on Zooz ZAC93 controller.")
            update_status({"mode": "exclusion", "status": "active", "started_at": time.time()})

            start_t = time.time()
            while time.time() - start_t < timeout_sec:
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

                event_data = msg.get("event", {})
                event_name = event_data.get("event")
                source = event_data.get("source")

                if source == "controller":
                    if event_name == "exclusion started":
                        print("[ZWaveMgr] Event: Exclusion confirmed started.")
                    elif event_name == "exclusion stopped":
                        print("[ZWaveMgr] Event: Exclusion stopped.")
                        update_status({"mode": "idle", "status": "exclusion_stopped"})
                        return True
                    elif event_name == "node removed":
                        node = event_data.get("node", {})
                        print(f"[ZWaveMgr] 🎯 NODE EXCLUDED: Node ID {node.get('nodeId')}")
                        update_status({
                            "mode": "idle",
                            "status": "node_excluded",
                            "last_excluded_node": node.get("nodeId"),
                            "excluded_node_details": node
                        })
                        return True

            print("[ZWaveMgr] Timeout reached. Stopping exclusion...")
            await ws.send_json({"messageId": "stop", "command": "controller.stop_exclusion"})
            update_status({"mode": "idle", "status": "timeout"})
            return False

async def run_inclusion(timeout_sec: int = 300):
    print(f"[ZWaveMgr] Connecting to {WS_URL} to begin inclusion (timeout {timeout_sec}s)...")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            await ws.receive_json()
            await ws.send_json({"messageId": "init", "command": "start_listening"})
            await ws.receive_json()

            # Begin inclusion (strategy: 0 = Default)
            await ws.send_json({
                "messageId": "inc",
                "command": "controller.begin_inclusion",
                "options": {"strategy": 0}
            })
            res = await ws.receive_json()
            if not res.get("success"):
                print(f"[ZWaveMgr] Error starting inclusion: {res}")
                update_status({"mode": "idle", "last_error": str(res)})
                return False

            print("[ZWaveMgr] ✅ Inclusion Mode is ACTIVE on Zooz ZAC93 controller.")
            update_status({"mode": "inclusion", "status": "active", "started_at": time.time()})

            start_t = time.time()
            while time.time() - start_t < timeout_sec:
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

                event_data = msg.get("event", {})
                event_name = event_data.get("event")
                source = event_data.get("source")

                if source == "controller":
                    if event_name == "node added":
                        node = event_data.get("node", {})
                        print(f"[ZWaveMgr] 🎉 NODE ADDED: Node ID {node.get('nodeId')}")
                        update_status({
                            "mode": "idle",
                            "status": "node_added",
                            "last_added_node": node.get("nodeId"),
                            "added_node_details": node
                        })
                        return True
                    elif event_name == "inclusion stopped":
                        print("[ZWaveMgr] Event: Inclusion stopped.")
                        update_status({"mode": "idle", "status": "inclusion_stopped"})
                        return True

            print("[ZWaveMgr] Timeout reached. Stopping inclusion...")
            await ws.send_json({"messageId": "stop", "command": "controller.stop_inclusion"})
            update_status({"mode": "idle", "status": "timeout"})
            return False

async def stop_process():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            await ws.receive_json()
            await ws.send_json({"messageId": "init", "command": "start_listening"})
            await ws.receive_json()
            await ws.send_json({"messageId": "stop_ex", "command": "controller.stop_exclusion"})
            await ws.send_json({"messageId": "stop_inc", "command": "controller.stop_inclusion"})
            print("[ZWaveMgr] Stopped any active inclusion/exclusion.")
            update_status({"mode": "idle", "status": "stopped"})

async def get_nodes():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            await ws.receive_json()
            await ws.send_json({"messageId": "init", "command": "start_listening"})
            res = await ws.receive_json()
            nodes = res.get("result", {}).get("state", {}).get("nodes", [])
            print(f"[ZWaveMgr] Total nodes in network: {len(nodes)}")
            for n in nodes:
                print(f"  Node {n.get('nodeId')}: status={n.get('status')}, ready={n.get('ready')}, device={n.get('deviceConfig', {}).get('description')}")

def main():
    parser = argparse.ArgumentParser(description="Z-Wave JS Manager CLI")
    parser.add_argument("action", choices=["exclude", "include", "stop", "nodes", "status"])
    parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds")
    args = parser.parse_args()

    if args.action == "exclude":
        asyncio.run(run_exclusion(timeout_sec=args.timeout))
    elif args.action == "include":
        asyncio.run(run_inclusion(timeout_sec=args.timeout))
    elif args.action == "stop":
        asyncio.run(stop_process())
    elif args.action == "nodes":
        asyncio.run(get_nodes())
    elif args.action == "status":
        if STATUS_FILE.exists():
            print(STATUS_FILE.read_text())
        else:
            print("No status file.")

if __name__ == "__main__":
    main()
