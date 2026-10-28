from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import jinja2

original_get_template = jinja2.Environment.get_template

def debug_get_template(self, name, parent=None, globals=None):
    print("get_template called: name=" + name)
    if globals:
        print("  globals keys: " + str(list(globals.keys())))
        for k, v in globals.items():
            print("  " + k + ": " + str(type(v)))
    else:
        print("  self.globals keys: " + str(list(self.globals.keys())))
    return original_get_template(self, name, parent, globals)

jinja2.Environment.get_template = debug_get_template

templates = Jinja2Templates(directory="templates")

scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [], "client": ("127.0.0.1", 12345), "server": ("localhost", 8000)}
request = Request(scope)

try:
    response = templates.TemplateResponse(request, "home.html", {
        "request": request,
        "washers": [],
        "dryers": [],
        "stats": {"available": 0, "in_use": 0, "total_machines": 0},
        "now": "",
        "soon": ""
    })
    print("TemplateResponse OK")
except Exception as e:
    print("Error: " + str(e))
    import traceback
    traceback.print_exc()