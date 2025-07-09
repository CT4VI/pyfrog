import pyfrog

server = pyfrog.WebServer(port=5000, subdomain="myserver", migrate_host=True, auto_hold=False)

# When set to True, migrate_host will cause the server to both accept and join queues to host domains
# auto_hold will keep the server running endlessly in the background. Setting this to false will require you to keep the program alive

@server.on_get # Standard get request
def home(data, request_obj):
    return {
        "message": "Welcome to my tunnel!",
        "ip": request_obj.remote_addr
    }

@server.on_post # Standard post request
def receive_data(data, request_obj=None):
    print("[POST] Data received:", data)
    return {"status": "received", "data": data}

@server.route("/echo", methods=["GET"]) # Custom route with HTML response and IP logging
def serve_html():
    return """
    <html>
        <head><title>IP Echo</title></head>
        <body>
            <h2>Checking your public IP...</h2>
            <script>
                fetch("https://api.ipify.org?format=json")
                  .then(res => res.json())
                  .then(data => {
                    document.body.innerHTML += "<p>Your public IP is: <strong>" + data.ip + "</strong></p>";
                    fetch("/log-ip", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ publicip: data.ip })
                    });
                  });
            </script>
        </body>
    </html>
    """, 200, {"Content-Type": "text/html"}

@server.on_request(path="/log-ip", methods=["POST"]) # Handling of IP logging
def log_public_ip(data, request_obj=None):
    ip = data.get("publicip") or data["publicip"]
    print(f"Public IP reported by client: {ip}")
    return {"logged": ip}

@server.on_request(path="/upload", methods=["PUT"]) # Uploading demonstration
def handle_upload(data, request_obj=None):
    print(f"[PUT] Upload data from {data['ip']}: {data}")
    return {"status": "uploaded"}

server.start()

# Gives the server a moment to connect

server.print_status()

if server.is_host(): # Determines if this device is currently the one hosting the server, or if it's in the queue
    print("This device is the HOST.")
else:
    print("This device is in the queue, waiting to become host.")

print("Connected peer IPs:", server.get_peers()) # Lists all IPs queueing to be the host
print("Tunnel is live at:", server.tunnel_url) # Display the current URL

input("Press ENTER to close the tunnel...\n")
server.stop()
