"""Start the single local Web platform and open it when the server is ready."""

import os
import threading
import time
import webbrowser

import uvicorn


def open_when_ready(server: uvicorn.Server) -> None:
    while not server.started and not server.should_exit:
        time.sleep(0.1)
    if server.started:
        port = server.servers[0].sockets[0].getsockname()[1]
        address = f"http://127.0.0.1:{port}/"
        print(f"Platform address: {address}", flush=True)
        if os.environ.get("BOHAI_OPEN_BROWSER") != "0":
            webbrowser.open(address, new=2)


if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config("src.server:app", host="127.0.0.1", port=0))
    threading.Thread(target=open_when_ready, args=(server,), daemon=True).start()
    server.run()
