# -*- coding: utf-8 -*-
"""CDP 浏览器模式：连接已开启调试端口的 Edge/Chrome（带油猴/登录态），
打开页面、等待脚本执行、提取图片/视频地址。"""
import json
import time
import urllib.request
import urllib.parse
import websocket

DEBUG_PORT = 9222
BASE = 'http://127.0.0.1:%d' % DEBUG_PORT


def is_connected():
    """检查调试端口是否可连接"""
    try:
        with urllib.request.urlopen(BASE + '/json/version', timeout=3) as r:
            return True
    except Exception:
        return False


def _new_page(url):
    """新建标签页，返回 (target_id, ws_url)"""
    u = BASE + '/json/new?' + urllib.parse.quote(url, safe='')
    req = urllib.request.Request(u, method='PUT')
    with urllib.request.urlopen(req, timeout=5) as r:
        d = json.loads(r.read().decode())
    return d['id'], d['webSocketDebuggerUrl']


def _list_pages():
    with urllib.request.urlopen(BASE + '/json', timeout=3) as r:
        return json.loads(r.read().decode())


def close_page(target_id):
    try:
        urllib.request.urlopen(BASE + '/json/close/' + target_id, timeout=3)
    except Exception:
        pass


def open_url(url):
    '在调试浏览器里新建标签页打开指定网址，返回 target_id'
    try:
        target_id, _ = _new_page(url)
        return target_id
    except Exception as e:
        print('open_url 失败: %s' % e)
        return None


def _send(ws, method, params=None, msg_id=None):
    if msg_id is None:
        msg_id = int(time.time() * 1000) % 1000000
    ws.send(json.dumps({'id': msg_id, 'method': method, 'params': params or {}}))
    while True:
        data = json.loads(ws.recv())
        if data.get('id') == msg_id:
            return data


def grab_page(url, wait_sec=5, scroll_times=0, max_images=0, timeout=60, log=None):
    """在浏览器中打开 url，等待油猴执行，滚动加载，返回 (images, videos)
    images/videos: 绝对 URL 列表"""
    def lg(m):
        if log:
            log(m)
    if not is_connected():
        raise RuntimeError('未检测到调试模式的浏览器，请先用专用快捷方式打开 Edge 并勾选「浏览器模式」')
    target_id = None
    ws = None
    try:
        target_id, ws_url = _new_page('about:blank')
        ws = websocket.create_connection(ws_url, timeout=timeout)
        _send(ws, 'Page.enable')
        _send(ws, 'Runtime.enable')
        _send(ws, 'Page.navigate', {'url': url})
        # 等待加载
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            try:
                r = _send(ws, 'Runtime.evaluate', {
                    'expression': 'document.readyState',
                    'returnByValue': True,
                })
                v = r.get('result', {}).get('result', {}).get('value', '')
                if v == 'complete':
                    break
            except Exception:
                pass
            time.sleep(0.5)
        # 滚动加载
        if scroll_times > 0:
            for _ in range(scroll_times):
                _send(ws, 'Runtime.evaluate', {
                    'expression': 'window.scrollBy(0, document.body.scrollHeight);',
                })
                time.sleep(1.0)
        # 提取所有图片和视频地址（含懒加载属性、油猴注入的）
        expr = r"""
(function(){
  var imgs = document.querySelectorAll('img');
  var out = [];
  var seen = {};
  function add(u){
    if(!u) return;
    u = u.trim();
    if(!u || u.startsWith('data:') || u.startsWith('blob:')) return;
    try{ u = new URL(u, location.href).href; }catch(e){ return; }
    if(!seen[u]){ seen[u]=1; out.push(u); }
  }
  imgs.forEach(function(im){
    add(im.currentSrc || im.src);
    ['data-src','data-original','data-lazy-src','data-url','src'].forEach(function(a){
      add(im.getAttribute(a));
    });
  });
  document.querySelectorAll('a[href$=".jpg"],a[href$=".jpeg"],a[href$=".png"],a[href$=".webp"],a[href$=".gif"],a[href$=".mp4"],a[href$=".webm"]').forEach(function(a){
    add(a.href);
  });
  return out;
})()
"""
        r = _send(ws, 'Runtime.evaluate', {'expression': expr, 'returnByValue': True})
        arr = r.get('result', {}).get('result', {}).get('value', []) or []
        images = []
        videos = []
        for u in arr:
            low = u.lower()
            if any(low.endswith(x) for x in ('.mp4', '.webm', '.m3u8', '.mov')):
                videos.append(u)
            elif any(low.endswith(x) for x in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')):
                images.append(u)
            elif any(x in low for x in ('/video', 'video.', 'mp4')):
                videos.append(u)
            else:
                images.append(u)
        if max_images > 0:
            images = images[:max_images]
        lg('浏览器抓到图片 %d 张、视频 %d 个' % (len(images), len(videos)))
        return images, videos
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        if target_id:
            try:
                close_page(target_id)
            except Exception:
                pass
