# -*- coding: utf-8 -*-
"""内置登录窗口 - 用系统浏览器内核打开目标网站，登录后自动保存 cookie
由主程序以子进程方式启动。用法: python login_window.py <网站根地址>
"""
import sys
import os
import json
import time
import webview

APP_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(APP_DIR, 'login_cookie.txt')


def main():
    # 用法: login_window <网站根地址> [cookie文件路径]
    if len(sys.argv) < 2:
        print('缺少网址参数')
        return
    url = sys.argv[1]
    cookie_file = sys.argv[2] if len(sys.argv) > 2 else COOKIE_FILE
    from urllib.parse import urlparse
    p = urlparse(url)
    domain = p.netloc or url

    print('登录窗口已打开: %s' % url)
    print('请在窗口里正常登录该网站')
    print('登录完成后，点窗口里的【完成登录】按钮')
    print('如果窗口里没有该按钮，直接关闭窗口也会自动保存')

    win = webview.create_window(
        '登录 - %s' % domain, url, width=900, height=700,
        js_api=None, confirm_close=False)

    # 注入"完成登录"按钮
    INJECT_JS = r"""
    (function(){
      if (document.getElementById('__done_btn__')) return;
      var btn = document.createElement('div');
      btn.id = '__done_btn__';
      btn.textContent = '完成登录';
      btn.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:999999;'+
        'background:#1890ff;color:#fff;padding:10px 22px;border-radius:6px;'+
        'font-size:15px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.3);';
      btn.onclick = function(){
        try { pywebview.api.done(); } catch(e) { window.pywebviewjsapi && pywebviewjsapi.done(); }
      };
      document.body.appendChild(btn);
    })();
    """

    class Api:
        def done(self):
            try:
                time.sleep(0.3)
                win.destroy()
            except Exception:
                pass

    win.expose(Api().done)

    def on_loaded():
        try:
            time.sleep(1.5)
            win.evaluate_js(INJECT_JS)
        except Exception as e:
            print('注入失败:', e)

    win.events.loaded += on_loaded

    # 启动后监控：窗口关闭时保存 cookie
    def save_cookie():
        try:
            time.sleep(1.5)
            cookies = win.get_cookies()
            parts = []
            for c in cookies:
                # c 可能是 dict 或对象
                name = c.get('name') if isinstance(c, dict) else getattr(c, 'name', None)
                value = c.get('value') if isinstance(c, dict) else getattr(c, 'value', None)
                if name and value is not None:
                    parts.append('%s=%s' % (name, value))
            data = {'domain': domain, 'cookie': '; '.join(parts),
                    'time': time.strftime('%Y-%m-%d %H:%M:%S')}
            with open(cookie_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            print('登录状态已保存: %d 条 cookie' % len(parts))
        except Exception as e:
            print('保存 cookie 失败:', repr(e))

    def on_closed():
        save_cookie()

    win.events.closed += on_closed

    try:
        webview.start()
    except Exception as e:
        print('窗口异常:', repr(e))
        # 兜底保存
        save_cookie()


if __name__ == '__main__':
    main()
