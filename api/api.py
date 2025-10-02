
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import asyncio

def create_api_server(bot, loop):
    class APIHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == '/api/shutup':
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                data = json.loads(post_data)
                duration = data.get('duration', 0)

                asyncio.run_coroutine_threadsafe(bot.mute_for(duration), loop)

                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Bot muted')
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Not found')

    def run_server():
        server_address = ('', 31335)
        httpd = HTTPServer(server_address, APIHandler)
        httpd.serve_forever()

    return run_server
