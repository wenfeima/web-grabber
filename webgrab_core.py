# -*- coding: utf-8 -*-
"""通用网页图片/视频抓取核心逻辑
输入：列表页 URL（如论坛板块页）
输出：按帖子标题分文件夹保存图片/视频
"""
import os
import re
import ssl
import time
import urllib.request
import urllib.parse

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

# 完整浏览器请求头，降低被反爬识别概率
BROWSER_HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'identity',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Cache-Control': 'max-age=0',
}


class GrabError(Exception):
    pass


class Fetcher:
    """带 cookie / 代理的页面抓取器"""

    # 下载引擎：'auto' / 'internal' / 'idm' / 'aria2'（类级默认，GUI 可覆盖）
    engine = 'auto'
    # 检测到的引擎信息，格式 {'engine':..., 'path':..., 'desc':...}，None 表示未检测/用内置
    engine_info = None

    def __init__(self, cookie='', proxy='', stop_event=None):
        self.cookie = cookie
        self.proxy = proxy
        self.stop_event = stop_event
        self._opener = None
        # 实例级也保留一份，便于单实例覆盖
        self.engine = Fetcher.engine

    def _get_opener(self):
        if self._opener is None:
            handlers = []
            if self.proxy:
                handlers.append(urllib.request.ProxyHandler({
                    'http': self.proxy, 'https': self.proxy}))
            self._opener = urllib.request.build_opener(*handlers)
        return self._opener

    def _open(self, req, timeout):
        if self._get_opener():
            return self._get_opener().open(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)

    @staticmethod
    def _safe_url(url):
        """URL 含非 ASCII 字符时做百分号编码，避免 urllib 报 ascii codec 错误"""
        try:
            url.encode('ascii')
            return url
        except UnicodeEncodeError:
            pass
        parts = urllib.parse.urlsplit(url)
        path = urllib.parse.quote(parts.path, safe="/%:@&=+$,;~*'()!-._~")
        query = urllib.parse.quote(parts.query, safe="=&%/?+$,;~*'()!-._:@")
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))

    # ---------------- 外部下载引擎（提速） ----------------
    @staticmethod
    def find_idm():
        """检测 IDM 安装路径"""
        import glob as _g
        cands = []
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    k = winreg.OpenKey(hive, r"Software\DownloadManager")
                    v, _ = winreg.QueryValueEx(k, "ExePath")
                    if v and os.path.exists(v):
                        cands.append(v)
                except Exception:
                    pass
        except Exception:
            pass
        for p in (r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
                  r"C:\Program Files\Internet Download Manager\IDMan.exe",
                  r"D:\Program Files (x86)\Internet Download Manager\IDMan.exe",
                  r"D:\Program Files\Internet Download Manager\IDMan.exe"):
            if os.path.exists(p):
                cands.append(p)
        # 去重
        seen = set(); out = []
        for c in cands:
            if c.lower() not in seen:
                seen.add(c.lower()); out.append(c)
        return out[0] if out else None

    @staticmethod
    def find_aria2():
        """检测 aria2c.exe：优先 exe/脚本同目录，其次 PATH"""
        import shutil as _sh
        # 与主程序同目录（打包/exe 场景）或本文件同目录（源码场景）
        base = None
        try:
            if getattr(__import__('sys'), 'frozen', False):
                base = os.path.dirname(__import__('sys').executable)
            else:
                base = os.path.dirname(os.path.abspath(__file__))
        except Exception:
            pass
        cands = []
        if base:
            for n in ('aria2c.exe', 'aria2c', 'aria2c-1.36.0-win-64bit-build1.exe'):
                p = os.path.join(base, n)
                if os.path.exists(p):
                    cands.append(p)
        try:
            _sh.which('aria2c') and cands.append(_sh.which('aria2c'))
        except Exception:
            pass
        seen = set(); out = []
        for c in cands:
            if c.lower() not in seen:
                seen.add(c.lower()); out.append(c)
        return out[0] if out else None

    @staticmethod
    def detect_engine():
        """返回 (engine, path, desc)。auto 时优先 aria2（可控、支持 referer），其次 IDM"""
        a = Fetcher.find_aria2()
        if a:
            return ('aria2', a, 'aria2 多线程 (%s)' % os.path.basename(a))
        i = Fetcher.find_idm()
        if i:
            return ('idm', i, 'IDM (%s)' % os.path.basename(i))
        return (None, None, None)

    @staticmethod
    def engine_path():
        """外部引擎可执行文件绝对路径；无则 None"""
        if Fetcher.engine_info:
            return Fetcher.engine_info.get('path')
        return None

    def _engine_download(self, url, filepath, referer=None, timeout=120):
        """用外部引擎下载单个文件。成功返回最终字节数；失败/不可用抛异常。"""
        import subprocess as _sp
        _dir = os.path.dirname(filepath) or '.'
        _name = os.path.basename(filepath)
        _url = self._safe_url(url)
        _engine = Fetcher.engine if Fetcher.engine != 'auto' else (Fetcher.engine_info or {}).get('engine')
        _path = Fetcher.engine_path()
        if not _engine or not _path or not os.path.exists(_path):
            raise GrabError('外部引擎不可用')

        if _engine == 'aria2':
            cmd = [_path, '--continue=true',
                   '-x', '16', '-s', '16', '-k', '1M',
                   '--file-allocation=none',
                   '--timeout=30', '--connect-timeout=30', '--retry-wait=3',
                   '--max-tries=3',
                   '-d', _dir, '-o', _name]
            cmd += ['--user-agent=' + UA]
            if self.cookie:
                cmd.append('--header=Cookie: ' + self.cookie)
            if referer:
                cmd.append('--referer=' + referer)
            if self.proxy:
                cmd.append('--all-proxy=' + self.proxy)
            cmd.append(_url)
            # 停止检查：外部进程无法逐块中断，但停止时杀掉进程
            try:
                proc = _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                while proc.poll() is None:
                    if self.stop_event is not None and self.stop_event.is_set():
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        raise GrabError('已停止')
                    time.sleep(0.2)
                if proc.returncode != 0:
                    raise IOError('aria2 下载失败 rc=%d' % proc.returncode)
                return os.path.getsize(filepath)
            except GrabError:
                raise
            except Exception as e:
                raise IOError('aria2 调用失败: %s' % e)

        elif _engine == 'idm':
            # IDM：添加任务到其队列并自动开始，IDM 内部多线程下载。
            # IDM 是异步的：添加一次任务，等待文件出现且大小稳定视为该文件完成。
            try:
                _sp.Popen([_path, '/d', _url, '/p', _dir, '/f', _name, '/a'],
                          stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            except Exception as e:
                raise IOError('IDM 调用失败: %s' % e)
            deadline = time.time() + max(timeout, 90)
            last_size = -1
            stable_cnt = 0
            while time.time() < deadline:
                if self.stop_event is not None and self.stop_event.is_set():
                    raise GrabError('已停止')
                sz = os.path.getsize(filepath) if os.path.exists(filepath) else -1
                if sz < 0:
                    # 文件尚未出现：给 IDM 一点启动时间
                    time.sleep(0.3)
                    continue
                if sz > 0 and sz == last_size:
                    stable_cnt += 1
                    if stable_cnt >= 10:   # 约2秒大小不变 -> 视为完成
                        return sz
                else:
                    last_size = sz
                    stable_cnt = 0
                time.sleep(0.2)
            raise IOError('IDM 下载超时（任务已加入 IDM 队列，可查看 IDM 窗口进度）')

        raise GrabError('未知外部引擎: %s' % _engine)

    def _dl_multipart(self, url, filepath, referer=None, timeout=30, retries=3, max_segments=8):
        """内置多线程分段下载（Range 并发），提速用。
        服务器支持 Range 且文件较大时把文件切成多段并发下载再合并。
        返回最终字节数；不支持 Range / 文件过小 / 被中断 -> 返回 None（由调用方回退单线程）。"""
        import threading as _th
        headers = dict(BROWSER_HEADERS)
        if self.cookie:
            headers['Cookie'] = self.cookie
        if referer:
            headers['Referer'] = referer

        # 探测：请求第一个字节，看是否 206 + Content-Range
        probe = dict(headers)
        probe['Range'] = 'bytes=0-0'
        try:
            req = urllib.request.Request(self._safe_url(url), headers=probe)
            resp = self._open(req, timeout)
        except Exception:
            return None
        if getattr(resp, 'status', None) != 206:
            try:
                resp.close()
            except Exception:
                pass
            return None
        total = None
        cr = resp.headers.get('Content-Range') or ''
        m = re.search(r'/(\d+)\s*$', cr)
        if m:
            total = int(m.group(1))
        try:
            resp.close()
        except Exception:
            pass
        if not total or total <= 0:
            return None
        # 文件太小（<512KB）分段收益低，走单线程
        if total < 512 * 1024:
            return None

        # 计算分段数：尽量每个分片 >=256KB
        segs = max_segments
        max_ok = total // (256 * 1024)
        if max_ok < segs:
            segs = max(1, max_ok)
        parts = []
        for i in range(segs):
            start = total * i // segs
            end = total * (i + 1) // segs - 1
            if i == segs - 1:
                end = total - 1
            parts.append((start, end))

        _dir = os.path.dirname(filepath) or '.'
        _base = os.path.basename(filepath)
        results = [None] * segs
        cancel = [False]
        lock = _th.Lock()

        def _one(idx, start, end):
            pfile = os.path.join(_dir, '.%s.part%d' % (_base, idx))
            h = dict(headers)
            h['Range'] = 'bytes=%d-%d' % (start, end)
            for attempt in range(retries + 1):
                if cancel[0] or (self.stop_event is not None and self.stop_event.is_set()):
                    return
                try:
                    r = self._open(urllib.request.Request(self._safe_url(url), headers=h), timeout)
                    if getattr(r, 'status', None) != 206:
                        # 服务器忽略 Range 返回全量 -> 不支持分段，整体回退
                        with lock:
                            cancel[0] = True
                        return
                    with open(pfile, 'wb') as f:
                        while True:
                            if self.stop_event is not None and self.stop_event.is_set():
                                with lock:
                                    cancel[0] = True
                                return
                            chunk = r.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                    with lock:
                        results[idx] = pfile
                    return
                except Exception:
                    if attempt >= retries:
                        with lock:
                            cancel[0] = True
                        return
                    time.sleep(1.0 * (attempt + 1))

        threads = []
        for idx, (s, e) in enumerate(parts):
            t = _th.Thread(target=_one, args=(idx, s, e), daemon=True)
            t.start()
            threads.append(t)
        # 停止时不等卡住的网络 read：给 join 加超时（daemon 线程随进程退出）
        if self.stop_event is not None and self.stop_event.is_set():
            _join_to = 0.5
        else:
            _join_to = None
        for t in threads:
            t.join(_join_to)

        if cancel[0]:
            # 被中断 / 不支持分段：清理分片，回退单线程
            for p in results:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            return None

        # 合并分片
        try:
            with open(filepath, 'wb') as out:
                for idx in range(segs):
                    p = results[idx]
                    if not p or not os.path.exists(p):
                        raise IOError('分片缺失')
                    with open(p, 'rb') as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            out.write(chunk)
        except Exception:
            for p in results:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            raise
        # 清理分片
        for p in results:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        final = os.path.getsize(filepath)
        if final != total:
            raise IOError('下载不完整: %d/%d bytes' % (final, total))
        return final

    def fetch(self, url, timeout=20, referer=None):
        headers = dict(BROWSER_HEADERS)
        if self.cookie:
            headers['Cookie'] = self.cookie
        if referer:
            headers['Referer'] = referer
        req = urllib.request.Request(self._safe_url(url), headers=headers)
        resp = self._open(req, timeout)
        data = resp.read()
        charset = resp.headers.get_content_charset() or 'utf-8'
        try:
            text = data.decode(charset, 'ignore')
        except Exception:
            text = data.decode('utf-8', 'ignore')
        return text

    def download(self, url, filepath, timeout=30, referer=None, retries=3):
        """下载文件，带重试 + 断点续传。可选的：外部引擎(aria2/IDM)提速。
        retries: 最大重试次数（0 表示不重试）。失败或读取中断时自动续传已下载部分。"""
        import os as _os
        # 外部引擎分发（除非显式 internal）
        _use_ext = Fetcher.engine != 'internal'
        if _use_ext and Fetcher.engine == 'auto' and Fetcher.engine_info is None:
            Fetcher.engine_info = {'engine': None, 'path': None, 'desc': None}
        if _use_ext and Fetcher.engine == 'auto' and Fetcher.engine_info.get('engine') is None:
            try:
                e, p, d = Fetcher.detect_engine()
                Fetcher.engine_info = {'engine': e, 'path': p, 'desc': d}
            except Exception:
                Fetcher.engine_info = {'engine': None, 'path': None, 'desc': None}
        if _use_ext:
            _ext = Fetcher.engine if Fetcher.engine != 'auto' else (Fetcher.engine_info or {}).get('engine')
            if _ext and _ext != 'internal':
                if _ext == 'idm':
                    # IDM 是异步下载器：添加一次任务后轮询等待完成。
                    # 不做重试/清理（任务仍在 IDM 队列，清理会丢下载）。
                    return self._engine_download(url, filepath, referer=referer, timeout=timeout)
                for attempt in range(retries + 1):
                    try:
                        # aria2 同步阻塞，自带断点续传
                        return self._engine_download(url, filepath, referer=referer, timeout=timeout)
                    except GrabError:
                        raise
                    except Exception as e:
                        if attempt >= retries:
                            # 外部引擎彻底失败：清理半成品，回退内置下载一次
                            try:
                                if os.path.exists(filepath):
                                    os.remove(filepath)
                            except Exception:
                                pass
                            break
                        time.sleep(1.5 * (attempt + 1))
        headers = dict(BROWSER_HEADERS)
        if self.cookie:
            headers['Cookie'] = self.cookie
        if referer:
            headers['Referer'] = referer

        # 内置提速：优先多线程分段下载；失败/不支持时回退下方单线程
        try:
            _r = self._dl_multipart(url, filepath, referer=referer, timeout=timeout, retries=retries)
            if _r is not None:
                return _r
        except GrabError:
            raise
        except Exception:
            pass

        last = None
        for attempt in range(retries + 1):
            size = 0
            if _os.path.exists(filepath):
                size = _os.path.getsize(filepath)
            h = dict(headers)
            if size > 0:
                h['Range'] = 'bytes=%d-' % size
            req = urllib.request.Request(self._safe_url(url), headers=h)
            try:
                resp = self._open(req, timeout)
                # 服务器可能忽略 Range 返回 200 全量
                if resp.status == 200 and size > 0:
                    size = 0  # 全量返回，从头写
                # 记录服务器声明的完整大小，用于下载完整性校验
                _total = None
                try:
                    _ct = resp.headers.get('Content-Length')
                    if _ct is not None:
                        _total = int(_ct)
                except Exception:
                    _total = None
                mode = 'ab' if size > 0 else 'wb'
                with open(filepath, mode) as f:
                    if size == 0:
                        f.seek(0)
                        f.truncate()
                    while True:
                        if self.stop_event is not None and self.stop_event.is_set():
                            raise GrabError('已停止')
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                final_size = _os.path.getsize(filepath)
                # 完整性校验：声明大小已知且不匹配 -> 视为下载不完整，抛错重试
                if _total is not None and final_size < _total:
                    raise IOError('下载不完整: %d/%d bytes' % (final_size, _total))
                return final_size
            except Exception as e:
                last = e
                if attempt >= retries:
                    break
                time.sleep(1.5 * (attempt + 1))
        # 重试耗尽：删除损坏的不完整文件，避免留下灰色占位
        try:
            if _os.path.exists(filepath):
                _os.remove(filepath)
        except Exception:
            pass
        raise last


# ---------- 链接/标题提取 ----------

def clean_title(t):
    import html as _html
    t = _html.unescape(t or '')
    t = re.sub(r'<[^>]+>', '', t)
    t = t.replace('&amp;', '&').replace('&nbsp;', ' ').replace('&quot;', '"')
    # 过滤字体图标等私有区字符（如 &#xe68b;）
    t = re.sub(r'[\ue000-\uf8ff]', '', t)
    return re.sub(r'[\\/:*?"<>|\r\n\t ]+', '_', t).strip('_')[:80] or 'untitled'
def _is_noise_title(t):
    """过滤掉数字/时间/图标/JS代码等噪声标题"""
    if re.fullmatch(r'\d+', t):
        return True
    if re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}.*', t):
        return True
    # JS 代码特征：含等号+引号/分号/var 等
    if re.search(r'[=;]\s*[\'"]|var\s+\w+\s*=|function\s*\(|\$\(|document\.', t):
        return True
    # 导航/功能性链接
    noise_words = ['登录异常', '游戏商城', '快速开始', '只需一步', '登录', '注册']
    if any(w in t for w in noise_words) and len(t) < 12:
        return True
    return False


def is_thread_page(url):
    """判断是否为单个帖子页（thread-xxx-1-1.html 或 数字id结尾的详情页），而非列表页"""
    if re.search(r'thread-\d+-1-1\.html', url, re.I):
        return True
    # 通用文章详情页: 目录/xxx/123.html 或 目录/日期/123.html
    if re.search(r'/[a-zA-Z0-9_\-]+(?:/[a-zA-Z0-9_\-]+)*/\d+\.html$', url):
        return True
    # 无后缀数字 id 详情页: 目录/xxx/123 或 目录/xxx/yyy/123（cosplay8 等）
    if re.search(r'/[a-zA-Z0-9_\-]+(?:/[a-zA-Z0-9_\-]+)*/[0-9]+$', url) and not re.search(r'/list/[a-zA-Z0-9_\-]+/[0-9]+$', url) and not re.search(r'/coser/[0-9]+$', url):
        return True
    return False


def extract_threads(html, base_url):
    """从列表页提取 (url, title) 列表，支持 Discuz thread-xxx 及通用文章详情页链接。
    每个帖子的链接在页面中出现多次（图标/标题/回复数/时间），
    取文本最长且非噪声的那条作为标题。"""
    threads = {}
    # Discuz 模式: thread-123-1-1.html
    pat1 = re.compile(r'href="([^"]*?thread-(\d+)-1-1\.html)"[^>]*>(.*?)</a>',
                      re.S | re.I)
    # 通用文章模式: 目录/xxx/123.html 或 目录/日期/123.html（数字id结尾的详情页）
    pat2 = re.compile(r'href="([^"]*?/(?:[a-zA-Z0-9_\-]+/)*\d+\.html)"[^>]*>(.*?)</a>',
                      re.S | re.I)
    pat = re.compile(r'href="([^"]*?thread-(\d+)-1-1\.html)"[^>]*>(.*?)</a>'
                     r'|href="([^"]*?/(?:[a-zA-Z0-9_\-]+/)*\d+\.html)"[^>]*>(.*?)</a>'
                     r'|href="([^"]*?/(?:[a-zA-Z0-9_\-]+/)+\d+)"[^>]*>(.*?)</a>'
                     r'|href="([^"]*?/[A-Za-z0-9]{4,10}\.html)"[^>]*>(.*?)</a>',
                     re.S | re.I)
    for m in pat.finditer(html):
        if m.group(2):  # Discuz 模式
            href, tid, t = m.group(1), m.group(2), m.group(3)
        elif m.group(4):  # 通用 .html 模式
            href, tid, t = m.group(4), None, m.group(5)
        elif m.group(6):  # 无后缀数字 id 模式
            href, tid, t = m.group(6), None, m.group(7)
        else:             # 根目录字母数字短 id .html 模式
            href, tid, t = m.group(8), None, m.group(9)
        # 标题：优先链接文本；为空(如图片链接)时回退用图片 alt
        title_src = t
        if not re.sub(r'<[^>]+>', '', title_src or '').strip():
            alts = re.findall(r'<img[^>]*\balt="([^"]*)"', title_src or '', re.I)
            if alts:
                title_src = alts[0]
        title = clean_title(title_src)
        if not title or _is_noise_title(title):
            continue
        # 过滤站点名/通用 alt（如"推次元 - 爱二次元COS分享发现平台"）
        if '分享发现平台' in title or '推次元' == title.strip('_'):
            continue
        if href.startswith('http'):
            url = href
        else:
            url = urllib.parse.urljoin(base_url, href)
        # 提取封面缩略图（始终从原始链接 HTML 的 img 提取，不被标题替换影响）
        thumb = ''
        for attr in ('data-src', 'data-original', 'src'):
            mm = re.search(r'<img[^>]*\b%s="([^"]+)"' % attr, t or '', re.I)
            if mm:
                thumb = mm.group(1).strip()
                break
        if thumb and not thumb.lower().startswith(('http://', 'https://')):
            thumb = urllib.parse.urljoin(url, thumb)
        # 排除 /list/分类/N 这类分类导航链接（cosplay8 等站点）
        if re.search(r'/list/[a-zA-Z0-9_\-]+/[0-9]+$', url):
            continue
        # 排除 /page/N、/index/N 等分页链接（无 .html 后缀的纯数字路径，如 aethercms 的 /page/2）
        if re.search(r'/(?:page|index|list|cat|tag|category|category-)/[0-9]+/?$', url, re.I):
            continue
        # 排除 /coser/N 作者主页链接（次元岛等站点，非图集）
        if re.search(r'/coser/[a-zA-Z0-9_\-]+/[0-9]+$', url) or re.search(r'/coser/[0-9]+$', url):
            continue
        # 排除站外/导航类短链（正文详情页链接通常较长且含数字id）
        key = tid if tid else url
        # 去重：untitled 占位标题优先级最低（如 <a> 里只有 <img> 无 alt 的情况）
        if title == 'untitled':
            if key not in threads:
                threads[key] = (url, title, thumb)
            continue
        if key in threads:
            # 已有 untitled 占位则直接覆盖；否则保留较长标题
            if threads[key][1] == 'untitled' or len(title) > len(threads[key][1]):
                # 覆盖时若新条目没有缩略图，保留旧缩略图
                _old_thumb = threads[key][2]
                threads[key] = (url, title, thumb or _old_thumb)
        else:
            threads[key] = (url, title, thumb)
    return list(threads.values())


# ---------- 图片/视频 URL 提取 ----------

IMG_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif')
VID_EXTS = ('.mp4', '.webm', '.mkv', '.mov', '.avi', '.flv', '.ts', '.m3u8', '.mpg', '.mpeg')


def _ext(u):
    """取 URL 路径的扩展名（忽略查询参数/锚点）"""
    try:
        p = urllib.parse.urlparse(u).path.lower()
        return os.path.splitext(p)[1]
    except Exception:
        return ''


def _is_img_url(u):
    if _ext(u) in IMG_EXTS or '/attach' in u.lower() or 'att.' in u.lower():
        return True
    # 兼容 .jpg!p330 这类"扩展名+!处理后缀"的 URL（cosplay8 等）
    if re.search(r'\.(?:jpg|jpeg|png|webp|bmp|avif)![a-zA-Z0-9_]+$', u, re.I):
        return True
    return False


def _is_vid_url(u):
    return _ext(u) in VID_EXTS


def extract_media(html, base_url):
    """从帖子页提取 (图片列表, 视频列表)，过滤模板/头像等噪音"""
    imgs = set()
    vids = set()

    # 图片：常见懒加载/原图属性
    img_attrs = ['zoomfile', 'data-original', 'data-src', 'data-loadsrc', 'file',
                 'data-echo', 'data-lazyload-src', 'data-url', 'data-srcset',
                 'data-lazy-src']
    for attr in img_attrs:
        for u in re.findall(r'%s="([^"]+)"' % attr, html, re.I):
            u = u.strip()
            if not u or u.startswith('data:'):
                continue
            u = urllib.parse.urljoin(base_url, u)
            if _is_img_url(u):
                imgs.add(u)

    # 图片：<img> 标签的 src/srcset
    for m in re.finditer(r'<img[^>]*?src="([^"]+)"', html, re.I):
        u = urllib.parse.urljoin(base_url, m.group(1).strip())
        if _is_img_url(u):
            imgs.add(u)
    for m in re.finditer(r'<img[^>]*?srcset="([^"]+)"', html, re.I):
        for part in m.group(1).split(','):
            part = part.strip().split(' ')[0]
            if part:
                u = urllib.parse.urljoin(base_url, part)
                if _is_img_url(u):
                    imgs.add(u)

    # 图片：JS/脚本里的图片直链数组（如 const images=['url','url']）兜底。
    # 覆盖 cosplay8 等把图片 URL 藏在 JS 数组里的站点。
    for m in re.finditer(r'''["'](https?://[^"'\s]+\.(?:jpg|jpeg|png|webp|bmp|avif))["']''', html, re.I):
        u = m.group(1).strip()
        if u and not u.startswith('data:'):
            imgs.add(u)

    # 视频：<video>/<source> 标签
    for m in re.finditer(r'<video[^>]*?src="([^"]+)"', html, re.I):
        u = urllib.parse.urljoin(base_url, m.group(1).strip())
        if _is_vid_url(u):
            vids.add(u)
    for m in re.finditer(r'<source[^>]*?src="([^"]+)"[^>]*?type="[^"]*video[^"]*"', html, re.I):
        vids.add(urllib.parse.urljoin(base_url, m.group(1).strip()))
    for m in re.finditer(r'<source[^>]*?type="[^"]*video[^"]*"[^>]*?src="([^"]+)"', html, re.I):
        vids.add(urllib.parse.urljoin(base_url, m.group(1).strip()))

    # 视频：正文里的直接视频链接
    for m in re.finditer(r'href="([^"]+\.(?:mp4|webm|mkv|mov|flv|ts|m3u8))"', html, re.I):
        vids.add(urllib.parse.urljoin(base_url, m.group(1).strip()))

    # 过滤模板/头像噪音
    noise_pat = re.compile(r'(template/|static/image|data/avatar|avatar|logo|smiley|'
                           r'\.gif|\.ico|/images/|icon|bg_|top_|qq_login|pn_|print|userinfo|'
                           r'thread-prev|thread-next|fav|share|report|thumb|/user/|'
                           r'未命名|unnamed|cropped-|default|placeholder|spacer)', re.I)
    imgs = {u for u in imgs if not noise_pat.search(u)}
    # 过滤 WordPress 小尺寸缩略图后缀 -WxH.ext（如 -180x180.png、-32x32.png），
    # 只保留同一基础文件名下尺寸最大的一张。
    _wp_best = {}
    for u in imgs:
        _m = re.search(r'-(\d{2,4})x\d{2,4}(\.(?:jpg|jpeg|png|webp))$', u, re.I)
        if _m:
            _w = int(_m.group(1))
            _base = u[:_m.start()]
            if _w < 300:
                continue  # 小缩略图直接丢弃
            if _base not in _wp_best or _w > _wp_best[_base][0]:
                _wp_best[_base] = (_w, u)
    if _wp_best:
        _wp_keep = {v[1] for v in _wp_best.values()}
        imgs = {u for u in imgs
                if not re.search(r'-\d{2,4}x\d{2,4}\.(?:jpg|jpeg|png|webp)$', u, re.I)
                or u in _wp_keep}

    # 尺寸去重：URL 带 w_xxx 尺寸参数的，保留同一路径下宽度最大的版本，
    # 丢弃明显的小缩略图（宽度 < 300）。
    best = {}
    for u in imgs:
        q = urllib.parse.urlparse(u).query
        mw = re.search(r'(?:^|[^\d])(?:w_|width=|w=)(\d+)', q, re.I)
        w = int(mw.group(1)) if mw else 0
        path_key = urllib.parse.urlparse(u)._replace(query='', fragment='').geturl()
        if w > 0 and w < 300:
            continue  # 小缩略图丢弃
        cur = best.get(path_key)
        if cur is None or w > cur[1]:
            best[path_key] = (u, w)
    if best:
        imgs = {v[0] for v in best.values()}
    return sorted(imgs), sorted(vids)


def get_page_title(html):
    m = re.search(r'<title>(.*?)</title>', html, re.S)
    if not m:
        return None
    t = m.group(1).strip()
    # 去掉 "站点名 - 标题" 前缀/后缀：取分隔后最长的一段（通常是标题）
    parts = re.split(r'\s*[|_-]\s*', t)
    t = max(parts, key=len)
    return clean_title(t)


def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', name)
    return name.strip().strip('.')[:100] or 'untitled'


def grab_list(url, cookie='', proxy='', max_threads=0, page_limit=1, log=None, auto_page=True, stop_event=None):
    """抓取列表页，返回帖子列表 [(url,title,thumb)]。
    page_limit: 翻几页（默认1）。
    auto_page: 为True时，若 page_limit<=1 且页面存在分页(index_N.html 或 -N.html)，
               自动探测总页数并抓完。"""
    def lg(msg):
        if log:
            log(msg)

    fetcher = Fetcher(cookie, proxy, stop_event)
    all_threads = {}

    def _page_url(base, page):
        """按页面格式生成第 page 页 URL"""
        if page <= 1:
            return base
        # 处理 index_N.html 分页
        m = re.search(r'(index_)\d+(\.html)$', base)
        if m:
            return base[:m.start(1)] + 'index_%d.html' % page
        # 处理 -N.html 分页
        m = re.search(r'-(\d+)(\.html)$', base)
        if m:
            return base[:m.start(1)] + '-%d.html' % page
        # 处理 次元岛等: /photo/index/2-1 -> /photo/list/2-1-N
        m = re.search(r'^(.*?)/index/([0-9]+-[0-9]+)$', base)
        if m:
            return m.group(1) + '/list/%s-%d' % (m.group(2), page)
        # 处理 .../list-N.html 或 /xxx/ 尾斜杠形式
        if base.endswith('/'):
            return base + ('index_%d.html' % page)
        return base

    MAX_DETECT_PAGES = 500

    def _detect_pages(html):
        """从列表页探测最大页码（带安全上限，防止把无关大数字当页码）"""
        maxp = 1
        # index_N.html
        for mm in re.finditer(r'index_(\d+)\.html', html):
            _v = int(mm.group(1))
            if _v <= MAX_DETECT_PAGES:
                maxp = max(maxp, _v)
        # -N.html（排除 URL 本身 id-xxx 这种长数字，只认 1-3 位常见页码）
        for mm in re.finditer(r'-(\d+)\.html', html):
            _v = int(mm.group(1))
            if _v <= MAX_DETECT_PAGES:
                maxp = max(maxp, _v)
        # 次元岛等: /list/分类-N-N
        for mm in re.finditer(r'/list/[0-9]+-[0-9]+-(\d+)', html):
            _v = int(mm.group(1))
            if _v <= MAX_DETECT_PAGES:
                maxp = max(maxp, _v)
        return maxp

    if auto_page and page_limit <= 1:
        # 先抓第一页，探测总页数
        try:
            html0 = fetcher.fetch(url)
        except Exception as e:
            lg('列表页失败: %s' % e)
            return []
        for u, t, th in extract_threads(html0, url):
            if u not in all_threads or len(t) > len(all_threads[u][1]):
                all_threads[u] = (u, t, th)
        maxp = _detect_pages(html0)
        if maxp > 1:
            lg('检测到 %d 页，自动翻页抓取' % maxp)
        page_limit = maxp
        # 防呆：探测页数巨大（>50）但第一页没识别到帖子，多半是误判，取消自动翻页
        if page_limit > 50 and not all_threads:
            lg('提示: 检测页数异常（%d 页）且未识别到帖子，取消自动翻页' % page_limit)
            page_limit = 1

    for page in range(1, page_limit + 1):
        if stop_event is not None and stop_event.is_set():
            lg('已停止')
            break
        page_url = _page_url(url, page)
        if page == 1 and all_threads:
            continue  # 第一页已在 auto_page 阶段抓过
        if page > 1:
            lg('抓取列表页: %s' % page_url)
        else:
            lg('抓取列表页: %s' % page_url)
        try:
            html = fetcher.fetch(page_url)
        except Exception as e:
            lg('列表页失败: %s' % e)
            continue
        threads = extract_threads(html, page_url)
        # 诊断：页面内容异常提示
        if len(threads) == 0 and ('验证' in html or '访问过于频繁' in html or '安全验证' in html):
            lg('! 提示: 页面返回了安全验证/风控页，请稍后再试或先点"登录"再抓取')
        for u, t, th in threads:
            if u not in all_threads or len(t) > len(all_threads[u][1]):
                all_threads[u] = (u, t, th)
        if page < page_limit:
            time.sleep(2.0)
    result = list(all_threads.values())
    if max_threads and max_threads > 0:
        result = result[:max_threads]
    return result


def grab_single_page(url, save_dir, cookie='', proxy='', grab_img=True, grab_vid=True, log=None, folder_name=None, progress_cb=None, stop_event=None, title_cb=None):
    """抓取单个网页/图站的图片视频，存到一个文件夹（默认以页面标题命名，也可自定义 folder_name）"""
    def lg(msg):
        if log:
            log(msg)

    fetcher = Fetcher(cookie, proxy, stop_event)
    lg('抓取网页: %s' % url)
    try:
        html = fetcher.fetch(url)
    except Exception as e:
        lg('网页抓取失败: %s' % e)
        raise GrabError('网页抓取失败: %s' % e) from e

    # 文件夹名：优先用自定义名，否则用页面标题
    if folder_name:
        title = folder_name
    else:
        title = get_page_title(html) or 'page'
    folder = os.path.join(save_dir, sanitize_filename(title))
    os.makedirs(folder, exist_ok=True)
    lg('  文件夹名: %s' % title)
    if title_cb:
        title_cb(title)

    imgs, vids = extract_media(html, url)
    first_img = imgs[0] if imgs else ''
    total = (len(imgs) if grab_img else 0) + (len(vids) if grab_vid else 0)
    n_img = n_vid = 0
    done = 0

    if grab_img:
        for i, u in enumerate(imgs, 1):
            if stop_event is not None and stop_event.is_set():
                break
            ext = os.path.splitext(urllib.parse.urlparse(u).path)[1] or '.jpg'
            fp = os.path.join(folder, 'img_%03d%s' % (i, ext))
            try:
                fetcher.download(u, fp, referer=url)
                n_img += 1
                lg('    [图%d/%d] %s' % (i, len(imgs), os.path.basename(fp)))
            except Exception as e:
                lg('    图片下载失败: %s' % e)
            done += 1
            if progress_cb:
                progress_cb(done, total)
            time.sleep(0.2)

    if grab_vid:
        for i, u in enumerate(vids, 1):
            if stop_event is not None and stop_event.is_set():
                break
            path = urllib.parse.urlparse(u).path
            ext = os.path.splitext(path)[1] or '.mp4'
            base = os.path.basename(path) or ('video_%03d' % i)
            fp = os.path.join(folder, base if base.lower().endswith(ext) else 'video_%03d%s' % (i, ext))
            try:
                fetcher.download(u, fp, timeout=20, referer=url)
                n_vid += 1
                lg('    [视频%d/%d] %s' % (i, len(vids), os.path.basename(fp)))
            except Exception as e:
                lg('    视频下载失败: %s' % e)
            done += 1
            if progress_cb:
                progress_cb(done, total)
            time.sleep(0.2)

    return n_img, n_vid, title, first_img


def grab_thread_page(url, save_dir, cookie='', proxy='', grab_img=True, grab_vid=True, log=None, folder_name=None, progress_cb=None, stop_event=None, title_cb=None):
    """抓取单个帖子页：直接用帖子真实标题建文件夹，下载全部图片/视频"""
    def lg(msg):
        if log:
            log(msg)

    fetcher = Fetcher(cookie, proxy, stop_event)
    lg('抓取帖子: %s' % url)
    try:
        html = fetcher.fetch(url)
    except Exception as e:
        lg('帖子页抓取失败: %s' % e)
        raise GrabError('帖子页抓取失败: %s' % e) from e

    # 文件夹名：优先用自定义名，否则用页面 <title>（真实标题）
    if folder_name:
        title = folder_name
    else:
        title = get_page_title(html) or 'post'
    folder = os.path.join(save_dir, sanitize_filename(title))
    os.makedirs(folder, exist_ok=True)
    lg('  文件夹名: %s' % title)
    if title_cb:
        title_cb(title)

    imgs, vids = extract_media(html, url)
    first_img = imgs[0] if imgs else ''
    total = (len(imgs) if grab_img else 0) + (len(vids) if grab_vid else 0)
    n_img = n_vid = 0
    done = 0

    if grab_img:
        for i, u in enumerate(imgs, 1):
            if stop_event is not None and stop_event.is_set():
                break
            ext = os.path.splitext(urllib.parse.urlparse(u).path)[1] or '.jpg'
            fp = os.path.join(folder, 'img_%03d%s' % (i, ext))
            try:
                fetcher.download(u, fp, referer=url)
                n_img += 1
                lg('    [图%d/%d] %s' % (i, len(imgs), os.path.basename(fp)))
            except Exception as e:
                lg('    图片下载失败: %s' % e)
            done += 1
            if progress_cb:
                progress_cb(done, total)
            time.sleep(0.2)

    if grab_vid:
        for i, u in enumerate(vids, 1):
            if stop_event is not None and stop_event.is_set():
                break
            path = urllib.parse.urlparse(u).path
            ext = os.path.splitext(path)[1] or '.mp4'
            base = os.path.basename(path) or ('video_%03d' % i)
            fp = os.path.join(folder, base if base.lower().endswith(ext) else 'video_%03d%s' % (i, ext))
            try:
                fetcher.download(u, fp, timeout=20, referer=url)
                n_vid += 1
                lg('    [视频%d/%d] %s' % (i, len(vids), os.path.basename(fp)))
            except Exception as e:
                lg('    视频下载失败: %s' % e)
            done += 1
            if progress_cb:
                progress_cb(done, total)
            time.sleep(0.2)

    return n_img, n_vid, title, first_img


def grab_thread(url, title, save_dir, cookie='', proxy='', grab_img=True, grab_vid=True, log=None, folder_name=None, progress_cb=None, stop_event=None, title_cb=None):
    """抓取单个帖子：下载图片/视频到 save_dir/文件夹 文件夹。
    folder_name 提供时用自定义名，否则用帖子标题"""
    def lg(msg):
        if log:
            log(msg)

    fetcher = Fetcher(cookie, proxy, stop_event)
    if folder_name:
        title = folder_name
    folder = os.path.join(save_dir, sanitize_filename(title))
    os.makedirs(folder, exist_ok=True)

    lg('  帖子: %s' % title)
    try:
        html = fetcher.fetch(url)
    except Exception as e:
        lg('  帖子页失败: %s' % e)
        raise GrabError('帖子页抓取失败: %s' % e) from e

    imgs, vids = extract_media(html, url)
    first_img = imgs[0] if imgs else ''
    # 真实标题：详情页 <title>（用于抓完后校验更新表格）
    real_title = get_page_title(html) or title
    if title_cb:
        title_cb(real_title)
    total = (len(imgs) if grab_img else 0) + (len(vids) if grab_vid else 0)
    n_img = n_vid = 0
    done = 0

    if grab_img:
        for i, u in enumerate(imgs, 1):
            if stop_event is not None and stop_event.is_set():
                break
            ext = os.path.splitext(urllib.parse.urlparse(u).path)[1] or '.jpg'
            fp = os.path.join(folder, 'img_%03d%s' % (i, ext))
            try:
                fetcher.download(u, fp, referer=url)
                n_img += 1
                lg('    [图%d/%d] %s' % (i, len(imgs), os.path.basename(fp)))
            except Exception as e:
                lg('    图片下载失败: %s' % e)
            done += 1
            if progress_cb:
                progress_cb(done, total)
            time.sleep(0.3)

    if grab_vid:
        for i, u in enumerate(vids, 1):
            if stop_event is not None and stop_event.is_set():
                break
            path = urllib.parse.urlparse(u).path
            ext = os.path.splitext(path)[1] or '.mp4'
            base = os.path.basename(path) or ('video_%03d' % i)
            fp = os.path.join(folder, base if base.lower().endswith(ext) else 'video_%03d%s' % (i, ext))
            try:
                fetcher.download(u, fp, timeout=20, referer=url)
                n_vid += 1
                lg('    [视频%d/%d] %s' % (i, len(vids), os.path.basename(fp)))
            except Exception as e:
                lg('    视频下载失败: %s' % e)
            done += 1
            if progress_cb:
                progress_cb(done, total)
            time.sleep(0.3)

    return n_img, n_vid, real_title, first_img


# ============================================================
# aiart.pics 专用抓取（AI 艺术图库）
# 列表接口 /api/prompts?page=N ; 详情接口 /api/prompts/{id}
# 图片前缀 https://img1.aiart.pics/
# ============================================================
AIART_API = 'https://aiart.pics/api/prompts'
AIART_IMG = 'https://img1.aiart.pics/'


def is_aiart_url(url):
    return 'aiart.pics' in (url or '')


def aiart_fetch_json(url, fetcher):
    """抓 JSON 接口（带重试，容忍偶发超时）"""
    import json
    last = None
    for attempt in range(3):
        try:
            text = fetcher.fetch(url, timeout=25)
            return json.loads(text)
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def grab_aiart(url, save_dir, cookie='', proxy='', grab_img=True, grab_vid=True,
               log=None, max_items=0, progress_cb=None, save_prompt=True):
    """抓取 aiart.pics 图片/视频，每条按标题建文件夹，并保存提示词 txt
    支持 URL 里的 category/tags/model 参数过滤（前端用 ?category=xxx）
    """
    import json
    from urllib.parse import urlparse, parse_qs
    def lg(msg):
        if log:
            log(msg)

    # 解析 URL 过滤参数（前端 ?category=xxx 实际作为 tags 传给接口）
    q = parse_qs(urlparse(url).query)
    tags_param = q.get('tags', [])
    category = q.get('category', [''])[0]
    model = q.get('model', [''])[0]
    sort = q.get('sort', ['latest'])[0]
    tag_list = list(tags_param)
    if tag_list:
        tag_list = tag_list[0].split(',')
    if category:
        tag_list.append(category)

    fetcher = Fetcher(cookie, proxy)
    lg('抓取 aiart.pics 图库')
    if tag_list:
        lg('分类过滤: %s' % ', '.join(tag_list))
    if model:
        lg('模型过滤: %s' % model)

    # 翻页收集列表（接口用 offset 分页，前端每页50）
    items = []
    offset = 0
    page_size = 50
    while True:
        params = []
        if tag_list:
            params.append('tags=' + urllib.parse.quote(','.join(tag_list)))
        if model:
            params.append('model=' + urllib.parse.quote(model))
        params.append('sort=' + urllib.parse.quote(sort))
        params.append('limit=%d' % page_size)
        params.append('offset=%d' % offset)
        url_full = AIART_API + '?' + '&'.join(params)
        try:
            data = aiart_fetch_json(url_full, fetcher)
        except Exception as e:
            lg('列表offset=%d失败: %s' % (offset, e))
            break
        batch = data.get('prompts') or []
        items.extend(batch)
        total = data.get('total') or 0
        lg('列表+%d 条 (已收集 %d, 该分类总数 %d)' % (len(batch), len(items), total))
        if max_items and max_items > 0 and len(items) >= max_items:
            items = items[:max_items]
            break
        if not batch:
            break
        if len(items) >= total:
            break
        offset += page_size

    if not items:
        lg('未获取到任何条目')
        return 0, 0

    n_img = n_vid = 0
    for idx, it in enumerate(items, 1):
        if progress_cb:
            progress_cb(idx, len(items))
        pid = it.get('id')
        title_obj = it.get('title') or {}
        title = title_obj.get('zh') or title_obj.get('en') or pid
        lg('[%d/%d] %s' % (idx, len(items), title))
        folder = os.path.join(save_dir, sanitize_filename(title))
        os.makedirs(folder, exist_ok=True)

        # 获取详情（含提示词）
        detail = None
        if pid:
            try:
                d = aiart_fetch_json('%s/%s' % (AIART_API, pid), fetcher)
                if d.get('success'):
                    detail = d.get('data')
            except Exception as e:
                lg('  详情失败: %s' % e)

        info = detail or it

        # 保存提示词 txt
        if save_prompt:
            prompts = info.get('prompts') or []
            lines = []
            lines.append('标题: %s' % (title_obj.get('zh') or title_obj.get('en') or ''))
            lines.append('EN: %s' % (title_obj.get('en') or ''))
            lines.append('模型: %s' % (info.get('model') or ''))
            lines.append('标签: %s' % (', '.join(info.get('tags') or [])))
            lines.append('作者: %s' % ((info.get('author') or {}).get('name') or ''))
            lines.append('来源: %s' % (info.get('originUrl') or ''))
            lines.append('')
            lines.append('--- 提示词 ---')
            lines.extend(prompts)
            desc = info.get('description') or {}
            if desc.get('zh') or desc.get('en'):
                lines.append('')
                lines.append('--- 描述 ---')
                lines.append(desc.get('zh') or desc.get('en'))
            try:
                with open(os.path.join(folder, '提示词.txt'), 'w', encoding='utf-8') as f:
                    f.write('\n'.join(lines))
            except Exception as e:
                lg('  提示词保存失败: %s' % e)

        # 下载图片
        if grab_img:
            for j, img in enumerate(info.get('images') or [], 1):
                path = img.get('path') or img.get('sPath')
                if not path:
                    continue
                u = path if path.startswith('http') else (AIART_IMG + path)
                ext = os.path.splitext(u.split('?')[0])[1] or '.jpg'
                fp = os.path.join(folder, 'img_%03d%s' % (j, ext))
                try:
                    fetcher.download(u, fp, referer='https://aiart.pics/')
                    n_img += 1
                    lg('    [图%d] %s' % (j, os.path.basename(fp)))
                except Exception as e:
                    lg('    图片失败: %s' % e)
                time.sleep(0.15)

        # 下载视频
        if grab_vid:
            for j, v in enumerate(info.get('videos') or [], 1):
                u = v.get('url') or v.get('src')
                if not u:
                    continue
                u = u if u.startswith('http') else (AIART_IMG + u)
                ext = os.path.splitext(u.split('?')[0])[1] or '.mp4'
                fp = os.path.join(folder, 'video_%03d%s' % (j, ext))
                try:
                    fetcher.download(u, fp, timeout=30, referer='https://aiart.pics/')
                    n_vid += 1
                    lg('    [视频%d] %s' % (j, os.path.basename(fp)))
                except Exception as e:
                    lg('    视频失败: %s' % e)
                time.sleep(0.15)

    return n_img, n_vid


def grab_aiart_collections(url, save_dir, cookie='', proxy='', grab_img=True, grab_vid=True,
                           log=None, max_collections=0, max_items=0, progress_cb=None,
                           save_prompt=True):
    """抓取 aiart.pics 精选合集(/collections)：列出所有合集，每个合集抓取全部作品
    目录结构: 保存目录/合集名/作品标题/图片...  （即用户要的"所有二级目录"）
    """
    import json
    from urllib.parse import urlparse, parse_qs
    def lg(msg):
        if log:
            log(msg)

    fetcher = Fetcher(cookie, proxy)
    lg('抓取 aiart.pics 精选合集')
    # 1) 列出所有合集
    collections = []
    offset = 0
    while True:
        try:
            data = aiart_fetch_json(
                'https://aiart.pics/api/collections?offset=%d&limit=50' % offset, fetcher)
        except Exception as e:
            lg('合集列表失败: %s' % e)
            break
        batch = data.get('collections') or []
        collections.extend(batch)
        total = data.get('total') or 0
        if not batch or len(collections) >= total:
            break
        offset += 50
    lg('共 %d 个合集' % len(collections))
    if max_collections and max_collections > 0:
        collections = collections[:max_collections]

    n_img = n_vid = 0
    for ci, coll in enumerate(collections, 1):
        if progress_cb:
            progress_cb(ci, len(collections))
        cname_obj = coll.get('name') or {}
        cname = cname_obj.get('zh') or cname_obj.get('en') or '合集%d' % ci
        lg('[合集%d/%d] %s' % (ci, len(collections), cname))
        coll_folder = os.path.join(save_dir, sanitize_filename(cname))
        os.makedirs(coll_folder, exist_ok=True)

        # 2) 获取合集详情拿到 promptIds
        cid = coll.get('id')
        prompt_ids = []
        if cid:
            try:
                cd = aiart_fetch_json(
                    'https://aiart.pics/api/collections/%s' % cid, fetcher)
                prompt_ids = (cd.get('collection') or {}).get('promptIds') or []
            except Exception as e:
                lg('  合集详情失败: %s' % e)
        if not prompt_ids:
            prompt_ids = [p.get('id') for p in (coll.get('prompts') or []) if p.get('id')]
        lg('  该合集含 %d 个作品' % len(prompt_ids))

        # 3) 逐个作品下载
        for idx, pid in enumerate(prompt_ids, 1):
            if max_items and max_items > 0 and idx > max_items:
                break
            try:
                d = aiart_fetch_json('%s/%s' % (AIART_API, pid), fetcher)
                if not d.get('success'):
                    lg('  作品%s详情失败' % pid)
                    continue
                info = d.get('data')
            except Exception as e:
                lg('  作品%s详情失败: %s' % (pid, e))
                continue
            t_obj = info.get('title') or {}
            title = t_obj.get('zh') or t_obj.get('en') or pid
            folder = os.path.join(coll_folder, sanitize_filename(title))
            os.makedirs(folder, exist_ok=True)

            # 保存提示词
            if save_prompt:
                lines = []
                lines.append('标题: %s' % (t_obj.get('zh') or t_obj.get('en') or ''))
                lines.append('EN: %s' % (t_obj.get('en') or ''))
                lines.append('模型: %s' % (info.get('model') or ''))
                lines.append('标签: %s' % (', '.join(info.get('tags') or [])))
                lines.append('作者: %s' % ((info.get('author') or {}).get('name') or ''))
                lines.append('来源: %s' % (info.get('originUrl') or ''))
                lines.append('')
                lines.append('--- 提示词 ---')
                lines.extend(info.get('prompts') or [])
                try:
                    with open(os.path.join(folder, '提示词.txt'), 'w', encoding='utf-8') as f:
                        f.write('\n'.join(lines))
                except Exception as e:
                    lg('    提示词保存失败: %s' % e)

            # 下载图片
            if grab_img:
                for j, img in enumerate(info.get('images') or [], 1):
                    path = img.get('path') or img.get('sPath')
                    if not path:
                        continue
                    u = path if path.startswith('http') else (AIART_IMG + path)
                    ext = os.path.splitext(u.split('?')[0])[1] or '.jpg'
                    fp = os.path.join(folder, 'img_%03d%s' % (j, ext))
                    try:
                        fetcher.download(u, fp, referer='https://aiart.pics/')
                        n_img += 1
                    except Exception as e:
                        lg('    图片失败: %s' % e)
                    time.sleep(0.15)

            # 下载视频
            if grab_vid:
                for j, v in enumerate(info.get('videos') or [], 1):
                    u = v.get('url') or v.get('src')
                    if not u:
                        continue
                    u = u if u.startswith('http') else (AIART_IMG + u)
                    ext = os.path.splitext(u.split('?')[0])[1] or '.mp4'
                    fp = os.path.join(folder, 'video_%03d%s' % (j, ext))
                    try:
                        fetcher.download(u, fp, timeout=30, referer='https://aiart.pics/')
                        n_vid += 1
                    except Exception as e:
                        lg('    视频失败: %s' % e)
                    time.sleep(0.15)
            lg('    [%d/%d] %s (图%d 视频%d)' % (idx, len(prompt_ids), title,
                                                  len(info.get('images') or []),
                                                  len(info.get('videos') or [])))

    return n_img, n_vid


def is_aiart_collections_url(url):
    return 'aiart.pics' in (url or '') and 'collection' in (url or '').lower()


def _extract_site_links(html, base_url, host):
    """提取页面内同域名的普通页面链接（去静态资源/锚点）"""
    links = set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"', html, re.I):
        href = m.group(1).strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:')):
            continue
        full = urllib.parse.urljoin(base_url, href)
        p = urllib.parse.urlparse(full)
        if p.scheme not in ('http', 'https'):
            continue
        if p.netloc.replace('www.', '').lower() != host.replace('www.', '').lower():
            continue
        full = urllib.parse.urldefrag(full)[0]
        if re.search(r'\.(jpg|jpeg|png|gif|webp|css|js|ico|svg|zip|rar|pdf|mp4|mp3)$', full, re.I):
            continue
        links.add(full)
    return links


def grab_site(url, save_dir, cookie='', proxy='', grab_img=True, grab_vid=True,
              log=None, page_limit=0, progress_cb=None, stop_flag=None, max_pages=300, stop_event=None):
    """全站抓取：从入口 URL 出发 BFS 遍历同域名所有页面，
    每页按「站点名/页面标题」分文件夹保存图片/视频。
    page_limit: 最多抓取页面数，0=全部（有 max_pages 安全上限防止失控）。
    stop_flag: 可选 threading.Event，置位后停止。
    返回 (页面数, 文件总数)"""
    def lg(msg):
        if log:
            log(msg)

    # 站点名 = 域名
    p0 = urllib.parse.urlparse(url)
    site_name = p0.netloc.replace('www.', '') or 'site'
    base_dir = os.path.join(save_dir, sanitize_filename(site_name))
    os.makedirs(base_dir, exist_ok=True)

    fetcher = Fetcher(cookie, proxy, stop_event)
    visited = set()
    queue = [url]
    pages = 0
    total_files = 0
    limit = page_limit if page_limit > 0 else max_pages
    lg('全站抓取模式: 站点 %s（最多 %d 页）' % (site_name, limit))

    while queue and pages < limit:
        if stop_flag is not None and stop_flag.is_set():
            lg('已停止')
            break
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)

        try:
            html = fetcher.fetch(cur, timeout=20)
        except Exception as e:
            lg('页面抓取失败(%s): %s' % (cur, e))
            continue

        title = get_page_title(html) or ('page_%d' % (pages + 1))
        page_dir = os.path.join(base_dir, sanitize_filename(title))
        try:
            imgs, vids = extract_media(html, cur)
        except Exception:
            imgs, vids = [], []
        os.makedirs(page_dir, exist_ok=True)
        n = 0
        if grab_img:
            for i, u in enumerate(imgs, 1):
                if stop_event is not None and stop_event.is_set():
                    break
                ext = os.path.splitext(urllib.parse.urlparse(u).path)[1] or '.jpg'
                fp = os.path.join(page_dir, 'img_%03d%s' % (i, ext))
                try:
                    fetcher.download(u, fp, referer=cur)
                    n += 1
                except Exception as e:
                    lg('    图片失败: %s' % e)
                time.sleep(0.2)
        if grab_vid:
            for i, u in enumerate(vids, 1):
                if stop_event is not None and stop_event.is_set():
                    break
                path = urllib.parse.urlparse(u).path
                ext = os.path.splitext(path)[1] or '.mp4'
                base = os.path.basename(path) or ('video_%03d' % i)
                fp = os.path.join(page_dir, base if base.lower().endswith(ext) else 'video_%03d%s' % (i, ext))
                try:
                    fetcher.download(u, fp, timeout=20, referer=cur)
                    n += 1
                except Exception as e:
                    lg('    视频失败: %s' % e)
                time.sleep(0.2)
        pages += 1
        total_files += n
        lg('  [页%d/%d] %s -> %d 个文件' % (pages, limit, title, n))
        if progress_cb:
            progress_cb(pages, len(visited))

        # BFS 发现新页面
        new_links = _extract_site_links(html, cur, p0.netloc)
        for lk in new_links:
            if lk not in visited and lk not in queue:
                queue.append(lk)

    lg('全站抓取完成: 共 %d 页, %d 个文件' % (pages, total_files))
    return pages, total_files




if __name__ == '__main__':
    # 简单命令行自测
    def log(m):
        print(m)

    threads = grab_list('https://bbs.3dmgame.com/forum-283-1.html', max_threads=3, log=log)
    print('找到帖子:', len(threads))
    for u, t in threads[:5]:
        print('  ', t, '->', u)
    if threads:
        grab_thread(threads[0][0], threads[0][1],
                    r'C:\Users\wenfeima\Doubao\chats\2026-09-04\new-chat\test_dl',
                    log=log)
