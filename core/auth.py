# -*- coding: utf-8 -*-
"""
认证与授权模块

- 密码：PBKDF2-HMAC-SHA256（200k 轮）+ 每用户独立随机盐，恒定时间比较
- 会话：服务端 session token（secrets.token_urlsafe），可过期、可吊销
- 验证码：PIL 生成的图形验证码，一次性消费，5 分钟过期
- 防爆破：按「用户名」与「IP」双维度计数，超阈值锁定
- 角色：admin（全部）/ operator（运维，无用户管理）/ viewer（只读）
"""
import base64
import hashlib
import hmac
import io
import os
import random
import secrets
import string
import threading
import time

# ---------------------------------------------------------------------------
# 角色与权限
# ---------------------------------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)

ROLE_LABELS = {
    ROLE_ADMIN: "管理员",
    ROLE_OPERATOR: "运维",
    ROLE_VIEWER: "只读",
}

# 权限点 → 允许的角色
PERMISSIONS = {
    "view":        {ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER},   # 查看页面/列表/日志
    "operate":     {ROLE_ADMIN, ROLE_OPERATOR},                 # 创建/启停/删除实例、执行命令
    "account":     {ROLE_ADMIN, ROLE_OPERATOR},                 # 增删账号
    "settings":    {ROLE_ADMIN, ROLE_OPERATOR},                 # 保存默认配置
    "user":        {ROLE_ADMIN},                                # 用户管理、改他人密码
}

PBKDF2_ROUNDS = 200_000
SESSION_TTL = 12 * 3600          # 会话有效期 12 小时
SESSION_TOUCH = 300              # 5 分钟内不重复写库
CAPTCHA_TTL = 300                # 验证码 5 分钟
CAPTCHA_LEN = 4
MAX_FAIL_PER_ACCOUNT = 6         # 单用户名连续失败上限
MAX_FAIL_PER_IP = 20             # 单 IP 连续失败上限
LOCK_SECONDS = 300               # 锁定时长


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF2_ROUNDS)
    return dk.hex(), salt


def verify_password(password, password_hash, salt):
    if not password_hash or not salt:
        return False
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, password_hash)


def password_strength(pw):
    """返回 (分数0-4, 提示文本)。用于前端强度条与后端最低要求。"""
    if not pw:
        return 0, "密码不能为空"
    score = 0
    if len(pw) >= 8:
        score += 1
    if len(pw) >= 12:
        score += 1
    if any(c.islower() for c in pw) and any(c.isupper() for c in pw):
        score += 1
    if any(c.isdigit() for c in pw) or any(c in "!@#$%^&*()-_=+[]{};:,.<>?/|~`" for c in pw):
        score += 1
    if len(pw) < 8:
        return min(score, 1), "至少 8 位"
    labels = ["很弱", "弱", "一般", "较强", "强"]
    return score, labels[min(score, 4)]


def random_password(length=14):
    """生成一个满足强度要求的随机密码（去掉易混淆字符）"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%^*-_"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        score, _ = password_strength(pw)
        if score >= 3:
            return pw


# ---------------------------------------------------------------------------
# 图形验证码
# ---------------------------------------------------------------------------
_CAPTCHA_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class CaptchaStore:
    """内存验证码池：一次性消费，定期清理"""

    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()
        self._last_gc = 0

    def _gc(self):
        now = time.time()
        if now - self._last_gc < 30:
            return
        self._last_gc = now
        with self._lock:
            for k in [k for k, v in self._items.items() if v["expire"] < now]:
                self._items.pop(k, None)

    def new(self):
        self._gc()
        code = "".join(random.choice(_CAPTCHA_ALPHABET) for _ in range(CAPTCHA_LEN))
        cid = secrets.token_urlsafe(16)
        with self._lock:
            self._items[cid] = {"code": code.upper(), "expire": time.time() + CAPTCHA_TTL}
        return cid, code

    def verify(self, cid, code):
        """校验并消费；返回 (ok, reason)"""
        if not cid or not code:
            return False, "缺少验证码"
        with self._lock:
            item = self._items.pop(cid, None)   # 无论对错都消费掉，防止重放
        if not item:
            return False, "验证码已失效，请重新获取"
        if item["expire"] < time.time():
            return False, "验证码已过期"
        if str(code).strip().upper() != item["code"]:
            return False, "验证码错误"
        return True, "ok"

    def render_png(self, code):
        """用 PIL 画一张带干扰的验证码图，返回 data URI。PIL 缺失时回退到 SVG。"""
        try:
            png = self.capture_png(code)
        except ImportError:
            return self.render_svg(code)
        if png is None:
            return self.render_svg(code)
        b64 = base64.b64encode(png).decode("ascii")
        return f"data:image/png;base64,{b64}"

    # ------------------------------------------------------------------
    # 字体解析：多路径探测 + fc-match + Pillow 内置字体三级兜底
    # ------------------------------------------------------------------
    _FONT_CACHE = {}
    _FONT_CANDIDATES = (
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/urw-base35/NimbusSans-Bold.otf",
        "/usr/share/fonts/google-droid-sans-fonts/DroidSans-Bold.ttf",
        "/usr/share/fonts/adobe-source-code-pro/SourceCodePro-Bold.otf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    )

    @classmethod
    def _find_font(cls, size):
        """返回一个可用的字体对象；找不到真实字库时退回 Pillow 内置位图字体。"""
        key = f"f{size}"
        if key in cls._FONT_CACHE:
            return cls._FONT_CACHE[key]
        from PIL import ImageFont

        for path in cls._FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, size)
                    cls._FONT_CACHE[key] = f
                    return f
                except Exception:
                    continue

        # 用 fontconfig 问一下系统默认无衬线字体
        try:
            import subprocess
            out = subprocess.run(["fc-match", "-f", "%{file}", "sans-serif:bold"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            if out and os.path.exists(out):
                f = ImageFont.truetype(out, size)
                cls._FONT_CACHE[key] = f
                return f
        except Exception:
            pass

        # 兜底：Pillow ≥10.1 的 load_default 支持 size；再老的版本用固定位图字体
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
        cls._FONT_CACHE[key] = f
        return f

    @staticmethod
    def _glyph_mask(char, font, size):
        """把单个字符渲染成遮罩图，用于旋转贴图与自检"""
        from PIL import Image, ImageDraw
        canvas = Image.new("L", (size * 2, size * 2), 0)
        d = ImageDraw.Draw(canvas)
        d.text((size // 2, size // 2), char, font=font, fill=255)
        bbox = canvas.getbbox()
        if not bbox:
            return None
        return canvas.crop(bbox)

    def capture_png(self, code, width=132, height=46):
        """
        生成验证码 PNG 原始字节。
        返回 None 表示 PIL 不可用；调用方需回退 SVG。
        """
        from PIL import Image, ImageDraw
        import math

        img = Image.new("RGB", (width, height), (248, 250, 252))
        draw = ImageDraw.Draw(img)

        # 背景干扰线 / 噪点（低对比，不压字符）
        for _ in range(6):
            draw.line([(random.randint(0, width), random.randint(0, height)),
                       (random.randint(0, width), random.randint(0, height))],
                      fill=(random.randint(186, 214), random.randint(198, 226),
                            random.randint(208, 236)), width=1)
        for _ in range(110):
            draw.point((random.randint(0, width), random.randint(0, height)),
                       fill=(random.randint(175, 215), random.randint(186, 222),
                             random.randint(198, 236)))

        font = self._find_font(30)
        n = len(code)
        slot = width / n
        placed = 0
        for i, ch in enumerate(code):
            mask = self._glyph_mask(ch, font, 30)
            if mask is None:
                continue
            angle = random.uniform(-19, 19)
            rot = mask.rotate(angle, expand=True, resample=Image.BICUBIC)
            color = (random.randint(18, 72), random.randint(52, 108), random.randint(118, 188))
            solid = Image.new("RGB", rot.size, color)
            x = int(slot * i + (slot - rot.width) / 2 + random.uniform(-2.5, 2.5))
            y = int((height - rot.height) / 2 + random.uniform(-3.5, 3.5))
            x = max(-2, min(x, width - rot.width + 2))
            y = max(-2, min(y, height - rot.height + 2))
            img.paste(solid, (x, y), rot)
            placed += 1

        # 若一个字符都没画上（极端情况），退回内置字体直接绘制
        if placed == 0:
            from PIL import ImageFont
            try:
                fb = ImageFont.load_default(size=26)
            except TypeError:
                fb = ImageFont.load_default()
            for i, ch in enumerate(code):
                draw.text((int(slot * i) + 12, 8), ch, font=fb, fill=(30, 64, 145))

        # 前景细线压一下字符顶/底（增加 OCR 难度，但不至于不可读）
        draw.line([(0, random.randint(6, height - 6)), (width, random.randint(6, height - 6))],
                  fill=(158, 182, 212), width=1)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def ink_ratio(png_bytes):
        """
        自检用：返回图片中"深色像素"占比。
        验证码真的画出字符时该值明显 > 0；画成空框/空白时会接近 0。
        """
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert("L")
        px = list(im.getdata())
        dark = sum(1 for v in px if v < 170)
        return dark / max(1, len(px))

    @staticmethod
    def render_svg(code):
        """无 PIL 时的降级方案：SVG 文本验证码（仍带干扰）"""
        import html
        parts = []
        for i, ch in enumerate(code):
            x = 18 + i * 28
            y = 32 + random.randint(-4, 4)
            rot = random.randint(-18, 18)
            color = f"rgb({random.randint(20,75)},{random.randint(55,110)},{random.randint(120,190)})"
            parts.append(
                f'<text x="{x}" y="{y}" font-size="26" font-weight="700" fill="{color}" '
                f'transform="rotate({rot} {x} {y})" font-family="monospace">{html.escape(ch)}</text>')
        lines = "".join(
            f'<line x1="{random.randint(0,132)}" y1="{random.randint(0,46)}" '
            f'x2="{random.randint(0,132)}" y2="{random.randint(0,46)}" '
            f'stroke="rgba(148,163,184,.5)" stroke-width="1"/>' for _ in range(5))
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="132" height="46" '
               f'viewBox="0 0 132 46"><rect width="132" height="46" fill="#f8fafc"/>'
               f'{lines}{"".join(parts)}</svg>')
        b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return f"data:image/svg+xml;base64,{b64}"


# ---------------------------------------------------------------------------
# 登录失败限速
# ---------------------------------------------------------------------------
class LoginGuard:
    def __init__(self):
        self._by_user = {}
        self._by_ip = {}
        self._lock = threading.Lock()

    def _check(self, table, key, limit):
        now = time.time()
        rec = table.get(key)
        if not rec:
            return True, 0
        if rec["until"] and rec["until"] > now:
            return False, int(rec["until"] - now)
        if rec["until"] and rec["until"] <= now:
            table.pop(key, None)
        return True, 0

    def check(self, username, ip):
        with self._lock:
            ok1, wait1 = self._check(self._by_user, (username or "").lower(), MAX_FAIL_PER_ACCOUNT)
            ok2, wait2 = self._check(self._by_ip, ip or "-", MAX_FAIL_PER_IP)
        if not ok1:
            return False, f"账号已被临时锁定，请 {wait1} 秒后重试"
        if not ok2:
            return False, f"当前 IP 尝试过于频繁，请 {wait2} 秒后重试"
        return True, "ok"

    def fail(self, username, ip):
        now = time.time()
        with self._lock:
            for table, key, limit in ((self._by_user, (username or "").lower(), MAX_FAIL_PER_ACCOUNT),
                                      (self._by_ip, ip or "-", MAX_FAIL_PER_IP)):
                rec = table.setdefault(key, {"count": 0, "until": 0})
                rec["count"] += 1
                if rec["count"] >= limit:
                    rec["until"] = now + LOCK_SECONDS
                    rec["count"] = 0

    def reset(self, username, ip):
        with self._lock:
            self._by_user.pop((username or "").lower(), None)
            self._by_ip.pop(ip or "-", None)


captcha_store = CaptchaStore()
login_guard = LoginGuard()
