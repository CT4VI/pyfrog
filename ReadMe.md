# PyFrog for Mac - I just like frogs. I'm a frog guy.
PyFrog is a python library designed to make the process of exposing local servers to the internet through a local tunnel extremely easy. Like a magic key that opens doors between realms.

## Requirements
- Python 3.7+
- Internet access

## Installation
Run the following bash script in the folder of the project that you want to import pyfrog into
```
git clone https://github.com/CT4VI/pyfrog.git
```

## Usage
Due to GitHub's file size limit Node.js couldn't be packaged with the rest of the code. However, there is an automated installer here. As such, on your first run in a given project, PyFrog will install both Node.js and Local Tunnel under two directories - embedded_node, and localtunnel, respectively.

To get started with PyFrog, you first need to import the project and create a server. To do this, simply write

```
import pyfrog as pf
myservername = pf.WebServer(port=5000, subdomain="myserver", migrate_host=True, auto_hold=False)
```

or

```
from pyfrog import WebServer
myservername = WebServer(port=5000, subdomain="myserver", migrate_host=True, auto_hold=False)
```

This will create a variable named whatever you chose to call the server. You can customise the port and subdomain as you wish. The two tags `migrate_host` and `auto_hold` control how the server uptime works. `migrate_host`, when set to True, will cause the program to instead first check whether a server already exists at a given subdomain. If it does, then instead of trying to launch the server, it connects itself to an internal list on the host's end. Should the server go down on the other device, then your script will instead become the host. This allows for multiple devices to simultaneously support the public URL, and helps facilitate things like multiplayer connections in games by connecting clients to a given host using the public URL as a binding point. `auto_hold` will cause the server to remain alive functionally forever, and will keep your program running even when your code ends, instead of closing the server when you tell it to. For this reason it's not recommended to set this to True unles syou have a specific need for it. These flags are by default set to True, and False, respectively.

After setting up your server, you then define its behaviour on request, as follows

```
@myservername.on_get
def myfunction(data, request_obj):
  Put whatever you want your code to do on a get request here

@myservername.on_post
def myotherfunction(data, request_obj):
  Put whatever you want your code to do on a post request here
```

`data` is the JSON data sent with the request, whilst `request_obj` is lower level data. If you don't need this extra data, simply replace it with `request_obj=None` in the function.

Should you wish to make use of custom routes, like https://myserver.loca.lt/echo, then you will need to add these behaviours in a similar way, such as

```
@server.route("/myroute", methods=["GET"])
def mycustomroute(data, request_obj):
  Put whatever you want your code to do on request to your custom route here
```

or

```
@server.on_request(path="/myotherroute", methods=["POST"])
def myothercustomroute(data, request_obj):
  Put whatever you want your code to do on request to your custom route here
```

Once you're happy with your behaviours, you may start your server with `myservername.start()`. This will put your code live at https://myserver.loca.lt. After your server has started, you can run anything you want and it will execute in parallel with maintaining the public URL. When you wish to close your server, simply execute `myservername.stop()`.

## Methods
- `server.start()`: Launches your custom server
- `server.stop()`: Closes your server
- `server.print_status()`: Returns whether your computer is the host of the URL you are after and the list of peers waiting to take over as host
- `server.is_host()`: A boolean reflecting if you're the host of the server
- `server.get_peers()`: A list of the current peers trying to reach the server
- `server.tunnel_url()`: The current URL of the local tunnel
