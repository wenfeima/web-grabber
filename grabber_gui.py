# -*- coding: utf-8 -*-
"""通用网页图片/视频抓取工具 - 主程序 (GUI)"""
# 版本号：每次修改后递增，用于界面标题区分版本
APP_VERSION = 'v39'
import os
import re
import sys
import json
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageTk
import io as _io
import time

# 确保能 import 核心逻辑
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import webgrab_core as core

# exe 模式下用 exe 所在目录（这样 config.json 保存在用户可见位置，能持久化）
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOGIN_COOKIE_FILE = os.path.join(APP_DIR, 'login_cookie.txt')
CONFIG_FILE = os.path.join(APP_DIR, 'grabber_config.json')
# 登录窗口：exe 模式用打包的 exe，源码模式用 .py
if getattr(sys, 'frozen', False):
    LOGIN_HELPER = os.path.join(os.path.dirname(sys.executable), '登录窗口.exe')
else:
    LOGIN_HELPER = os.path.join(APP_DIR, 'login_window.py')


def load_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class GrabberApp:
    def __init__(self, root):
        self.root = root
        self.root.title('通用网页图片/视频抓取工具 %s' % APP_VERSION)
        self.root.geometry('920x780')
        self.root.minsize(840, 700)

        self.cookie = ''
        self.log_queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.tasks = {}        # iid -> dict(url,title,img_done,img_total,folder)
        self.total_files = 0   # 本次任务文件总数
        self.done_files = 0    # 已下载文件数
        self._task_q = queue.Queue()
        self._thumb_q = queue.Queue()
        self.thumb_refs = {}   # iid -> PhotoImage 引用，防止被GC
        self._preview_threads = None  # 预览出的帖子列表 [(url,title,thumb)]
        self._preview_url = None      # 预览对应的网址
        self._preview_mode = None     # 预览对应的模式
        self._preview_q = queue.Queue()  # 预览结果队列（主线程填表用）
        self._ui_q = queue.Queue()        # UI 操作队列（reset/add 等，跨线程转主线程）

        self.cfg = load_config()

        self._build_ui()
        self._load_settings()
        self._poll_log()
        self.root.after(200, self._process_task_updates)
        self.root.after(150, self._poll_thumbs)

    # ---------------- 配置加载/保存 ----------------
    def _load_settings(self):
        """从配置文件恢复上次的设置"""
        self.proxy_var.set(self.cfg.get('proxy', ''))
        self.proxy_enabled.set(bool(self.cfg.get('proxy_enabled', False)))
        self.url_var.set(self.cfg.get('url', ''))
        # 默认保存到程序目录下的 TU 文件夹
        default_dir = os.path.join(APP_DIR, 'TU')
        try:
            os.makedirs(default_dir, exist_ok=True)
        except Exception:
            pass
        self.dir_var.set(self.cfg.get('save_dir') or default_dir)
        self.thread_var.set(str(self.cfg.get('threads', 4)))
        self.limit_var.set(str(self.cfg.get('limit', 0)))
        self.grab_img.set(bool(self.cfg.get('grab_img', True)))
        self.grab_vid.set(bool(self.cfg.get('grab_vid', True)))
        self.mode_var.set(self.cfg.get('mode', '自动'))
        self.engine_var.set(self.cfg.get('engine', '自动'))
        self._on_proxy_toggle()
        self._on_engine_change()
        self._update_url_list()

    def _save_settings(self):
        """保存当前设置到配置文件"""
        self.cfg.update({
            'proxy': self.proxy_var.get().strip(),
            'proxy_enabled': bool(self.proxy_enabled.get()),
            'url': self.url_var.get().strip(),
            'save_dir': self.dir_var.get().strip(),
            'threads': int(self.thread_var.get() or 4),
            'limit': int(self.limit_var.get() or 0),
            'grab_img': bool(self.grab_img.get()),
            'grab_vid': bool(self.grab_vid.get()),
            'mode': self.mode_var.get(),
            'engine': self.engine_var.get(),
        })
        save_config(self.cfg)

    def _on_proxy_toggle(self):
        """代理开关：启用时可用输入框，关闭时置灰"""
        if self.proxy_enabled.get():
            self.proxy_entry.configure(state='normal')
        else:
            self.proxy_entry.configure(state='disabled')

    def _on_engine_change(self):
        """下载引擎切换：auto 时检测外部引擎并提示"""
        eng = self.engine_var.get()
        core.Fetcher.engine = {
            '自动': 'auto', '内置': 'internal', 'IDM': 'idm', 'aria2': 'aria2'
        }.get(eng, 'auto')
        # auto 模式下立即探测一次，把结果写到配置显示
        try:
            core.Fetcher.engine_info = None
            if eng == '自动':
                e, p, d = core.Fetcher.detect_engine()
                core.Fetcher.engine_info = {'engine': e, 'path': p, 'desc': d}
                if hasattr(self, 'engine_hint'):
                    if e:
                        self.engine_hint.configure(
                            text='  检测到: %s' % d, foreground='#2a7')
                    else:
                        self.engine_hint.configure(
                            text='  未检测到 aria2/IDM，使用内置下载', foreground='#888')
            elif hasattr(self, 'engine_hint'):
                if eng == '内置':
                    self.engine_hint.configure(text='  使用内置下载（多线程分段）', foreground='#2a7')
                else:
                    _p = core.Fetcher.engine_path()
                    if _p:
                        self.engine_hint.configure(
                            text='  使用 %s (%s)' % (eng, os.path.basename(_p)), foreground='#2a7')
                    else:
                        self.engine_hint.configure(
                            text='  未找到 %s，将回退内置下载' % eng, foreground='#c60')
        except Exception:
            pass

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {'padx': 10, 'pady': 5}
        # 大号按钮样式
        st = ttk.Style()
        st.configure('Big.TButton', font=('Microsoft YaHei UI', 13, 'bold'),
                     padding=(24, 10))
        # 外层容器，顶部留出间距
        outer = ttk.Frame(self.root, padding=(15, 15, 15, 10))
        outer.pack(fill='both', expand=True)

        # 顶部标题
        ttk.Label(outer, text='通用网页图片/视频抓取工具 %s' % APP_VERSION,
                  font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(0, 10))

        # 主内容区（单页：配置 + 表格 + 日志全部在一页）
        frm = ttk.Frame(outer)
        frm.pack(fill='both', expand=True)
        frm.columnconfigure(1, weight=1)

        # ---- 第1行：网址 ----
        ttk.Label(frm, text='网址:').grid(row=0, column=0, sticky='w', **pad)
        self.url_var = tk.StringVar()
        self.url_combo = ttk.Combobox(frm, textvariable=self.url_var, width=66)
        self.url_combo.grid(row=0, column=1, columnspan=3, sticky='we', **pad)
        ttk.Button(frm, text='收藏', command=self._fav_url).grid(row=0, column=4, **pad)
        # 提示文字（仅一行，不重叠）
        ttk.Label(frm, text='例: https://bbs.3dmgame.com/forum-283-1.html（论坛列表页） 或 https://xxx.com/pic.html（普通图片页）',
                  foreground='#888').grid(row=1, column=1, columnspan=4, sticky='w', padx=10, pady=(0, 8))

        # ---- 第2行：保存位置 ----
        ttk.Label(frm, text='保存位置:').grid(row=2, column=0, sticky='w', **pad)
        self.dir_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.dir_var, width=55).grid(
            row=2, column=1, columnspan=3, sticky='we', **pad)
        ttk.Button(frm, text='选择...', command=self._choose_dir).grid(
            row=2, column=4, **pad)

        # ---- 第3行：代理 ----
        self.proxy_enabled = tk.BooleanVar(value=False)
        self.proxy_var = tk.StringVar()
        ttk.Checkbutton(frm, text='启用代理', variable=self.proxy_enabled,
                        command=self._on_proxy_toggle).grid(
            row=3, column=0, sticky='w', **pad)
        self.proxy_entry = ttk.Entry(frm, textvariable=self.proxy_var, width=55)
        self.proxy_entry.grid(row=3, column=1, columnspan=3, sticky='we', **pad)
        ttk.Label(frm, text='例: http://127.0.0.1:10809', foreground='#888').grid(
            row=3, column=4, sticky='w', **pad)

        # ---- 第4行：选项 ----
        opt = ttk.Frame(frm)
        opt.grid(row=4, column=0, columnspan=5, sticky='w', **pad)
        ttk.Label(opt, text='模式:').pack(side='left', padx=(5, 2))
        self.mode_var = tk.StringVar(value='自动')
        ttk.Combobox(opt, textvariable=self.mode_var, state='readonly', width=12,
                     values=('自动', '单个网页', '论坛列表页', '全站抓取')).pack(side='left', padx=2)

        self.browser_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text='浏览器模式', variable=self.browser_mode,
                        command=self._on_browser_mode).pack(side='left', padx=8)

        # 超链接：一键启动调试模式的 Edge（9222 端口，保留油猴/登录态）
        self.edge_link = tk.Label(opt, text='启动调试浏览器', foreground='#1a6fd4',
                                  cursor='hand2', font=('Microsoft YaHei UI', 9, 'underline'))
        self.edge_link.pack(side='left', padx=(0, 8))
        self.edge_link.bind('<Button-1>', lambda e: self._start_debug_edge())
        # 超链接：独立窗口打开 Edge（登录/装插件用，同一个profile）
        self.edge_solo_link = tk.Label(opt, text='独立窗口打开(登录/装插件)', foreground='#1a6fd4',
                                        cursor='hand2', font=('Microsoft YaHei UI', 9, 'underline'))
        self.edge_solo_link.pack(side='left', padx=(0, 8))
        self.edge_solo_link.bind('<Button-1>', lambda e: self._open_edge_solo())

        # 第二行：抓取选项 + 线程数 + 页码
        opt2 = ttk.Frame(frm)
        opt2.grid(row=5, column=0, columnspan=5, sticky='w', **pad)
        self.grab_img = tk.BooleanVar(value=True)
        self.grab_vid = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt2, text='抓取图片', variable=self.grab_img).pack(side='left', padx=8)
        ttk.Checkbutton(opt2, text='抓取视频', variable=self.grab_vid).pack(side='left', padx=8)

        ttk.Label(opt2, text='  线程数:').pack(side='left', padx=(12, 2))
        self.thread_var = tk.StringVar(value='4')
        ttk.Spinbox(opt2, from_=1, to=20, textvariable=self.thread_var, width=4).pack(side='left')

        ttk.Label(opt2, text='  页码(0=全部):').pack(side='left', padx=(12, 2))
        self.limit_var = tk.StringVar(value='0')
        ttk.Spinbox(opt2, from_=0, to=5000, textvariable=self.limit_var, width=6).pack(side='left')

        ttk.Label(opt2, text='  引擎:').pack(side='left', padx=(12, 2))
        self.engine_var = tk.StringVar(value='自动')
        self.engine_combo = ttk.Combobox(opt2, textvariable=self.engine_var, state='readonly', width=6,
                                         values=('自动', '内置', 'IDM', 'aria2'))
        self.engine_combo.pack(side='left', padx=2)
        self.engine_combo.bind('<<ComboboxSelected>>', lambda e: self._on_engine_change())
        self.engine_hint = ttk.Label(opt2, text='', foreground='#888')
        self.engine_hint.pack(side='left', padx=(6, 0))

        # ---- 第6行：按钮 + 登录状态提示灯 ----
        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=5, sticky='we', **pad)
        # 左侧：辅助按钮
        ttk.Button(btns, text='登录（可选）', command=self._open_login).pack(side='left', padx=5)
        # 登录状态提示灯（绿=已登录，红=未登录）
        self.login_light = tk.Canvas(btns, width=16, height=16, highlightthickness=0)
        self.login_light.pack(side='left', padx=(0, 8))
        self._draw_login_light(False)
        ttk.Button(btns, text='清除登录', command=self._clear_login).pack(side='left', padx=5)
        ttk.Button(btns, text='停止', command=self._stop_grab).pack(side='left', padx=5)
        ttk.Button(btns, text='预览列表', command=self._preview_list).pack(side='left', padx=5)
        # 中间弹性空白，把开始抓取推到右侧
        ttk.Frame(btns).pack(side='left', expand=True, fill='x')
        # 右侧：大号「开始抓取」按钮，独立方便点击
        big_btn = ttk.Button(btns, text='开始抓取', command=self._start_grab)
        big_btn.pack(side='right', padx=10, pady=4)
        big_btn.configure(style='Big.TButton')

        # ---- 第6行：主内容区分页：任务列表 / 运行日志 ----
        self.nb = ttk.Notebook(frm)
        nb = self.nb
        nb.grid(row=7, column=0, columnspan=6, sticky='nsew', padx=5, pady=4)
        frm.rowconfigure(7, weight=1)

        # --- 页1：任务列表（表格 + 统计栏） ---
        tab_list = ttk.Frame(nb)
        nb.add(tab_list, text='任务列表')
        ttk.Label(tab_list, text='（双击标题=打开网页, 双击进度=打开图片）').pack(anchor='w', padx=6, pady=(6, 2))
        ttk.Style().configure('Treeview', rowheight=52)
        cols = ('no', 'date', 'title', 'progress', 'status')
        self.tree = ttk.Treeview(tab_list, columns=cols, show='tree headings', height=8)
        self.tree.heading('no', text='序号')
        self.tree.heading('date', text='日期')
        self.tree.heading('title', text='标题')
        self.tree.heading('progress', text='进度')
        self.tree.heading('status', text='状态')
        self.tree.column('#0', width=60, anchor='center')   # 树列放缩略图
        self.tree.column('no', width=50, anchor='center')
        self.tree.column('date', width=90, anchor='center')
        self.tree.column('title', width=400, anchor='w')
        self.tree.column('progress', width=130, anchor='center')
        self.tree.column('status', width=90, anchor='center')
        # 滚动条在右侧，表格占满剩余
        tscr = ttk.Scrollbar(tab_list, orient='vertical', command=self.tree.yview)
        tscr.pack(side='right', fill='y', pady=2)
        self.tree.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=2)
        self.tree.configure(yscrollcommand=tscr.set)
        self.tree.bind('<Double-1>', self._on_tree_double)

        # 统计栏放在任务列表页底部
        stat = ttk.Frame(tab_list)
        stat.pack(fill='x', padx=6, pady=(2, 6))
        self.stat_total = tk.StringVar(value='图片总数 0')
        self.stat_done = tk.StringVar(value='已下载 0')
        self.stat_list = tk.StringVar(value='总列表数 0')
        self.stat_ok = tk.StringVar(value='访问完成 0')
        self.stat_speed = tk.StringVar(value='0 KB/s')
        ttk.Label(stat, textvariable=self.stat_total).pack(side='left', padx=10)
        ttk.Label(stat, textvariable=self.stat_done).pack(side='left', padx=10)
        ttk.Label(stat, textvariable=self.stat_list).pack(side='left', padx=10)
        ttk.Label(stat, textvariable=self.stat_ok).pack(side='left', padx=10)
        ttk.Label(stat, textvariable=self.stat_speed).pack(side='left', padx=10)

        # --- 页2：运行日志（独占一页，完整显示） ---
        tab_log = ttk.Frame(nb)
        nb.add(tab_log, text='运行日志')
        ttk.Label(tab_log, text='运行日志:').pack(anchor='w', padx=6, pady=(6, 2))
        self.log = scrolledtext.ScrolledText(tab_log, height=12, state='disabled',
                                             font=('Consolas', 9))
        self.log.pack(fill='both', expand=True, padx=6, pady=(0, 6))

        # --- 页3：浏览器（内嵌调试 Edge，放在日志后面） ---
        tab_brw = tk.Frame(nb)
        nb.add(tab_brw, text='浏览器')
        self.tab_brw_host = tk.Frame(tab_brw, bg='#2b2b2b')
        self.tab_brw_host.pack(fill='both', expand=True, padx=2, pady=2)
        self.tab_brw_hint = ttk.Label(tab_brw,
                                      text='尚未启动调试浏览器——点上方「启动调试浏览器」，Edge 会自动嵌入此区域',
                                      foreground='#888')
        self.tab_brw_hint.place(relx=0.5, rely=0.5, anchor='center')
        self.tab_brw_host.bind('<Configure>', self._on_host_resize)

    def _choose_dir(self):
        d = filedialog.askdirectory(title='选择保存位置')
        if d:
            self.dir_var.set(d)

    # ---------------- 网址历史/收藏 ----------------
    def _fav_url(self):
        """收藏当前网址"""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo('提示', '请先填写网址再收藏')
            return
        favs = self.cfg.get('fav_urls', [])
        if url not in favs:
            favs.insert(0, url)
            self.cfg['fav_urls'] = favs
            save_config(self.cfg)
            self._update_url_list()
            self._log('已收藏: %s' % url)
        else:
            self._log('该网址已在收藏中')

    def _update_url_list(self):
        """刷新下拉列表：收藏 + 历史"""
        items = []
        favs = self.cfg.get('fav_urls', [])
        history = self.cfg.get('url_history', [])
        for u in favs:
            items.append('★ ' + u)
        for u in history:
            if u not in favs:
                items.append(u)
        self.url_combo['values'] = items

    def _record_url_history(self):
        """把当前网址记入历史（去重，保留最近20条）"""
        url = self.url_var.get().strip()
        if not url:
            return
        history = self.cfg.get('url_history', [])
        if url in history:
            history.remove(url)
        history.insert(0, url)
        self.cfg['url_history'] = history[:20]
        save_config(self.cfg)
        self._update_url_list()

    # ---------------- 日志 ----------------
    def _log(self, msg):
        self.log_queue.put(msg)

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log.configure(state='normal')
                self.log.insert('end', msg + '\n')
                self.log.see('end')
                self.log.configure(state='disabled')
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log)

    # ---------------- 登录 ----------------
    def _draw_login_light(self, logged_in):
        '''画登录状态提示灯：绿=已登录，红=未登录'''
        self.login_light.delete('all')
        color = '#22c55e' if logged_in else '#ef4444'
        self.login_light.create_oval(2, 2, 14, 14, fill=color, outline=color)
        self.login_light.create_oval(4, 4, 8, 8, fill='white', outline='')

    def _open_login(self):
        url = self.url_var.get().strip()
        if url.startswith('★ '):
            url = url[2:].strip()
        if not url:
            messagebox.showinfo('提示', '请先在"列表页网址"里填目标网站的网址\n登录窗口会打开该网站，登录后自动保存登录状态')
            return
        # 取网站根域名
        try:
            from urllib.parse import urlparse
            p = urlparse(url)
            root = '%s://%s' % (p.scheme, p.netloc)
        except Exception:
            root = url
        # 浏览器模式已启动：直接在内嵌浏览器里打开网址，登录态自动保存在 Edge profile
        try:
            import cdp_browser
            if cdp_browser.is_connected():
                cdp_browser.open_url(root)
                self._log('已在内嵌浏览器打开: %s（登录后直接抓取即可，登录态自动保留）' % root)
                try:
                    self.nb.select(2)
                except Exception:
                    pass
                return
        except Exception as e:
            self._log('内嵌浏览器打开失败，回退独立登录窗口: %s' % e)

        # 浏览器模式未启动：用原来的独立登录窗口（兼容直连模式）
        self._log('打开登录窗口: %s' % root)
        try:
            if getattr(sys, 'frozen', False):
                subprocess.Popen([LOGIN_HELPER, root, LOGIN_COOKIE_FILE],
                                 cwd=os.path.dirname(LOGIN_HELPER),
                                 creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
            else:
                subprocess.Popen([sys.executable, LOGIN_HELPER, root, LOGIN_COOKIE_FILE],
                                 cwd=APP_DIR, creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
        except Exception as e:
            self._log('登录窗口启动失败: %s' % e)
        self._wait_cookie()

    def _wait_cookie(self, tries=40):
        def check():
            if os.path.exists(LOGIN_COOKIE_FILE):
                try:
                    with open(LOGIN_COOKIE_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if data.get('cookie'):
                        self.cookie = data['cookie']
                        domain = data.get('domain', '')
                        self._draw_login_light(True)
                        self._log('已获取登录状态: %s' % domain)
                        return
                except Exception:
                    pass
            self.root.after(500, check)
        check()

    def _clear_login(self):
        self.cookie = ''
        if os.path.exists(LOGIN_COOKIE_FILE):
            try:
                os.remove(LOGIN_COOKIE_FILE)
            except Exception:
                pass
        self._draw_login_light(False)
        self._log('已清除登录状态')

    # ---------------- 抓取 ----------------
    def _preview_list(self):
        """预览列表：只抓列表页识别帖子，填到表格（状态=待下载），不下载图片内容"""
        if self.worker and self.worker.is_alive():
            messagebox.showwarning('提示', '抓取正在进行中')
            return
        url = self.url_var.get().strip()
        if url.startswith('★ '):
            url = url[2:].strip()
            self.url_var.set(url)
        if not url:
            messagebox.showwarning('提示', '请先填写网址')
            return
        mode = self.mode_var.get()
        try:
            limit = max(0, int(self.limit_var.get()))
        except ValueError:
            limit = 0
        proxy = self.proxy_var.get().strip() if self.proxy_enabled.get() else ''
        self._log('开始预览列表...')
        # 清空旧预览/旧任务
        self._reset_tasks()
        self._preview_threads = None
        self._preview_url = None
        self._preview_mode = None
        def lg(m):
            self._log(m)
        def run():
            try:
                # aiart 专用
                if core.is_aiart_url(url):
                    self._log('aiart.pics 走专用抓取，不支持列表预览')
                    return
                if mode == '全站抓取':
                    # 全站模式也先识别列表
                    tl = core.grab_list(url, cookie=self.cookie, proxy=proxy,
                                        page_limit=limit if limit > 0 else 1,
                                        auto_page=(limit == 0), log=lg,
                                        stop_event=self.stop_flag)
                elif mode in ('论坛列表页', '自动'):
                    tl = core.grab_list(url, cookie=self.cookie, proxy=proxy,
                                        page_limit=limit if limit > 0 else 1,
                                        auto_page=(limit == 0), log=lg,
                                        stop_event=self.stop_flag)
                else:
                    # 单个网页等：预览意义不大，直接提示
                    self._log('当前模式不支持列表预览，请用"论坛列表页"或"全站抓取"')
                    return
                if not tl:
                    self._log('未识别到帖子，请检查网址或模式')
                    return
                self._preview_threads = tl
                self._preview_url = url
                self._preview_mode = mode
                # 填表（转主线程，避免 tkinter 跨线程——after 必须主线程调，这里用队列）
                self._preview_q.put(tl)
                self._log('预览完成: 共 %d 个帖子（状态=待下载），点"开始抓取"即开始下载' % len(tl))
            except Exception as e:
                self._log('预览失败: %s' % e)
        threading.Thread(target=run, daemon=True).start()

    def _fill_preview_table(self, tl):
        """主线程填预览表格"""
        self._reset_tasks()
        for i, (u, t, th) in enumerate(tl, 1):
            self._add_task(u, t, th, status='待下载')

    def _start_grab(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning('提示', '抓取正在进行中')
            return
        url = self.url_var.get().strip()
        # 去掉收藏项的前缀 "★ "
        if url.startswith('★ '):
            url = url[2:].strip()
            self.url_var.set(url)
        save_dir = self.dir_var.get().strip()
        if not url or not save_dir:
            messagebox.showwarning('提示', '请填写网址和保存位置')
            return
        if not os.path.isdir(save_dir):
            try:
                os.makedirs(save_dir, exist_ok=True)
            except Exception:
                messagebox.showerror('错误', '保存位置无效: %s' % save_dir)
                return
        try:
            threads = max(1, int(self.thread_var.get()))
        except ValueError:
            threads = 4
        try:
            limit = max(0, int(self.limit_var.get()))
        except ValueError:
            limit = 0
        grab_img = self.grab_img.get()
        grab_vid = self.grab_vid.get()
        # 代理：勾选启用才用，否则直连
        proxy = self.proxy_var.get().strip() if self.proxy_enabled.get() else ''
        mode = self.mode_var.get()
        self._save_settings()
        self._record_url_history()

        self.stop_flag.clear()
        # 若表格里已有匹配当前网址/模式的预览结果，则直接用预览列表下载（不再重新抓列表）
        _preview = None
        if self._preview_url == url and self._preview_mode == mode and self._preview_threads:
            _preview = self._preview_threads
        self.worker = threading.Thread(target=self._run_grab, args=(
            url, save_dir, threads, limit, grab_img, grab_vid, proxy, mode, _preview), daemon=True)
        self.worker.start()

    def _on_browser_mode(self):
        if self.browser_mode.get():
            self._log('浏览器模式：点「启动调试浏览器」一键启动 Edge（9222 端口，含油猴/登录态），或先手动开好调试端口')
        else:
            self._log('浏览器模式已关闭')

    def _open_edge_solo(self):
        """独立窗口打开 Edge（用同一个调试profile，方便登录Microsoft账户和装插件）"""
        edge = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        if not os.path.isfile(edge):
            edge = r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
        if not os.path.isfile(edge):
            self._log('错误: 未找到 Edge')
            return
        import edge_profile
        profile_dir = edge_profile.DEBUG_PROFILE_DIR
        # 清理锁文件
        for lf in ['SingletonLock', 'SingletonCookie', 'SingletonSocket', 'lockfile', 'Lockfile']:
            try:
                fp = os.path.join(profile_dir, lf)
                if os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass
        # 不加 --remote-debugging-port，普通方式启动（独立窗口）
        args = [edge, '--no-first-run', '--no-default-browser-check',
                '--disable-session-crashed-bubble',
                '--user-data-dir=' + profile_dir]
        try:
            subprocess.Popen(args, cwd=os.path.dirname(edge))
            self._log('已独立窗口打开 Edge（同一个调试profile）')
            self._log('请在这个窗口里登录Microsoft账户、装好需要的插件，完成后关闭窗口')
            self._log('然后再点「启动调试浏览器」，登录态和插件就会在内嵌浏览器里生效')
        except Exception as e:
            self._log('独立窗口打开 Edge 失败: %s' % e)

    def _start_debug_edge(self):
        """超链接点击：启动/连接调试 Edge，就绪后自动嵌入「浏览器」页签"""
        try:
            import cdp_browser
            if cdp_browser.is_connected():
                self._log('调试浏览器已在运行（9222 端口就绪），正在嵌入...')
                threading.Thread(target=self._wait_edge_ready, daemon=True).start()
                return
        except Exception:
            pass
        edge = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        if not os.path.isfile(edge):
            edge = r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
        if not os.path.isfile(edge):
            self._log('错误: 未找到 Edge，请安装 Microsoft Edge 后再试')
            return
        import edge_profile
        if not edge_profile.profile_exists():
            # 首次：需要复制默认配置到独立调试 profile（油猴扩展/登录态）
            if edge_profile.is_edge_running():
                self._log('首次使用浏览器模式需要复制 Edge 配置，请先完全退出 Edge（含后台）后再点「启动调试浏览器」')
                return
            if not messagebox.askyesno(
                    '首次使用浏览器模式',
                    '需要把你的 Edge 配置（油猴扩展 + 登录态）复制到独立的调试配置目录，\n'
                    '约几百 MB，仅首次一次；之后日常 Edge 与抓取互不影响。\n\n是否继续？'):
                self._log('已取消浏览器模式初始化')
                return
            threading.Thread(target=self._first_time_edge_setup, args=(edge,), daemon=True).start()
            return
        # 启动前检测：如果 Edge 正在运行，提示用户保存工作后自动关闭
        import edge_profile
        if edge_profile.is_edge_running():
            if not messagebox.askyesno(
                    '需要关闭 Edge',
                    '检测到 Edge 正在运行，启动调试浏览器需要先关闭所有 Edge 窗口。\n'
                    '请先保存好你正在浏览的内容，关闭后会自动启动调试模式的 Edge。\n\n是否继续？'):
                self._log('已取消启动调试浏览器')
                return
            self._log('正在关闭所有 Edge 进程...')
            try:
                import subprocess as _sp
                # 杀两次，确保子进程也死干净
                for _k in range(2):
                    _sp.run(['taskkill', '/F', '/IM', 'msedge.exe'],
                            capture_output=True, text=True, timeout=10)
                    import time as _time
                    _time.sleep(1)
                # 循环确认所有 msedge.exe 真的死了，最多等10秒
                for _wait in range(20):
                    _time.sleep(0.5)
                    try:
                        _r = _sp.run(['tasklist', '/FI', 'IMAGENAME eq msedge.exe'],
                                      capture_output=True, text=True, timeout=5)
                        if 'msedge.exe' not in _r.stdout:
                            self._log('所有 Edge 进程已关闭')
                            break
                    except Exception:
                        break
                else:
                    self._log('警告: 仍有 Edge 进程残留，可能导致启动失败')
            except Exception:
                pass
        self._launch_debug_edge(edge)

    def _first_time_edge_setup(self, edge):
        """后台线程：复制 Edge 配置后启动调试浏览器"""
        import edge_profile
        ok, msg = edge_profile.copy_profile(log=self._log)
        if not ok:
            self._log('首次配置复制失败: %s' % msg)
            return
        self._launch_debug_edge(edge)

    def _launch_debug_edge(self, edge):
        """启动调试 Edge（独立调试 profile，端口必开）"""
        import edge_profile
        profile_dir = edge_profile.DEBUG_PROFILE_DIR
        # 启动前清理 profile 锁文件
        lock_files = ['SingletonLock', 'SingletonCookie', 'SingletonSocket',
                      'lockfile', 'Lockfile']
        for _lf in lock_files:
            try:
                _fp = os.path.join(profile_dir, _lf)
                if os.path.exists(_fp):
                    os.remove(_fp)
            except Exception:
                pass
        args = [edge, '--remote-debugging-port=9222',
                '--remote-allow-origins=*',
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-session-crashed-bubble',
                '--user-data-dir=' + profile_dir]
        try:
            _proc = subprocess.Popen(args, cwd=os.path.dirname(edge))
            self._log('正在启动调试浏览器（Edge 9222 端口，PID=%s）...' % _proc.pid)
        except Exception as e:
            self._log('启动调试浏览器失败: %s' % e)
            return
        threading.Thread(target=self._wait_edge_ready, daemon=True).start()

    def _wait_edge_ready(self):
        """后台线程：等调试端口就绪 + 找到 Edge 窗口，回主线程嵌入"""
        self._log('等待调试端口就绪（最多20秒）...')
        import urllib.request as _ur
        import embed_edge
        ok = False
        last_err = ''
        for _try in range(40):
            time.sleep(0.5)
            if _try == 19:
                self._log('等待调试端口中...（已等10秒，Edge 启动较慢请稍候）')
            try:
                with _ur.urlopen('http://127.0.0.1:9222/json/version', timeout=2) as _r:
                    _body = _r.read().decode('utf-8', errors='replace')[:100]
                    ok = True
                    self._log('调试端口 9222 已连接（尝试 %d 次），返回: %s' % (_try + 1, _body))
                    break
            except Exception as _e:
                last_err = str(_e)[:80]
                if _try == 5:
                    self._log('诊断: 前5次连接失败，最后错误: %s' % last_err)
        if not ok:
            self._log('错误: 20秒内未连上 Edge 调试端口 9222，最后错误: %s' % last_err)
            return
        self._log('端口已连接，正在查找 Edge 窗口（最多15秒）...')
        hwnd = embed_edge.find_edge_window(timeout=15)
        if hwnd:
            self._log('找到 Edge 窗口 (hwnd=%s)，正在嵌入...' % hwnd)
            self._ui_q.put(('embed', hwnd))
            self._log('已放入嵌入队列，等待主线程处理...')
        else:
            self._log('调试浏览器已就绪，但未找到 Edge 窗口，请稍后再点「启动调试浏览器」')

    def _embed_edge_now(self, hwnd):
        """主线程：把 Edge 窗口嵌入「浏览器」页签"""
        self._log('主线程开始嵌入 Edge (hwnd=%s)...' % hwnd)
        import embed_edge
        host = self.tab_brw_host
        host.update_idletasks()
        w = max(host.winfo_width(), 320)
        h = max(host.winfo_height(), 200)
        self._log('宿主尺寸: %dx%d, 宿主hwnd=%s' % (w, h, host.winfo_id()))
        try:
            embed_edge.embed_edge(hwnd, int(host.winfo_id()), 0, 0, w, h)
            self.edge_hwnd = hwnd
        except Exception as e:
            self._log('嵌入 Edge 失败: %s' % e)
            import traceback
            self._log('详细错误: %s' % traceback.format_exc())
            return
        self._log('Edge 已嵌入「浏览器」页签（可手动浏览/登录，抓取时自动开标签）')
        if hasattr(self, 'tab_brw_hint'):
            try:
                self.tab_brw_hint.destroy()
            except Exception:
                pass
            del self.tab_brw_hint
        # 切到浏览器页签让用户看到
        try:
            self.nb.select(2)
        except Exception:
            pass

    def _on_host_resize(self, e):
        """宿主 Frame 尺寸变化时同步调整内嵌 Edge 窗口"""
        hwnd = getattr(self, 'edge_hwnd', None)
        if hwnd:
            try:
                import embed_edge
                embed_edge.resize_edge(hwnd, 0, 0, e.width, e.height)
            except Exception:
                pass

    def _stop_grab(self):
        self.stop_flag.set()
        self._log('已请求停止（当前文件下载完成后停止）')

    def _run_grab(self, url, save_dir, threads, limit, grab_img, grab_vid, proxy, mode, preview_threads=None):
        import urllib.parse
        log = self._log
        # 清空任务表格 & 统计
        self._reset_tasks()
        log('========== 开始抓取 ==========')
        if proxy:
            log('使用代理: %s' % proxy)
        else:
            log('直连模式（不经过代理）')
        # 保存路径统一套一层"网站域名"文件夹：保存目录/域名/标题/...
        if url:
            try:
                _h = urllib.parse.urlparse(url).netloc.replace('www.', '')
                if _h:
                    save_dir = os.path.join(save_dir, core.sanitize_filename(_h))
                    os.makedirs(save_dir, exist_ok=True)
                    log('保存到: %s' % save_dir)
            except Exception:
                pass

        # 浏览器模式：通过 CDP 连接调试模式的 Edge（含油猴/登录态）抓取
        if self.browser_mode.get():
            log('抓取模式: 浏览器模式（连接 Edge 调试端口）')
            self._add_task(url, '浏览器抓取')
            try:
                import cdp_browser
                if not cdp_browser.is_connected():
                    log('错误: 未检测到调试模式的浏览器，请用专用快捷方式启动 Edge（9222 端口）')
                    self._update_task(url, status='失败')
                    log('========== 抓取完成 ==========')
                    return
                images, videos = cdp_browser.grab_list_page(url, wait_sec=6, scroll_times=3,
                                                             max_posts=30, log=log, timeout=60)
                title = 'browser_' + str(int(time.time()))
                folder = os.path.join(save_dir, core.sanitize_filename(title))
                os.makedirs(folder, exist_ok=True)
                self.stats_imgs = 0
                self.stats_vids = 0
                _fetcher = core.Fetcher(self.cookie, proxy, self.stop_flag)
                from concurrent.futures import ThreadPoolExecutor
                n = 0
                dl_list = []
                if grab_img:
                    dl_list += [('img', u) for u in images]
                if grab_vid:
                    dl_list += [('vid', u) for u in videos]
                def dl_one(item):
                    import urllib.parse
                    kind, u = item
                    if self.stop_flag.is_set():
                        return 0
                    ext = os.path.splitext(urllib.parse.urlparse(u).path)[1] or ('.jpg' if kind == 'img' else '.mp4')
                    try:
                        _fetcher.download(u, os.path.join(folder, kind + '_%s%s' % (abs(hash(u)) % 1000000, ext)),
                                          referer=url, timeout=30)
                        return 1
                    except Exception:
                        return 0
                if dl_list:
                    with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
                        n = sum(ex.map(dl_one, dl_list))
                self._update_task(url, progress=(n, len(dl_list)), status='完成')
                log('浏览器模式下载完成: %d 个文件' % n)
                self._update_task(url, status='完成')
            except Exception as e:
                log('浏览器模式失败: %s' % e)
                self._update_task(url, status='失败')
            log('========== 抓取完成 ==========')
            return

        # aiart.pics 专用抓取（AI 艺术图库）
        if core.is_aiart_url(url):
            if core.is_aiart_collections_url(url):
                log('识别到 aiart.pics 精选合集，抓取全部合集')
                self._add_task(url, 'aiart.pics 合集')
                def cb(done, total):
                    self._update_task(url, progress=(done, total), status='下载中')
                try:
                    core.grab_aiart_collections(url, save_dir, cookie=self.cookie, proxy=proxy,
                                                grab_img=grab_img, grab_vid=grab_vid, log=log,
                                                max_collections=limit, progress_cb=cb)
                    self._update_task(url, status='完成')
                except Exception as e:
                    log('抓取失败: %s' % e)
                    self._update_task(url, status='失败')
                log('========== 抓取完成 ==========')
                return
            log('识别到 aiart.pics 图库，使用专用抓取')
            self._add_task(url, 'aiart.pics 图库')
            def cb(done, total):
                self._update_task(url, progress=(done, total), status='下载中')
            try:
                core.grab_aiart(url, save_dir, cookie=self.cookie, proxy=proxy,
                                grab_img=grab_img, grab_vid=grab_vid, log=log,
                                max_items=limit, progress_cb=cb)
                self._update_task(url, status='完成')
            except Exception as e:
                log('抓取失败: %s' % e)
                self._update_task(url, status='失败')
            log('========== 抓取完成 ==========')
            return

        # 全站抓取模式：先看入口是否为列表页；是则按"列表翻页+进详情页"抓全部，
        # 否则才 BFS 遍历整个站点
        if mode == '全站抓取':
            log('抓取模式: 全站抓取')
            # 先尝试识别为列表页（页码=翻页数，0=全部翻到底）
            try:
                threads_list = core.grab_list(url, cookie=self.cookie, proxy=proxy,
                                              page_limit=limit if limit > 0 else 1,
                                              auto_page=(limit == 0), log=log,
                                              stop_event=self.stop_flag)
            except Exception as e:
                log('列表解析失败: %s' % e)
                threads_list = []
            if threads_list:
                log('入口是列表页，检测到 %d 个帖子，按列表翻页+进详情页抓取' % len(threads_list))
                for i, (u, t, th) in enumerate(threads_list, 1):
                    self._add_task(u, t, th)
                def work(item):
                    if self.stop_flag.is_set():
                        return
                    u, t, th = item
                    self._update_task(u, status='下载中')
                    def cb(done, total):
                        self._update_task(u, progress=(done, total), status='下载中')
                    try:
                        _r = core.grab_thread(u, t, save_dir, cookie=self.cookie, proxy=proxy,
                                         grab_img=grab_img, grab_vid=grab_vid, log=log,
                                         progress_cb=cb, stop_event=self.stop_flag)
                        _rt = _r[2] if len(_r) >= 3 else t
                        self._update_task(u, status='完成', title=_rt)
                    except core.GrabError as e:
                        self._update_task(u, status='失败')
                        log('  [失败] %s: %s' % (t, e))
                    except Exception as e:
                        self._update_task(u, status='失败')
                        log('  [失败] %s: %s' % (t, e))
                with ThreadPoolExecutor(max_workers=threads) as ex:
                    list(ex.map(work, threads_list))
                log('========== 抓取完成 ==========')
                return
            # 入口不是列表页：BFS 遍历整个站点
            log('入口不是列表页，按 BFS 遍历全站')
            self._add_task(url, '全站抓取')
            def cb(done, total):
                self._update_task(url, progress=(done, total), status='下载中')
            try:
                core.grab_site(url, save_dir, cookie=self.cookie, proxy=proxy,
                               grab_img=grab_img, grab_vid=grab_vid, log=log,
                               page_limit=limit, progress_cb=cb,
                               stop_flag=self.stop_flag, stop_event=self.stop_flag,
                               max_threads=threads)
                self._update_task(url, status='完成')
            except Exception as e:
                log('抓取失败: %s' % e)
                self._update_task(url, status='失败')
            log('========== 抓取完成 ==========')
            return

        # 单个网页模式：直接抓当前页面所有图片/视频
        if mode == '单个网页':
            log('抓取模式: 单个网页')
            # 在表格中显示一行任务
            self._add_task(url, '单个网页抓取')
            def cb(done, total):
                self._update_task(url, progress=(done, total), status='下载中')
            try:
                _r = core.grab_single_page(url, save_dir, cookie=self.cookie, proxy=proxy,
                                           grab_img=grab_img, grab_vid=grab_vid, log=log,
                                           progress_cb=cb, stop_event=self.stop_flag,
                                           title_cb=lambda t: self._update_task(url, title=t))
                if len(_r) >= 4:
                    _n, _v, _t, _fi = _r[:4]
                else:
                    _n, _v = _r[0], _r[1]; _t = '单个网页抓取'; _fi = ''
                self._update_task(url, status='完成', title=_t)
                if _fi:
                    for _iid, _info in self.tasks.items():
                        if _info['url'] == url:
                            threading.Thread(target=self._load_thumb,
                                             args=(_iid, _fi), daemon=True).start()
                            break
            except Exception as e:
                log('抓取失败: %s' % e)
                self._update_task(url, status='失败')
            log('========== 抓取完成 ==========')
            return

        # 自动模式：URL 是单个帖子页 → 直接抓该帖
        if mode == '自动' and core.is_thread_page(url):
            log('检测到单个帖子页，直接抓取该帖')
            # 在表格中显示一行任务（标题先用占位，抓到后更新真实标题+首图）
            self._add_task(url, '单个帖子')
            def cb(done, total):
                self._update_task(url, progress=(done, total), status='下载中')
            try:
                _r = core.grab_thread_page(url, save_dir, cookie=self.cookie, proxy=proxy,
                                           grab_img=grab_img, grab_vid=grab_vid, log=log,
                                           progress_cb=cb, stop_event=self.stop_flag,
                                           title_cb=lambda t: self._update_task(url, title=t))
                if len(_r) >= 4:
                    _n, _v, _t, _fi = _r[:4]
                else:
                    _n, _v = _r[0], _r[1]; _t = '单个帖子'; _fi = ''
                self._update_task(url, status='完成', title=_t)
                if _fi:
                    # 给任务行异步加载首图缩略图
                    for _iid, _info in self.tasks.items():
                        if _info['url'] == url:
                            threading.Thread(target=self._load_thumb,
                                             args=(_iid, _fi), daemon=True).start()
                            break
            except Exception as e:
                log('抓取失败: %s' % e)
                self._update_task(url, status='失败')
            log('========== 抓取完成 ==========')
            return

        # 论坛列表页 / 自动模式：先抓列表（页码 0=自动翻到底）
        if preview_threads:
            threads_list = preview_threads
            log('使用预览的列表（%d 个帖子）开始下载' % len(threads_list))
        else:
            try:
                threads_list = core.grab_list(url, cookie=self.cookie, proxy=proxy,
                                              page_limit=limit if limit > 0 else 1,
                                              auto_page=(limit == 0), log=log,
                                              stop_event=self.stop_flag)
            except Exception as e:
                log('列表解析失败: %s' % e)
                return

        # 自动模式：列表页没找到帖子 → 当作单个网页处理
        if mode == '自动' and not threads_list:
            log('该页面没有检测到论坛帖子链接，按单个网页模式抓取')
            self._add_task(url, '单个网页抓取')
            def cb(done, total):
                self._update_task(url, progress=(done, total), status='下载中')
            try:
                _r = core.grab_single_page(url, save_dir, cookie=self.cookie, proxy=proxy,
                                           grab_img=grab_img, grab_vid=grab_vid, log=log,
                                           progress_cb=cb, stop_event=self.stop_flag,
                                           title_cb=lambda t: self._update_task(url, title=t))
                if len(_r) >= 4:
                    _n, _v, _t, _fi = _r[:4]
                else:
                    _n, _v = _r[0], _r[1]; _t = '单个网页抓取'; _fi = ''
                self._update_task(url, status='完成', title=_t)
                if _fi:
                    for _iid, _info in self.tasks.items():
                        if _info['url'] == url:
                            threading.Thread(target=self._load_thumb,
                                             args=(_iid, _fi), daemon=True).start()
                            break
            except Exception as e:
                log('抓取失败: %s' % e)
                self._update_task(url, status='失败')
            log('========== 抓取完成 ==========')
            return

        if not threads_list:
            log('未找到任何帖子，请检查网址是否正确，或改用"单个网页"模式')
            return
        log('共找到 %d 个帖子' % len(threads_list))

        # 把帖子加入任务表格（带封面缩略图）
        for i, (u, t, th) in enumerate(threads_list, 1):
            self._add_task(u, t, th)

        def work(item):
            if self.stop_flag.is_set():
                return
            u, t, th = item
            self._update_task(u, status='下载中')
            def cb(done, total):
                self._update_task(u, progress=(done, total), status='下载中')
            try:
                _r = core.grab_thread(u, t, save_dir, cookie=self.cookie, proxy=proxy,
                                 grab_img=grab_img, grab_vid=grab_vid, log=log,
                                 progress_cb=cb, stop_event=self.stop_flag,
                                 title_cb=lambda tt: self._update_task(u, title=tt))
                _rt = _r[2] if len(_r) >= 3 else t
                self._update_task(u, status='完成', title=_rt)
            except core.GrabError as e:
                self._update_task(u, status='失败')
                log('  [失败] %s: %s' % (t, e))
            except Exception as e:
                self._update_task(u, status='失败')
                log('  [失败] %s: %s' % (t, e))

        with ThreadPoolExecutor(max_workers=threads) as ex:
            list(ex.map(work, threads_list))
        log('========== 抓取完成 ==========')

    # ---------------- 任务列表 ----------------
    def _reset_tasks(self):
        """清空任务表格和统计（线程安全：非主线程调用时转主线程执行）"""
        if threading.current_thread() is not threading.main_thread():
            self._ui_q.put(('reset',))
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.tasks.clear()
        self.thumb_refs.clear()
        self.total_files = 0
        self.done_files = 0
        self.stat_total.set('图片总数 0')
        self.stat_done.set('已下载 0')
        self.stat_list.set('总列表数 0')
        self.stat_ok.set('访问完成 0')
        self.stat_speed.set('0 KB/s')

    def _add_task(self, url, title, thumb='', status='等待'):
        """在表格中新增一行（线程安全：非主线程调用时转主线程执行）"""
        if threading.current_thread() is not threading.main_thread():
            self._ui_q.put(('add', url, title, thumb, status))
            return
        from datetime import datetime
        date = datetime.now().strftime('%m-%d')
        iid = self.tree.insert('', 'end', values=(len(self.tasks)+1, date, title, '0/0', status))
        self.tasks[iid] = {'url': url, 'title': title, 'done': 0, 'total': 0}
        self.stat_list.set('总列表数 %d' % len(self.tasks))
        if thumb:
            threading.Thread(target=self._load_thumb, args=(iid, thumb), daemon=True).start()

    def _load_thumb(self, iid, thumb_url):
        """后台线程下载缩略图字节，主线程解码显示"""
        try:
            import urllib.request
            import ssl
            hdr = dict(core.BROWSER_HEADERS)
            if self.cookie:
                hdr['Cookie'] = self.cookie
            req = urllib.request.Request(thumb_url, headers=hdr)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            data = urllib.request.urlopen(req, timeout=15, context=ctx).read()
            self._thumb_q.put((iid, data))
        except Exception as e:
            self._log('缩略图失败: %s' % e)

    def _poll_thumbs(self):
        """主线程轮询缩略图更新（在此解码图片，避免跨线程 PhotoImage）"""
        try:
            while True:
                iid, data = self._thumb_q.get_nowait()
                try:
                    img = Image.open(_io.BytesIO(data))
                    img.thumbnail((48, 48))
                    photo = ImageTk.PhotoImage(img)
                    if iid in self.tasks:
                        self.tree.item(iid, image=photo)
                        self.thumb_refs[iid] = photo  # 保持引用防GC
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(150, self._poll_thumbs)

    def _update_task(self, url, progress=None, status=None, title=None):
        """更新表格中对应 url 的行（在线程中调用，通过队列转主线程）"""
        self._task_q.put((url, progress, status, title))

    def _process_task_updates(self):
        """主线程轮询任务更新"""
        try:
            while True:
                _tl = self._preview_q.get_nowait()
                self._fill_preview_table(_tl)
        except queue.Empty:
            pass
        try:
            while True:
                _cmd = self._ui_q.get_nowait()
                if _cmd[0] == 'reset':
                    self._reset_tasks()
                elif _cmd[0] == 'add':
                    _, _u, _t, _th, _st = _cmd
                    self._add_task(_u, _t, _th, _st)
                elif _cmd[0] == 'embed':
                    self._embed_edge_now(_cmd[1])
        except queue.Empty:
            pass
        try:
            while True:
                url, progress, status, title = self._task_q.get_nowait()
                # 找对应行
                for iid, info in list(self.tasks.items()):
                    if info['url'] == url:
                        if title:
                            info['title'] = title
                            self.tree.set(iid, 'title', title)
                        if progress:
                            info['done'], info['total'] = progress
                            self.tree.set(iid, 'progress', '%d/%d' % progress)
                        if status:
                            self.tree.set(iid, 'status', status)
                        if status == '完成':
                            self.done_files += 1
                            self.stat_ok.set('访问完成 %d' % self.done_files)
                        break
        except queue.Empty:
            pass
        self.root.after(200, self._process_task_updates)

    def _on_tree_double(self, event):
        """双击：标题列打开网页，进度列打开图片"""
        region = self.tree.identify('region', event.x, event.y)
        if region != 'cell':
            return
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self.tasks:
            return
        info = self.tasks[iid]
        if col == '#3':  # 标题 → 打开网页
            import webbrowser
            webbrowser.open(info['url'])
        elif col == '#4':  # 进度 → 打开图片文件夹
            save_dir = self.dir_var.get().strip()
            folder = os.path.join(save_dir, core.sanitize_filename(info['title']))
            if os.path.isdir(folder):
                os.startfile(folder)
            else:
                self._log('图片文件夹不存在: %s' % folder)


def main():
    root = tk.Tk()
    GrabberApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
