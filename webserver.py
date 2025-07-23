import threading
import subprocess
import signal
import atexit
import os
import shlex
import time
import platform
import requests
import ssl
import shutil
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
        self._cert_file = os.path.join(self._base_path, "certs", "cacert.pem")

        self.app = Flask(__name__)

        atexit.register(self.stop)
        signal.signal(signal.SIGTERM, lambda sig, frame: self.stop())
        signal.signal(signal.SIGINT, lambda sig, frame: self.stop())

    def start(self):
        # Kill any existing tunnel processes
        if hasattr(self, "_tunnel_process") and self._tunnel_process:
            self._tunnel_process.terminate()
            self._tunnel_process = None

        # If migrate_host is true, check whether the subdomain is already live and depending on the response, start the watcher thread
        if self.migrate_host:
            role = self._auto_host_or_join()
            if role == "client":
                self._start_watcher_thread()
                return
            else:
                self._is_host = True

        # Inject internal system routes just like user-defined ones
        self._any_hooks.append((
            "/status",
            ["GET"],
            lambda data, request_obj=None: {
                "status": "online",
                "peers": list(self._peer_list)
            }
        ))

        self._any_hooks.append((
            "/register",
            ["POST"],
            self._create_register_hook()
        ))

        self._setup_routes()

        self._server_thread = threading.Thread(
            target=lambda: serve(self.app, host="0.0.0.0", port=self.port),
            daemon=True
        )
        self._server_thread.start()

        # Wait for Flask to bind
        if self._wait_for_port():
            self._start_tunnel()
        else:
            print("[ERROR] Failed to start tunnel: Flask server did not become ready.")
            return



        if self.auto_hold:
            self._hold_thread = threading.Thread(target=self._hold_forever, daemon=True)
            self._hold_thread.start()

    def _create_register_hook(self):
        def register(data, request_obj=None):
            ip = data.get("ip") or (request_obj.remote_addr if request_obj else "unknown")
            self._peer_list[ip] = time.time()
            self._prune_stale_peers()
            return {"peers": list(self._peer_list)}

        return register

    def _auto_host_or_join(self):
        try: # Ping the server subdomain. If a response is received, send the ip to the registration route and return client status
            response = requests.get(f"https://{self.subdomain}.loca.lt/status", timeout=2)
            if response.status_code == 200:
                local_ip = self._get_local_ip()
                requests.post(f"https://{self.subdomain}.loca.lt/register", json={"ip": local_ip})
                return "client"
        except: # If no response is received, subdomain isn't up. Return host status
            return "host"

    def _get_local_ip(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try: # Connect to public Google DNS. From this connection, extract the IP
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except: # If no IP is found, default to the local host
            return "127.0.0.1"
        finally: # Once finished, close the Google DNS
            s.close()

    def _start_watcher_thread(self):
        # If the watcher thread is already live, skip the function
        if self._watcher_thread and self._watcher_thread.is_alive():
            return

        # If it isn't, begin monitoring the host
        self._watcher_thread = threading.Thread(target=self._monitor_host, daemon=True)
        self._watcher_thread.start()

    def _monitor_host(self):
        while not self._is_host:
            try: # If not the host, ping the server over and over
                local_ip = self._get_local_ip()
                requests.post(f"https://{self.subdomain}.loca.lt/register", json={"ip": local_ip}, timeout=2)
                res = requests.get(f"https://{self.subdomain}.loca.lt/status", timeout=2)

                if res.status_code != 200:
                    raise Exception("Non-200 response")
            except: # If the ping fails, then retry starting the server
                self._is_host = True
                self.start()
                break
            time.sleep(self._role_check_interval) # Wait a duration determined by the role check interval before checking again

    def _setup_routes(self):
        @self.app.route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]) # For all actions happening not defined in the user script
        def default_route():
            # Record the method of the request and all information regarding it
            method = request.method
            data = self._extract_request_data()

            # To a given log file, record all details of the request
            if self._log_requests:
                with open(self._log_file, "a") as f:
                    f.write(f"[{time.ctime()}] {method} {request.path}\n")
                    f.write(f"Headers: {dict(request.headers)}\n")
                    f.write(f"Data: {data}\n")

            # Attempt to tell the user that no route is supported for their request
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

            # Throw an error 500 if the server couldn't communicate with the requester
            except Exception as e:
                response = {"error": str(e)}, 500

            # Attempt to append the status of the response to the log file
            if self._log_requests:
                with open(self._log_file, "a") as f:
                    if isinstance(response, tuple):
                        status = response[1]
                    else:
                        status = 200
                    f.write(f"Response status: {status}\n\n")

            return response

        # Flag and redirect if the request comes through on a custom route
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

    def _extract_request_data(self):
        result = {} # Create a dictionary to store request data

        # Decode all data from the request
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

        # Add headers from the request to the stored data
        headers = dict(request.headers)
        result["headers"] = headers

        # Add client IP to the stored data
        result["ip"] = headers.get("X-Forwarded-For", request.remote_addr)

        return result

    def _ensure_certificates(self):
        if CURRENT_OS == "Darwin":
            cert_script = "/Applications/Python 3.12/Install Certificates.command"
            if os.path.exists(cert_script):
                subprocess.run(["open", cert_script])
                time.sleep(2)  # Give it a second to complete

    def _download_with_cert_bundle(self, url, output_path):
        import requests

        # Use bundled certificate for validation
        with requests.get(url, stream=True, verify=self._cert_file) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

    def _ensure_node_available(self):
        import tarfile, urllib.request, os

        embedded_dir = os.path.join(self._base_path, "embedded_node")
        node_path = os.path.join(embedded_dir, "bin", "node")

        if os.path.exists(node_path):
            return node_path

        print("[INFO] Node.js not found, downloading...")
        os.makedirs(embedded_dir, exist_ok=True)

        url = self._get_nodejs_download_url()
        archive_path = os.path.join(self._base_path, "node.tar.gz")

        import requests
        import certifi

        print(f"[INFO] Downloading Node.js from {url}")
        with requests.get(url, stream=True, verify=certifi.where()) as res:
            res.raise_for_status()
            with open(archive_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    f.write(chunk)

        print("[INFO] Extracting Node.js...")
        if archive_path.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                zip_ref.extractall(path=embedded_dir)
        else:
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=embedded_dir, members=self._strip_components(tar, 1))
        os.remove(archive_path)

        return node_path

    def _get_nodejs_download_url(self):
        version = "v20.11.1"
        base_url = f"https://nodejs.org/dist/{version}/"

        arch = platform.machine()
        if arch in ("x86_64", "AMD64"):
            arch = "x64"
        elif arch in ("arm64", "aarch64"):
            arch = "arm64"

        if CURRENT_OS == "Darwin":
            return base_url + f"node-{version}-darwin-{arch}.tar.gz"
        elif CURRENT_OS == "Linux":
            return base_url + f"node-{version}-linux-{arch}.tar.gz"
        elif CURRENT_OS == "Windows":
            return base_url + f"node-{version}-win-{arch}.zip"
        else:
            raise OSError("Unsupported OS for Node.js auto-installation")

    def _strip_components(self, tar, n):
        for member in tar.getmembers():
            path_parts = member.name.split('/')
            member.name = '/'.join(path_parts[n:])
            yield member

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

    def is_host(self):
        return self._is_host

    def _hold_forever(self):
        try:
            while True:
                signal.pause()
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        print("[INFO] Stopping PyFrog WebServer...")
        
        # Close tunnel windows on Mac
        self._close_mac_tunnel_windows()
        
        # Kill tunnel process if running
        if self._tunnel_pid:
            try:
                print(f"[INFO] Terminating tunnel process {self._tunnel_pid}")
                os.kill(self._tunnel_pid, signal.SIGTERM)
            except Exception as e:
                print(f"[WARNING] Could not kill tunnel process: {e}")
        self._tunnel_pid = None
        
        # Kill any remaining localtunnel processes
        if CURRENT_OS == "Darwin":
            subprocess.run(["pkill", "-f", "lt.js"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        print("[INFO] PyFrog WebServer stopped.")

    def _start_tunnel(self):
        if hasattr(self, "_tunnel_process") and self._tunnel_process:
            return

        import subprocess

        node_path = self._ensure_node_available()
        lt_script = self._setup_localtunnel(node_path)

        cmd = [
            node_path,
            lt_script,
            "--port", str(self.port),
            "--subdomain", self.subdomain,
            "--local-host", "127.0.0.1"
        ]

        if CURRENT_OS == "Darwin":
            self._start_tunnel_mac(cmd)
        elif CURRENT_OS == "Windows":
            self._start_tunnel_windows(cmd)
        elif CURRENT_OS == "Linux":
            self._start_tunnel_linux(cmd)
        else:
            raise OSError("Unsupported OS")

        self._tunnel_url = f"https://{self.subdomain}.loca.lt"

    def _setup_localtunnel(self, node_path):
        import os
        import subprocess

        lt_dir = os.path.join(self._base_path, "localtunnel")
        os.makedirs(lt_dir, exist_ok=True)

        npm_path = os.path.join(os.path.dirname(node_path), "npm")
        package_json = os.path.join(lt_dir, "package.json")

        if not os.path.exists(package_json):
            print("[INFO] Initializing localtunnel directory...")
            subprocess.run([node_path, npm_path, "init", "-y"], cwd=lt_dir)

        lt_module = os.path.join(lt_dir, "node_modules", "localtunnel")
        if not os.path.exists(lt_module):
            print("[INFO] Installing localtunnel...")
            subprocess.run([node_path, npm_path, "install", "localtunnel"], cwd=lt_dir)

        lt_bin = os.path.join(lt_module, "bin", "lt.js")
        if CURRENT_OS == "Windows":
            lt_bin = os.path.join(lt_module, "bin", "lt.cmd")  # fallback or adjustment if needed
        return lt_bin

    def _close_mac_tunnel_windows(self):
        script = '''
        tell application "Terminal"
            repeat with w in windows
                try
                    if custom title of w contains "PyFrogTunnel" then
                        close w
                    end if
                end try
            end repeat
        end tell
        '''
        subprocess.run(["osascript", "-e", script])

    def _start_tunnel_mac(self, cmd):
        subprocess.run(["defaults", "write", "com.apple.Terminal", "NSQuitAlwaysKeepsWindows", "-bool", "false"])
        self._close_mac_tunnel_windows()
        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        applescript = f'''
            tell application "Terminal"
                set found to false
                repeat with w in windows
                    if name of w contains "Tunnel" then
                        set found to true
                        tell w
                            do script "echo Killing any previous tunnel...; pkill -f 'lt.js'; sleep 1; clear; {quoted_cmd}" in selected tab
                        end tell
                        exit repeat
                    end if
                end repeat

                if not found then
                    set t to do script "echo Launching fresh tunnel...; pkill -f 'lt.js'; sleep 1; clear; {quoted_cmd}"
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

    def get_peers(self):
        self._prune_stale_peers()
        return list(self._peer_list.keys())

    def print_status(self):
        role = "Host" if self._is_host else "Queued client"
        print(f"[{role}] Active devices: {', '.join(self.get_peers())}")

    @property
    def tunnel_url(self):
        return self._tunnel_url
