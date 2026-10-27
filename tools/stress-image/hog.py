"""Worst-case player instance for crash tests: holds MEM_MB of RAM, burns CPU, answers HTTP on :80."""
import http.server
import os
import threading

MEM_MB = int(os.environ.get("MEM_MB", "230"))   # just under the default 256 MB cap
BURN = os.environ.get("BURN_CPU", "1") == "1"   # use its whole CPU share

buf = bytearray(MEM_MB * 1024 * 1024)
for i in range(0, len(buf), 4096):               # touch every page so the RAM is really used
    buf[i] = 1

def burn():
    x = 0
    while True:
        x = (x * 31 + 7) % 1000003

if BURN:
    threading.Thread(target=burn, daemon=True).start()

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"stress instance: holding %d MB\n" % MEM_MB)
    def log_message(self, *a):
        pass

http.server.ThreadingHTTPServer(("", 80), H).serve_forever()
