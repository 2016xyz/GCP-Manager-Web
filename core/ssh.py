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


_TOFU_SEEN = {}          # {hostname: fingerprint}
_TOFU_CLS = None         # 惰性构建（paramiko 是可选依赖，模块导入时可能还没装）


def _tofu_policy_cls():
    """
    构建 TOFU 主机密钥策略类。

    为什么不用 AutoAddPolicy：它会**静默**接受任何主机密钥，意味着中间人
    可以完整接管这次 SSH（拿到 root 密码、注入安装命令）。产品连接的虽然是
    用户自己刚建的实例，但链路上仍可能被动过手脚（DNS 劫持、同网段欺骗、代理被控）。
    这里首次连接记录指纹，之后不一致就中断连接。

    惰性构建的原因：paramiko 在本模块是可选依赖（未安装时产品仍要能起），
    所以不能在建模块时就去继承它的类。
    """
    global _TOFU_CLS
    if _TOFU_CLS is not None:
        return _TOFU_CLS

    class TofuPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            fp = key.get_fingerprint().hex()
            known = _TOFU_SEEN.get(hostname)
            if known is None:
                _TOFU_SEEN[hostname] = fp
                return
            if known != fp:
                raise Exception(
                    f"主机密钥与首次连接不一致（{hostname}）：已知 {known[:16]}… "
                    f"本次 {fp[:16]}…，可能存在中间人，已中断连接")

    _TOFU_CLS = TofuPolicy
    return _TOFU_CLS


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
    # TOFU：首次记录指纹，之后不一致就中断（替代原来的 AutoAddPolicy 静默信任）
    client.set_missing_host_key_policy(_tofu_policy_cls()())
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
        if transport is None:
            # 连接建立失败时 get_transport() 会返回 None；早前直接往下走
            # 会在 transport.set_keepalive 处抛 AttributeError（报错信息毫无线索）
            raise RuntimeError("SSH 传输通道建立失败（connect 返回后未拿到 transport）")
        transport.set_keepalive(keepalive)

        # 打开一个 session 通道执行命令。
        # 早前这里写的是 `client.get_channel(0) if False else transport.open_session()`，
        # 那条件恒为假，等于给后来的人留了个「这行到底走哪条分支」的谜题 ——
        # 直接写成它实际执行的语义。
        chan = transport.open_session()
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
# 日志文件先建成 600 再接管输出：默认 umask 下 tee 会建出 644，
# 而同机其它用户不应该看到这份安装过程记录
: > "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "[INFO] starting root password mode setup"
export DEBIAN_FRONTEND=noninteractive
# ★ 改密码这一行必须关掉 trace。
#   `set -x` 会把每条命令连参数一起回显到 stderr，而上面刚把 stderr 重定向进了
#   $LOG_FILE —— 于是 'root:<明文密码>' 会被原样写进实例上的日志文件，
#   任何能读该文件的进程/人都能拿到 root 密码。这不是理论问题：
#   startup-script 的日志同时也是串口输出，会进 GCP 的串口日志缓冲区。
set +x
echo root:{password} | chpasswd
set -x
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
    # ★ 用 shlex.quote 做完整的 shell 转义，而不是只替换单引号。
    # 原来的 replace("'", "'\"'\"'") 只防了单引号场景；密码含 $、`、\n、{、} 等
    # 字符时仍可能导致命令注入或 .format() 的 KeyError/格式注入。
    # shlex.quote 会把整个字符串包在单引号里并正确转义内部的单引号。
    import shlex
    pw = root_password or ""
    # 密码里不应该有换行符/控制字符（chpasswd 不支持），主动剥掉
    pw = pw.replace("\n", "").replace("\r", "").replace("\0", "")
    safe_pw = shlex.quote(pw)
    # 用字符串拼接而非 .format()，避免密码中的 { } 被当成格式占位符
    return ROOT_STARTUP_SCRIPT_TEMPLATE.replace("{password}", safe_pw)
