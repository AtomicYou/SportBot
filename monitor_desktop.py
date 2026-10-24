"""Standalone Windows window with its own private local monitoring server."""
import asyncio
import ctypes
import json
import socket
import sys
import threading
import time
from pathlib import Path

from aiohttp import web
import webview
from monitor import ROOT, create_app


class LocalServer:
    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self.ready = threading.Event()
        self.loop = None
        self.stop = None
        self.error = None

    def run(self):
        async def serve():
            self.loop = asyncio.get_running_loop()
            self.stop = asyncio.Event()
            runner = web.AppRunner(create_app(ROOT, self.port), access_log=None)
            try:
                await runner.setup()
                await web.SockSite(runner, self.socket).start()
                self.ready.set()
                await self.stop.wait()
            finally:
                await runner.cleanup()
        try:
            asyncio.run(serve())
        except Exception as error:
            self.error = type(error).__name__
            self.ready.set()

    def close(self):
        if self.loop and self.stop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.stop.set)


def main():
    if not (ROOT/'data').is_dir():
        ctypes.windll.user32.MessageBoxW(None, 'Помести EXE в папку sport-bot-v2 рядом с bot.py и папкой data.', 'Inno Sport Monitor', 0x10)
        return
    server = LocalServer()
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    if not server.ready.wait(20) or server.error:
        ctypes.windll.user32.MessageBoxW(None, 'Не удалось запустить локальную панель.', 'Inno Sport Monitor', 0x10)
        return
    testing = '--check-layout' in sys.argv
    window = webview.create_window('Inno Sport Monitor', f'http://127.0.0.1:{server.port}/',
                                  width=1366, height=768, min_size=(1000, 620), maximized=not testing,
                                  background_color='#0d1214', text_select=True)
    def layout_check():
        if not testing:
            return
        try:
            results = []
            for width, height in ((1920, 1080), (1280, 720)):
                window.resize(width, height)
                time.sleep(3)
                results.append(window.evaluate_js('''JSON.stringify({width:innerWidth,height:innerHeight,
                    bodyWidth:document.body.scrollWidth,bodyHeight:document.body.scrollHeight,
                    footerBottom:document.querySelector('footer').getBoundingClientRect().bottom,
                    feedHeight:document.querySelector('.feed').getBoundingClientRect().height,
                    status:document.querySelector('#status').textContent,
                    metrics:[...document.querySelectorAll('.metrics article')].map(e=>({x:e.getBoundingClientRect().x,right:e.getBoundingClientRect().right,bottom:e.getBoundingClientRect().bottom}))})'''))
            output = ROOT/'.tools'/('monitor-layout-exe.json' if getattr(sys, 'frozen', False) else 'monitor-layout.json')
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps([json.loads(r) for r in results], ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            window.destroy()
    try:
        webview.start(layout_check, gui='edgechromium', private_mode=True, debug=False)
    finally:
        server.close()
        worker.join(timeout=5)


if __name__ == '__main__':
    main()
