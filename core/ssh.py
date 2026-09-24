# -*- coding: utf-8 -*-
"""
SSH 执行引擎（Web 版）— 移植原版 v7.6 的 run_ssh_command / wait_auto_ssh
并支持通过 WebSocket/日志回调实时回显。
"""
import logging
import socket
import threading
import time

PARAMIKO_AVAILABLE = False
paramiko = None


def check_paramiko():
    global PARAMIKO_AVAILABLE, paramiko
    if not PARAMIKO_AVAILABLE:
        try:
            import paramiko as _p
            paramiko = _p
            for name in ("paramiko", "paramiko.transport", "paramiko.auth_handler", "paramiko.packet"):
                lg = logging.getLogger(name)
                lg.setLevel(logging.CRITICAL)
                lg.propagate = False
            PARAMIKO_AVAILABLE = True
        except ImportError:
            PARAMIKO_AVAILABLE = False
    return PARAMIKO_AVAILABLE


class SSHStopper:
    """可被外部置为停止的执行控制器"""

    def __init__(self):
        self.stopped = False
        self.lock = threading.Lock()

    def stop(self):
        with self.lock:
            self.stopped = True

    def is_stopped(self):
        with self.lock:
            return self.stopped


def run_ssh_command(ip, username, password, command,
                    key_path=None,
                    connect_timeout=15, idle_timeout=180, total_timeout=1800,
                    keepalive=30, port=22,
                    log_callback=None, stopper=None, stop_flag=None):
    """
    返回 (ok: bool, output: str)
    移植自原版 v7.6 run_ssh_command，改为无 Qt 依赖的纯回调版本。
    """
    if not check_paramiko():
        return False, "paramiko 未安装，请执行 pip install paramiko"

    def emit(text):
        if log_callback:
            try:
                log_callback(text)
            except Exception:
                pass

    def is_stopped():
        if stopper is not None and stopper.is_stopped():
            return True
        if stop_flag is not None and callable(stop_flag) and stop_flag():
            return True
        return False

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    chan = None
    buffer = []
    try:
        connect_kwargs = dict(hostname=ip, port=port, username=username,
                              timeout=connect_timeout, banner_timeout=connect_timeout,
                              auth_timeout=connect_timeout)
        if key_path:
            connect_kwargs["key_filename"] = key_path
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        else:
            connect_kwargs["password"] = password
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        client.connect(**connect_kwargs)
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(keepalive)

        chan = client.get_channel(0) if False else transport.open_session()
        chan.get_pty()
        chan.set_combine_stderr(True)
        chan.settimeout(0.0)
        chan.exec_command(command)

        start = time.time()
        last_data = time.time()
        while True:
            if is_stopped():
                emit("\n[已手动停止]")
                try:
                    chan.close()
                except Exception:
                    pass
                return False, "".join(buffer) + "\n[已手动停止]"
            if chan.recv_ready():
                data = chan.recv(65536)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    buffer.append(text)
                    last_data = time.time()
                    emit(text)
                continue
            if chan.exit_status_ready() and not chan.recv_ready():
                # 关闭前把残余读干净
                while chan.recv_ready():
                    data = chan.recv(65536)
                    if data:
                        text = data.decode("utf-8", errors="replace")
                        buffer.append(text)
                        emit(text)
                break
            now = time.time()
            if now - last_data > idle_timeout:
                emit(f"\n[空闲超时 {idle_timeout}s，终止]")
                try:
                    chan.close()
                except Exception:
                    pass
                return False, "".join(buffer) + f"\n[空闲超时 {idle_timeout}s]"
            if now - start > total_timeout:
                emit(f"\n[总超时 {total_timeout}s，终止]")
                try:
                    chan.close()
                except Exception:
                    pass
                return False, "".join(buffer) + f"\n[总超时 {total_timeout}s]"
            time.sleep(0.01)

        code = chan.recv_exit_status()
        out = "".join(buffer)
        return code == 0, out
    except Exception as exc:
        return False, ("".join(buffer) + f"\n[SSH 错误] {exc}").strip()
    finally:
        try:
            if chan:
                chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def wait_ssh_ready(ip, username, password, key_path=None, timeout=300, port=22,
                   log_callback=None, stopper=None, stop_flag=None):
    """轮询端口 + 认证，直到 SSH 可用"""
    deadline = time.time() + max(30, int(timeout or 300))
    last_error = ""
    while time.time() < deadline:
        if stopper is not None and stopper.is_stopped():
            return False, "已停止"
        if stop_flag is not None and callable(stop_flag) and stop_flag():
            return False, "已停止"
        try:
            sock = socket.create_connection((ip, port), timeout=5)
            sock.close()
        except Exception as exc:
            last_error = f"端口未开放：{exc}"
            time.sleep(5)
            continue
        ok, out = run_ssh_command(ip, username, password, "echo __SSH_READY__",
                                  key_path=key_path, connect_timeout=8,
                                  idle_timeout=8, total_timeout=20, stopper=stopper)
        if ok and "__SSH_READY__" in (out or ""):
            return True, "ready"
        last_error = out or "ssh 认证失败"
        time.sleep(5)
    return False, last_error


ROOT_STARTUP_SCRIPT_TEMPLATE = """#!/bin/bash
set -euxo pipefail
mkdir -p /root
LOG_FILE=/root/gcp_root_mode.log
exec > >(tee -a "$LOG_FILE") 2>&1
echo "[INFO] starting root password mode setup"
export DEBIAN_FRONTEND=noninteractive
echo 'root:{password}' | chpasswd
passwd -u root || true
if [ -f /etc/ssh/sshd_config ]; then
  sed -i 's/^\\s*#\\?\\s*PermitRootLogin.*/PermitRootLogin yes/g' /etc/ssh/sshd_config || true
  sed -i 's/^\\s*#\\?\\s*PasswordAuthentication.*/PasswordAuthentication yes/g' /etc/ssh/sshd_config || true
  sed -i 's/^\\s*#\\?\\s*KbdInteractiveAuthentication.*/KbdInteractiveAuthentication yes/g' /etc/ssh/sshd_config || true
  sed -i 's/^\\s*#\\?\\s*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication yes/g' /etc/ssh/sshd_config || true
  grep -q '^PermitRootLogin yes$' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
  grep -q '^PasswordAuthentication yes$' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config
  grep -q '^KbdInteractiveAuthentication yes$' /etc/ssh/sshd_config || echo 'KbdInteractiveAuthentication yes' >> /etc/ssh/sshd_config
  grep -q '^ChallengeResponseAuthentication yes$' /etc/ssh/sshd_config || echo 'ChallengeResponseAuthentication yes' >> /etc/ssh/sshd_config
fi
rm -rf /etc/ssh/sshd_config.d/* /etc/ssh/ssh_config.d/* || true
mkdir -p /etc/ssh/sshd_config.d /etc/ssh/ssh_config.d
cat >/etc/ssh/sshd_config.d/99-root-password.conf <<'EOF'
PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication yes
ChallengeResponseAuthentication yes
UsePAM yes
EOF
sshd -t || sshd -T || true
systemctl restart ssh || systemctl restart sshd || service ssh restart || service sshd restart || true
sleep 2
echo "[INFO] root password mode setup finished"
touch /root/.gcp_root_mode_ok
"""


def build_root_startup_script(root_password):
    safe = (root_password or "").replace("'", "'\"'\"'")
    return ROOT_STARTUP_SCRIPT_TEMPLATE.format(password=safe)
