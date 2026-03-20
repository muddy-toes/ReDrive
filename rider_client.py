"""rider_client.py — ReDrive rider bridge.

Connects your local machine (which has a ReStim device attached) to a ReDrive
relay room on the cloud server.  The server sends T-code commands; this script
forwards them to the local ReStim WebSocket.

Usage:
    python rider_client.py <ROOM_CODE> [--server wss://redrive.estimstation.com] [--restim ws://localhost:12346]

Requires: pip install aiohttp websockets (or just aiohttp)
"""

import argparse
import asyncio
import sys

try:
    import aiohttp
except ImportError:
    print("aiohttp not installed.  Run:  pip install aiohttp")
    sys.exit(1)

DEFAULT_SERVER = "wss://redrive.estimstation.com"
DEFAULT_RESTIM = "ws://localhost:12346"


async def run(room_code: str, server_url: str, restim_url: str):
    relay_ws_url = f"{server_url.rstrip('/')}/room/{room_code}/rider-ws"
    print(f"ReDrive rider client")
    print(f"  Room:   {room_code}")
    print(f"  Relay:  {relay_ws_url}")
    print(f"  ReStim: {restim_url}")
    print()

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                print("Connecting to relay…")
                async with session.ws_connect(relay_ws_url) as relay:
                    print("Connected to relay.  Connecting to ReStim…")
                    async with session.ws_connect(restim_url) as restim:
                        print("Connected to ReStim.  Forwarding T-code.\n")

                        async for msg in relay:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                if msg.data.startswith('{'):
                                    # JSON control message (driver_status, bottle_status, etc.) - skip
                                    continue
                                try:
                                    await restim.send_str(msg.data)
                                except Exception as e:
                                    print(f"ReStim send error: {e}")
                                    break
                            elif msg.type in (aiohttp.WSMsgType.ERROR,
                                             aiohttp.WSMsgType.CLOSE):
                                break

                        print("Relay connection closed.")

        except aiohttp.ClientConnectorError as e:
            print(f"Connection error: {e}")
        except Exception as e:
            print(f"Error: {e}")

        print("Reconnecting in 5 s…")
        await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="ReDrive rider bridge")
    parser.add_argument("room_code",
                        help="10-character room code from the driver")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"Relay server WebSocket URL (default: {DEFAULT_SERVER})")
    parser.add_argument("--restim", default=DEFAULT_RESTIM,
                        help=f"Local ReStim WebSocket URL (default: {DEFAULT_RESTIM})")
    args = parser.parse_args()

    code = args.room_code.strip().upper()
    if len(code) != 10:
        print(f"Error: room code must be 10 characters, got {len(code)}")
        sys.exit(1)

    asyncio.run(run(code, args.server, args.restim))


if __name__ == "__main__":
    main()
