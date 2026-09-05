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
        # 提取图片：过滤小图和无关图，优先大图
        expr = r"""
(function(){
  var imgs = document.querySelectorAll('img');
  var out = [];
  var seen = {};
  var skipKw = ['logo','avatar','qrcode','qr-code','emoji','smile','spinner','placeholder'];
  function add(u, w, h){
    if(!u) return;
    u = u.trim();
    if(!u || u.startsWith('data:') || u.startsWith('blob:')) return;
    try{ u = new URL(u, location.href).href; }catch(e){ return; }
    var low = u.toLowerCase();
    // 只过滤明显的无关图（logo/头像/二维码/表情）
    for(var i=0;i<skipKw.length;i++){ if(low.indexOf(skipKw[i])>=0) return; }
    // 只过滤极小图（<50px的图标/logo），不过滤正常缩略图
    if(w && h && (w < 50 && h < 50)) return;
    if(!seen[u]){ seen[u]=1; out.push(u); }
  }
  imgs.forEach(function(im){
    var w = im.naturalWidth || im.width || 0;
    var h = im.naturalHeight || im.height || 0;
    add(im.currentSrc || im.src, w, h);
    ['data-src','data-original','data-lazy-src','data-url','src','data-lazy','data-actual'].forEach(function(a){
      add(im.getAttribute(a), w, h);
    });
  });
  // 直接链接到图片的 a 标签（通常是原图链接）
  document.querySelectorAll('a[href$=".jpg"],a[href$=".jpeg"],a[href$=".png"],a[href$=".webp"],a[href$=".gif"],a[href$=".mp4"],a[href$=".webm"]').forEach(function(a){
    add(a.href, 0, 0);
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


def grab_list_page(url, wait_sec=5, scroll_times=3, max_posts=30, timeout=60, log=None):
    '''列表页模式：自动识别帖子链接，逐个进入抓原图，返回 (images, videos)'''
    def lg(m):
        if log:
            log(m)
    if not is_connected():
        raise RuntimeError('未检测到调试模式的浏览器')

    # 第一步：打开列表页，提取帖子链接
    lg('正在分析列表页，识别帖子链接...')
    target_id = None
    ws = None
    post_links = []
    try:
        target_id, ws_url = _new_page('about:blank')
        ws = websocket.create_connection(ws_url, timeout=timeout)
        _send(ws, 'Page.enable')
        _send(ws, 'Runtime.enable')
        _send(ws, 'Page.navigate', {'url': url})
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            try:
                r = _send(ws, 'Runtime.evaluate', {
                    'expression': 'document.readyState', 'returnByValue': True})
                if r.get('result', {}).get('result', {}).get('value', '') == 'complete':
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if scroll_times > 0:
            for _ in range(scroll_times):
                _send(ws, 'Runtime.evaluate', {'expression': 'window.scrollBy(0, document.body.scrollHeight);'})
                time.sleep(1.0)
        # 提取帖子链接的 JavaScript
        js_expr = (
            "(function(){"
            "var links=document.querySelectorAll('a[href]');"
            "var out=[];var seen={};"
            "var host=location.hostname.replace(/^www\\./,'');"
            "var skip=['/','/index','/home','/category','/tag','/tags','/search','/page','/about','/contact','/login','/register','/user','/users','/member','/members'];"
            "links.forEach(function(a){"
            "if(!a.href)return;"
            "try{var u=new URL(a.href,location.href);}catch(e){return;}"
            "if(u.hostname.replace(/^www\\./,'')!==host)return;"
            "var p=u.pathname;if(p==='/'||p==='')return;"
            "for(var i=0;i<skip.length;i++){if(p.indexOf(skip[i])===0)return;}"
            "var parts=p.split('/').filter(Boolean);"
            "if(parts.length<2&&!/\\d/.test(p))return;"
            "var clean=u.origin+u.pathname;"
            "if(!seen[clean]){seen[clean]=1;out.push(clean);}"
            "});"
            "return out;"
            "})()"
        )
        r = _send(ws, 'Runtime.evaluate', {'expression': js_expr, 'returnByValue': True})
        post_links = r.get('result', {}).get('result', {}).get('value', []) or []
        if max_posts > 0:
            post_links = post_links[:max_posts]
        lg('识别到 %d 个帖子链接' % len(post_links))
    finally:
        if ws:
            try: ws.close()
            except Exception: pass
        if target_id:
            try: close_page(target_id)
            except Exception: pass

    if not post_links:
        lg('未识别到帖子链接，回退为单页抓取')
        return grab_page(url, wait_sec=wait_sec, scroll_times=scroll_times, timeout=timeout, log=log)

    # 第二步：逐个进入帖子抓原图
    all_images = []
    all_videos = []
    seen = set()
    for idx, post_url in enumerate(post_links):
        lg('[%d/%d] 正在抓取: %s' % (idx + 1, len(post_links), post_url))
        try:
            imgs, vids = grab_page(post_url, wait_sec=wait_sec, scroll_times=1, timeout=timeout, log=None)
            for u in imgs:
                if u not in seen:
                    seen.add(u)
                    all_images.append(u)
            for u in vids:
                if u not in seen:
                    seen.add(u)
                    all_videos.append(u)
        except Exception as e:
            lg('  抓取失败: %s' % str(e)[:80])
        if (idx + 1) % 5 == 0:
            lg('进度: %d/%d，已收集图片 %d 张' % (idx + 1, len(post_links), len(all_images)))

    lg('列表页抓取完成：%d 个帖子，共图片 %d 张、视频 %d 个' % (len(post_links), len(all_images), len(all_videos)))
    return all_images, all_videos
