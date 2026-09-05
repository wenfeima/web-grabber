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


def get_current_url():
    """获取浏览器当前活动标签页的 URL（用于地址栏同步）"""
    try:
        pages = _list_pages()
        # 找第一个 type=page 且不是内部页面的标签页
        for p in pages:
            if p.get('type') == 'page':
                url = p.get('url', '')
                if url and url != 'about:blank' and not url.startswith('chrome://') and not url.startswith('edge://'):
                    return url
        # 如果都不符合，返回第一个 page 的 URL
        for p in pages:
            if p.get('type') == 'page':
                return p.get('url', '')
        return ''
    except Exception:
        return ''


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
        # 提取图片：过滤小图和无关图，优先大图，支持更多懒加载属性和srcset
        expr = r"""
(function(){
  var imgs = document.querySelectorAll('img');
  var out = [];
  var seen = {};
  var skipKw = ['logo','avatar','qrcode','qr-code','emoji','smile','spinner','placeholder','icon'];
  // 缩略图关键词（用于识别和转换）
  var thumbKw = ['thumb','thumbnail','_small','_medium','_cover','_avatar','_150x150','_300x300','_200x200','_100x100','_50x50','/w150','/w200','/w300','?w=150','?w=200','?w=300','?imageView2','?x-oss-process'];
  function add(u, w, h){
    if(!u) return;
    u = u.trim();
    if(!u || u.startsWith('data:') || u.startsWith('blob:')) return;
    try{ u = new URL(u, location.href).href; }catch(e){ return; }
    var low = u.toLowerCase();
    for(var i=0;i<skipKw.length;i++){ if(low.indexOf(skipKw[i])>=0) return; }
    if(w && h && (w < 50 && h < 50)) return;
    if(!seen[u]){ seen[u]=1; out.push(u); }
  }
  // 解析 srcset，取最大尺寸的图片
  function parseSrcset(srcset){
    if(!srcset) return null;
    var parts = srcset.split(',');
    var maxUrl = null;
    var maxW = 0;
    parts.forEach(function(p){
      p = p.trim();
      var m = p.match(/^(\S+)\s+(\d+)w$/);
      if(m){
        var w = parseInt(m[2]);
        if(w > maxW){ maxW = w; maxUrl = m[1]; }
      } else if(p){
        if(!maxUrl) maxUrl = p.split(' ')[0];
      }
    });
    return maxUrl;
  }
  // 尝试把缩略图URL转换成原图URL
  function toOriginal(u){
    if(!u) return null;
    var low = u.toLowerCase();
    // 去掉常见的缩略图参数
    var patterns = [
      [/_thumb\.(jpg|jpeg|png|webp|gif)$/i, '.$1'],
      [/_small\.(jpg|jpeg|png|webp|gif)$/i, '.$1'],
      [/_medium\.(jpg|jpeg|png|webp|gif)$/i, '.$1'],
      [/_cover\.(jpg|jpeg|png|webp|gif)$/i, '.$1'],
      [/_\d+x\d+\.(jpg|jpeg|png|webp|gif)$/i, '.$1'],
      [/[?&]w=\d+.*$/i, ''],
      [/[?&]h=\d+.*$/i, ''],
      [/\?imageView2.*$/i, ''],
      [/\?x-oss-process.*$/i, ''],
      [/\/thumb\//i, '/'],
      [/\/thumbnail\//i, '/'],
      [/\/small\//i, '/'],
      [/\/medium\//i, '/'],
    ];
    for(var i=0;i<patterns.length;i++){
      if(patterns[i][0].test(u)){
        var orig = u.replace(patterns[i][0], patterns[i][1]);
        if(orig !== u) return orig;
      }
    }
    return null;
  }
  imgs.forEach(function(im){
    var w = im.naturalWidth || im.width || 0;
    var h = im.naturalHeight || im.height || 0;
    // 基本属性
    add(im.currentSrc || im.src, w, h);
    // 更多懒加载属性
    ['data-src','data-original','data-lazy-src','data-url','src','data-lazy','data-actual',
     'data-full','data-large','data-big','data-hd','data-origin','data-real','data-raw',
     'data-srcset','data-orig','data-originalsrc','data-lazyload','data-load'].forEach(function(a){
      var val = im.getAttribute(a);
      if(val){
        if(a.indexOf('srcset')>=0){
          var parsed = parseSrcset(val);
          if(parsed) add(parsed, w, h);
        } else {
          add(val, w, h);
        }
      }
    });
    // srcset 属性
    var srcset = im.srcset || im.getAttribute('srcset');
    if(srcset){
      var parsed = parseSrcset(srcset);
      if(parsed) add(parsed, w, h);
    }
    // 尝试把当前图片转换成原图
    var orig = toOriginal(im.currentSrc || im.src);
    if(orig) add(orig, 0, 0);
  });
  // 直接链接到图片的 a 标签（通常是原图链接）- 支持更多扩展名和查询参数
  document.querySelectorAll('a').forEach(function(a){
    var href = a.href || '';
    if(!href) return;
    var low = href.toLowerCase().split('?')[0];
    if(low.match(/\.(jpg|jpeg|png|webp|gif|bmp|mp4|webm|mov)$/)){
      add(href, 0, 0);
    }
  });
  return out;
})()
"""
        r = _send(ws, 'Runtime.evaluate', {'expression': expr, 'returnByValue': True})
        arr = r.get('result', {}).get('result', {}).get('value', []) or []
        # 获取页面标题
        try:
            r_title = _send(ws, 'Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True})
            page_title = r_title.get('result', {}).get('result', {}).get('value', '') or ''
        except Exception:
            page_title = ''
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
        return images, videos, page_title
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
        js_expr = "(function(){var out=[];var seen={};var host=location.hostname.replace(/^www\\./,'');var navKw=['nav','menu','header','footer','sidebar','widget','breadcrumb','pagination','pager','toolbar','topbar','bottom','social','share','follow','subscribe','login','register','signup','search','about','contact','category','tag','archive','author','user','member','profile','comment'];function isNav(el){var e=el;while(e&&e!==document.body){if(e.tagName){var t=e.tagName.toLowerCase();if(t==='nav'||t==='header'||t==='footer'||t==='aside')return true;}var c=(e.className&&typeof e.className==='string')?e.className.toLowerCase():'';var i=(e.id&&typeof e.id==='string')?e.id.toLowerCase():'';for(var k=0;k<navKw.length;k++){if(c.indexOf(navKw[k])>=0||i.indexOf(navKw[k])>=0)return true;}e=e.parentElement;}return false;}var pathKw=['/about','/contact','/category','/tag','/archive','/search','/login','/register','/signup','/user','/member','/profile','/author','/comment','/page/','/feed','/rss','/sitemap','/tags','/categories'];var links=document.querySelectorAll('a');var total=links.length;var sameHost=0;var filtered=0;links.forEach(function(a){if(!a.href)return;try{var u=new URL(a.href,location.href);}catch(e){return;}if(u.hostname.replace(/^www\\./,'')!==host)return;sameHost++;var p=u.pathname;if(p==='/'||p==='')return;if(isNav(a)){filtered++;return;}var pl=p.toLowerCase();for(var k=0;k<pathKw.length;k++){if(pl.indexOf(pathKw[k])===0){filtered++;return;}}var clean=u.origin+u.pathname;if(!seen[clean]){seen[clean]=1;out.push(clean);}});return JSON.stringify({total:total,sameHost:sameHost,filtered:filtered,found:out.length,links:out,sample:out.slice(0,10)});})()"
        r = _send(ws, 'Runtime.evaluate', {'expression': js_expr, 'returnByValue': True})
        _raw_val = r.get('result', {}).get('result', {}).get('value', '{}')
        try:
            _debug = json.loads(_raw_val) if isinstance(_raw_val, str) else {}
            lg('调试: 页面链接总数=%d, 同域名=%d, 过滤导航=%d, 识别到=%d' % (_debug.get('total',0), _debug.get('sameHost',0), _debug.get('filtered',0), _debug.get('found',0)))
            if _debug.get('sample'):
                lg('前10个链接: %s' % str(_debug['sample'])[:200])
            post_links = _debug.get('links', [])
        except Exception as _e:
            lg('解析调试信息失败: %s' % _e)
            post_links = []
        if max_posts > 0 and len(post_links) >= max_posts:
            post_links = post_links[:max_posts]
            lg('识别到 %d 个帖子链接（已达上限）' % len(post_links))
        else:
            lg('识别到 %d 个帖子链接' % len(post_links))

        # 翻页逻辑：识别"下一页"链接，自动翻页继续抓
        max_pages = 50  # 最大翻页数，防止无限翻页
        all_links = list(post_links)
        seen_links = set(all_links)
        for page_num in range(2, max_pages + 1):
            if max_posts > 0 and len(all_links) >= max_posts:
                lg('已达最大帖子数 %d，停止翻页' % max_posts)
                break
            # 识别下一页链接
            next_js = "(function(){var host=location.hostname.replace(/^www\./,'');var nextTexts=['下一页','下页','下一頁','下一页','next','>','»','›','下一个','后一页','后页'];var nextClasses=['next','pagination-next','page-next','pager-next','next-page','nextpage'];var links=document.querySelectorAll('a');for(var i=0;i<links.length;i++){var a=links[i];if(!a.href)continue;try{var u=new URL(a.href,location.href);}catch(e){continue;}if(u.hostname.replace(/^www\./,'')!==host)continue;var text=(a.textContent||a.innerText||'').trim().toLowerCase();var cls=(a.className&&typeof a.className==='string')?a.className.toLowerCase():'';var rel=(a.getAttribute&&a.getAttribute('rel'))?a.getAttribute('rel').toLowerCase():'';if(rel==='next')return a.href;for(var k=0;k<nextTexts.length;k++){if(text===nextTexts[k]||text.indexOf(nextTexts[k])>=0&&text.length<=10)return a.href;}for(var k=0;k<nextClasses.length;k++){if(cls.indexOf(nextClasses[k])>=0)return a.href;}}return '';})()"
            try:
                r = _send(ws, 'Runtime.evaluate', {'expression': next_js, 'returnByValue': True})
                next_url = r.get('result', {}).get('result', {}).get('value', '')
            except Exception:
                next_url = ''
            if not next_url:
                lg('未找到下一页链接，停止翻页（共 %d 页）' % (page_num - 1))
                break
            lg('正在翻到第 %d 页: %s' % (page_num, next_url[:80]))
            # 导航到下一页
            _send(ws, 'Page.navigate', {'url': next_url})
            time.sleep(2)
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
            # 滚动触发懒加载
            if scroll_times > 0:
                for _ in range(scroll_times):
                    _send(ws, 'Runtime.evaluate', {'expression': 'window.scrollBy(0, document.body.scrollHeight);'})
                    time.sleep(1.0)
            # 提取当前页帖子链接
            try:
                r = _send(ws, 'Runtime.evaluate', {'expression': js_expr, 'returnByValue': True})
                _raw_val = r.get('result', {}).get('result', {}).get('value', '{}')
                _debug = json.loads(_raw_val) if isinstance(_raw_val, str) else {}
                page_links = _debug.get('links', [])
                new_count = 0
                for pl in page_links:
                    if pl not in seen_links:
                        seen_links.add(pl)
                        all_links.append(pl)
                        new_count += 1
                        if max_posts > 0 and len(all_links) >= max_posts:
                            break
                lg('第 %d 页识别到 %d 个新链接（累计 %d 个）' % (page_num, new_count, len(all_links)))
            except Exception as _e:
                lg('第 %d 页解析失败: %s' % (page_num, _e))
        post_links = all_links[:max_posts] if max_posts > 0 else all_links
        lg('翻页完成，共识别到 %d 个帖子链接' % len(post_links))
    finally:
        if ws:
            try: ws.close()
            except Exception: pass
        if target_id:
            try: close_page(target_id)
            except Exception: pass

    if not post_links:
        lg('未识别到帖子链接，回退为单页抓取')
        imgs, vids, ptitle = grab_page(url, wait_sec=wait_sec, scroll_times=scroll_times, timeout=timeout, log=log)
        return [{'title': ptitle, 'url': url, 'images': imgs, 'videos': vids}]

    # 第二步：逐个进入帖子抓原图
    posts = []
    seen = set()
    for idx, post_url in enumerate(post_links):
        lg('[%d/%d] 正在抓取: %s' % (idx + 1, len(post_links), post_url))
        try:
            imgs, vids, ptitle = grab_page(post_url, wait_sec=wait_sec, scroll_times=1, timeout=timeout, log=None)
            # 去重
            unique_imgs = []
            for u in imgs:
                if u not in seen:
                    seen.add(u)
                    unique_imgs.append(u)
            unique_vids = []
            for u in vids:
                if u not in seen:
                    seen.add(u)
                    unique_vids.append(u)
            posts.append({'title': ptitle, 'url': post_url, 'images': unique_imgs, 'videos': unique_vids})
        except Exception as e:
            lg('  抓取失败: %s' % str(e)[:80])
            posts.append({'title': '', 'url': post_url, 'images': [], 'videos': []})
        if (idx + 1) % 5 == 0:
            total_imgs = sum(len(p['images']) for p in posts)
            lg('进度: %d/%d，已收集图片 %d 张' % (idx + 1, len(post_links), total_imgs))

    total_imgs = sum(len(p['images']) for p in posts)
    total_vids = sum(len(p['videos']) for p in posts)
    lg('列表页抓取完成：%d 个帖子，共图片 %d 张、视频 %d 个' % (len(posts), total_imgs, total_vids))
    return posts
