# -*- coding: utf-8 -*-
"""Edge 调试专用 profile：把默认配置（油猴扩展/登录态）复制到独立目录。

背景：Chrome/Edge 136+ 起，浏览器用「默认 user data 目录」启动时，
--remote-debugging-port 会被安全策略忽略，必须搭配 --user-data-dir 指向
非标准目录才能开启调试端口。本模块把默认配置复制一份到独立目录，
端口即可正常开启，且油猴脚本/登录态照常生效。"""
import os
import subprocess

DEFAULT_EDGE_UD = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'User Data')
DEBUG_PROFILE_DIR = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'WebGrabber', 'edge_debug_profile')

# 复制时排除的缓存/无关目录（保留 Extensions / Local Storage / Cookies 等关键数据）
EXCLUDE_DIRS = [
    'Service Worker', 'Cache', 'Code Cache', 'GPUCache',
    'DawnWebGPUCache', 'DawnCache', 'DawnGraphiteCache',
    'GrShaderCache', 'ShaderCache', 'GraphiteDawnCache',
    'image_cache', 'component_crx_cache',
    'Profile 1', 'Profile 2', 'Profile 3', 'Profile 4', 'Profile 5',
]


def profile_exists():
    """调试 profile 是否已就绪"""
    return os.path.isdir(DEBUG_PROFILE_DIR) and os.path.isfile(
        os.path.join(DEBUG_PROFILE_DIR, 'Local State'))


def is_edge_running():
    """检测是否有 msedge 进程在运行（复制期间会锁文件）"""
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq msedge.exe'],
                             capture_output=True, text=True, timeout=10).stdout
        return 'msedge.exe' in out
    except Exception:
        return False


def copy_profile(log=None):
    """把默认 Edge 配置复制到调试 profile（排除缓存），返回 (ok, message)"""
    def lg(m):
        if log:
            log(m)
    if not os.path.isdir(DEFAULT_EDGE_UD):
        return False, '未找到 Edge 默认配置目录'
    try:
        os.makedirs(DEBUG_PROFILE_DIR, exist_ok=True)
    except Exception as e:
        return False, '无法创建调试配置目录: %s' % e
    lg('开始复制 Edge 配置（油猴扩展 + 登录态，约几百 MB，首次仅一次，请稍候）...')
    cmd = ['robocopy', DEFAULT_EDGE_UD, DEBUG_PROFILE_DIR, '/E',
           '/XD'] + EXCLUDE_DIRS + [
        '/XF', '*.tmp', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1']
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return False, '复制进程异常: %s' % e
    # robocopy 退出码：0-7 表示成功（含复制了文件/未变化），8+ 为错误
    if p.returncode >= 8:
        return False, '复制失败（robocopy 错误码 %d），请确认 Edge 已完全退出' % p.returncode
    # 删除 First Run 标记，避免复制出的 profile 弹首次设置向导
    for name in ('First Run', 'First Run Dev'):
        fr = os.path.join(DEBUG_PROFILE_DIR, name)
        if os.path.exists(fr):
            try:
                os.remove(fr)
            except Exception:
                pass
    lg('Edge 调试配置复制完成，后续启动无需再复制')
    return True, 'ok'
