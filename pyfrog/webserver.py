import threading
import subprocess
import signal
import atexit
import os
import shlex
import time
import platform
import requests
from waitress import serve
from functools import wraps
from flask import Flask, request, jsonify

CURRENT_OS = platform.system()

class WebServer:
    def __init__(self, port=5000, subdomain="myserver", auto_hold=True, migrate_host=True):
        self.port = port
        self.subdomain = subdomain
        self.auto_hold = auto_hold
        self.migrate_host = migrate_host
        self._tunnel_pid = None
        self._tunnel_url = None
        self._server_thread = None
        self._hold_thread = None
        self._watcher_thread = None
        self._log_requests = True
        self._is_host = False
        self._log_file = "server_requests.log"
        self._role_check_interval = 10  # seconds
        self._peer_expiry = 30  # seconds until a peer is considered stale
        self._get_hooks = []
        self._post_hooks = []
        self._any_hooks = []
        self._custom_routes = []
        self._peer_list = {}  # { ip: last_seen_timestamp }
        self._base_path = os.path.dirname(os.path.abspath(__file__))

        self.app = Flask(__name__)

        atexit.register(self.stop)
        signal.signal(signal.SIGTERM, lambda sig, frame: self.stop())
        signal.signal(signal.SIGINT, lambda sig, frame: self.stop())

    def _setup_routes(self):
        @self.app.route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
        def default_route():
            method = request.method
            data = self._extract_request_data()

            # Logging
            if self._log_requests:
                with open(self._log_file, "a") as f:
                    f.write(f"[{time.ctime()}] {method} {request.path}\n")
                    f.write(f"Headers: {dict(request.headers)}\n")
                    f.write(f"Data: {data}\n")

            try:
                if method == "GET":
                    for hook in self._get_hooks:
                        response = jsonify(hook(data, request_obj=request))
                        break
                    else:
                        response = "No GET handler", 400

                elif method == "POST":
                    for hook in self._post_hooks:
                        response = jsonify(hook(data, request_obj=request))
                        break
                    else:
                        response = "No POST handler", 400

                else:
                    for methods, hook in self._any_hooks:
                        if method in methods:
                            response = jsonify(hook(data, request_obj=request))
                            break
                    else:
                        response = f"Method {method} not supported here", 405

            except Exception as e:
                response = {"error": str(e)}, 500

            if self._log_requests:
                with open(self._log_file, "a") as f:
                    if isinstance(response, tuple):
                        status = response[1]
                    else:
                        status = 200
                    f.write(f"Response status: {status}\n\n")

            # print("Headers:", dict(request.headers))

            return response

        for path, methods, func in self._custom_routes:
            @wraps(func)
            def handler(*args, _func=func, **kwargs):
                result = _func(*args, **kwargs)

                # If user returns dict, jsonify it. Otherwise return as-is (HTML, text, etc.)
                if isinstance(result, dict):
                    return jsonify(result)
                else:
                    return result

            self.app.route(path, methods=methods)(handler)

        for path, methods, func in self._any_hooks:
            @wraps(func)
            def route_wrapper(*args, _func=func, **kwargs):
                data = self._extract_request_data()

                if self._log_requests:
                    with open(self._log_file, "a") as f:
                        f.write(f"[{time.ctime()}] {request.method} {request.path}\n")
                        f.write(f"Headers: {dict(request.headers)}\n")
                        f.write(f"Data: {data}\n")

                try:
                    result = jsonify(_func(data, request_obj=request))
                    status = 200
                except Exception as e:
                    result = jsonify({"error": str(e)})
                    status = 500

                if self._log_requests:
                    with open(self._log_file, "a") as f:
                        f.write(f"Response status: {status}\n\n")

                return result, status

            self.app.route(path, methods=methods)(route_wrapper)

        @self.on_get
        def status(data, request_obj=None):
            self._prune_stale_peers()
            return {"status": "online", "peers": list(self._peer_list)}

        @self.on_request(path="/register", methods=["POST"])
        def register_peer(data, request_obj=None):
            ip = data.get("ip") or request_obj.remote_addr
            now = time.time()

            self._peer_list[ip] = now

            # Clean out stale peers
            self._prune_stale_peers(now)

            return {"peers": list(self._peer_list)}

    def _prune_stale_peers(self, now=None):
        now = now or time.time()
        expired = []

        for ip, last_seen in self._peer_list.items():
            if now - last_seen > self._peer_expiry:
                expired.append(ip)

        for ip in expired:
            del self._peer_list[ip]

    def route(self, path, methods=["GET"]):
        def decorator(func):
            self._custom_routes.append((path, methods, func))
            return func
        return decorator

    def on_get(self, func):
        self._get_hooks.append(func)
        return func

    def on_post(self, func):
        self._post_hooks.append(func)
        return func

    def on_request(self, path="/", methods=["PUT"]):
        def decorator(func):
            self._any_hooks.append((path, methods, func))
            return func

        return decorator

    def _wait_for_port(self, timeout=5):
        import socket
        start = time.time()
        while time.time() - start < timeout:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return True
            time.sleep(0.1)
        return False

    def start(self):
        if self.migrate_host:
            role = self._auto_host_or_join()
            if role == "client":
                # print(f"[INFO] Tunnel is already up. This device joined as a client.")
                self._start_watcher_thread()
                return
            else:
                # print(f"[INFO] This device has become the host.")
                self._is_host = True

        self._setup_routes()

        self._server_thread = threading.Thread(
            target=lambda: serve(self.app, host="127.0.0.1", port=self.port),
            daemon=True
        )
        self._server_thread.start()

        # Wait for Flask to bind
        if self._wait_for_port():
            self._start_tunnel()
        else:
            print("[ERROR] Failed to start tunnel: Flask server did not become ready.")
            return

        self._start_tunnel()

        if self.auto_hold:
            self._hold_thread = threading.Thread(target=self._hold_forever)
            self._hold_thread.start()

    def _start_watcher_thread(self):
        if self._watcher_thread and self._watcher_thread.is_alive():
            return  # Already running

        self._watcher_thread = threading.Thread(target=self._monitor_host, daemon=True)
        self._watcher_thread.start()

    def _monitor_host(self):
        while not self._is_host:
            try:
                local_ip = self._get_local_ip()
                requests.post(f"https://{self.subdomain}.loca.lt/register", json={"ip": local_ip}, timeout=2)
                res = requests.get(f"https://{self.subdomain}.loca.lt/status", timeout=2)

                if res.status_code != 200:
                    raise Exception("Non-200 response")
            except:
                # print(f"[INFO] Host appears to be down — this device will now host.")
                self._is_host = True
                self.start()
                break
            time.sleep(self._role_check_interval)

    def is_host(self):
        return self._is_host

    def _hold_forever(self):
        try:
            while True:
                signal.pause()
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        if self._tunnel_pid:
            try:
                os.kill(self._tunnel_pid, signal.SIGTERM)
            except Exception:
                pass
        self._tunnel_pid = None

    def _start_tunnel(self):
        node_path = os.path.join(self._base_path, "embedded_node/bin/node")
        lt_script = os.path.join(self._base_path, "localtunnel/node_modules/localtunnel/bin/lt.js")
        cmd = [node_path, lt_script, "--port", str(self.port), "--subdomain", self.subdomain, "--print-requests"]

        if CURRENT_OS == "Darwin":
            self._start_tunnel_mac(cmd)
        elif CURRENT_OS == "Windows":
            self._start_tunnel_windows(cmd)
        elif CURRENT_OS == "Linux":
            self._start_tunnel_linux(cmd)
        else:
            pass
            # print("Unsupported OS for LocalTunnel auto-launch.")

        self._tunnel_url = f"https://{self.subdomain}.loca.lt"

    def _start_tunnel_mac(self, cmd):
        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        applescript = f'''
        tell application "Terminal"
            set windowExists to false
            repeat with w in windows
                if name of w contains "Tunnel" then
                    set windowExists to true
                    do script "{quoted_cmd}" in w
                    exit repeat
                end if
            end repeat
            if not windowExists then
                set t to do script "{quoted_cmd}"
                set custom title of front window to "Tunnel"
            end if
        end tell
        '''
        subprocess.run(["osascript", "-e", applescript])

    def _start_tunnel_windows(self, cmd):
        cmd_line = " ".join(f'"{c}"' for c in cmd)
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "cmd.exe", "/k", cmd_line],
            shell=True
        )

    def _start_tunnel_linux(self, cmd):
        import shutil
        cmd_line = " ".join(shlex.quote(c) for c in cmd)

        # Check for terminal emulator
        terminal = None
        for term in ["x-terminal-emulator", "xterm", "gnome-terminal", "konsole"]:
            if shutil.which(term):
                terminal = term
                break

        if terminal:
            subprocess.Popen([terminal, "-e", cmd_line])
        else:
            # Fallback: run in background
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _extract_request_data(self):
        result = {}

        if request.method == "GET":
            result.update(request.args.to_dict())
            result["query"] = request.query_string.decode()

        else:
            content_type = request.content_type or ""
            if "application/json" in content_type:
                result.update(request.get_json(force=True, silent=True) or {})
            elif "application/x-www-form-urlencoded" in content_type:
                result.update(request.form.to_dict())
            elif "multipart/form-data" in content_type:
                result.update(request.form.to_dict(flat=True))
            elif "text/plain" in content_type:
                result["text"] = request.get_data(as_text=True)
            else:
                result["raw"] = request.get_data()

        headers = dict(request.headers)
        result["headers"] = headers

        # Extract client IP (prefer public IP from headers)
        result["ip"] = headers.get("X-Forwarded-For", request.remote_addr)

        return result

    def _auto_host_or_join(self):
        try:
            response = requests.get(f"https://{self.subdomain}.loca.lt/status", timeout=2)
            if response.status_code == 200:
                # Server is already up, try to register as peer
                local_ip = self._get_local_ip()
                requests.post(f"https://{self.subdomain}.loca.lt/register", json={"ip": local_ip})
                return "client"
        except:
            # Tunnel is unreachable, we will host it
            return "host"

    def _get_local_ip(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except:
            return "127.0.0.1"
        finally:
            s.close()

    def get_peers(self):
        self._prune_stale_peers()
        return list(self._peer_list.keys())

    def print_status(self):
        role = "Host" if self._is_host else "Queued client"
        print(f"[{role}] Active devices: {', '.join(self.get_peers())}")

    @property
    def tunnel_url(self):
        return self._tunnel_url
